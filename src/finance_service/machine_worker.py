"""Read-only daily observations using the existing durable lease/outbox worker."""
import json
import os
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from economic_machine.values import MachineError, digest
from .context import AuthenticatedContext
from .machine_bridge import MachineBridge
from .machine_observations import MachineObservations
from .postgres_repository import PostgresOperationalRepository
from .worker import FinanceWorker, RetryableJobError


def next_run(at, daily_time, timezone):
    hour, minute = (int(part) for part in daily_time.split(":"))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise MachineError("invalid daily routine time")
    zone = ZoneInfo(timezone)
    local = datetime.fromisoformat(at).astimezone(zone)
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(UTC).isoformat()

def job_context(job, clock):
    at = clock()
    return AuthenticatedContext(**job["scope"], session_id="watch-worker", trace_id="watch-" + job["job_id"],
        issued_at=at, expires_at=(datetime.fromisoformat(at)+timedelta(minutes=5)).isoformat())


def observe_routine(bridge, job):
    ctx = job_context(job, bridge.clock)
    version, state = bridge.load(ctx)
    w = state['workspace']
    routine = next((r for r in w['routines'] if r['id'] == job['routine_id']), None)
    if routine is None or not routine['enabled'] or digest(routine) != job['dependency_hash']:
        return {'status': 'CANCELLED', 'execution_authority': 'NONE'}
    review = bridge.reviews.refresh(ctx, state, source='ROUTINE', notify=bool(routine['notify_on']))
    at = bridge.clock()
    routine.update(next_due_at=next_run(at, routine['time'], routine['timezone']),
        last_checked_at=at, last_review_id=review['id'], last_result=review['status'],
        last_error=review['reason'] if review['status']=='DATA_UNAVAILABLE' else None,
        valid_until=review['expires_at'], worker_status='CONNECTED')
    if review['status'] != 'DATA_UNAVAILABLE': routine['last_success_at'] = at
    w['activity'].append({'id':job['job_id'],'role':'watch','title':'Portfolio routine completed',
        'detail':review['reason'],'at':at,'severity':'INFO' if review['status']=='HOLD' else 'WARNING'})
    w['activity'] = w['activity'][-200:]
    try:
        bridge.commit(ctx, version, state)
    except MachineError as exc:
        if 'version conflict' in str(exc): raise RetryableJobError('WORKSPACE_CHANGED') from exc
        raise
    return {'status': review['status'], 'review_id':review['id'], 'execution_authority':'NONE'}


def main():
    clock = lambda: datetime.now(UTC).isoformat()
    repository = PostgresOperationalRepository(api_dsn_file=os.environ["FINANCE_SERVICE_POSTGRES_API_DSN_FILE"], worker_dsn_file=os.environ["FINANCE_SERVICE_POSTGRES_WORKER_DSN_FILE"])
    observations = MachineObservations(clock)
    bridge = MachineBridge(repository, clock, None, observations)

    worker = FinanceWorker(repository, {"MACHINE_DAILY_OBSERVATION": lambda job: observe_routine(bridge, job)}, worker_id="machine-watch-1", role="WATCH", lease_seconds=180)
    heartbeat = Path('/var/lib/machine-finance/worker-heartbeat')
    while True:
        at = clock()
        with repository.connect(worker=True) as db:
            rows = db.execute("SELECT body_json,tenant_id,owner_id,wallet,network FROM finance_service_records WHERE record_kind='MACHINE_STATE' AND record_id='current'").fetchall()
        for row in rows:
            state = row['body_json'] if isinstance(row['body_json'],dict) else json.loads(row['body_json'])
            scope = {k: row[k] for k in ('tenant_id','owner_id','wallet','network')}
            recover_expired_requests(bridge, scope, state)
            if state.get('native_execution', {}).get('submission') and (state.get('native_execution', {}).get('reconciliation') or {}).get('status') != 'RECONCILED' and (state['workspace'].get('execution') or {}).get('status') not in {'POSITION_RECONCILED','FAILED','DISPUTED'}:
                try:
                    ctx=job_context({'scope':scope,'job_id':'native-reconcile'},clock)
                    bridge.native.reconcile(ctx)
                except Exception as exc:
                    print('Native reconciliation pending:', type(exc).__name__, str(exc), flush=True)
            wf=state.get('stake_workflow')
            if wf and wf['steps'][wf['cursor']].get('submission') and wf['steps'][wf['cursor']]['status'] not in ('POSITION_RECONCILED','FAILED','DISPUTED'):
                try:bridge.stake.reconcile(job_context({'scope':scope,'job_id':'stake-reconcile'},clock))
                except Exception as exc:print('Staking reconciliation pending:',type(exc).__name__,str(exc),flush=True)
            for r in state['workspace']['routines']:
                if r['enabled'] and r.get('next_due_at', at) <= at:
                    repository.enqueue_job(scope, role='WATCH',routine_id=r['id'],job_kind='MACHINE_DAILY_OBSERVATION',subject_id='current',dependency_hash=digest(r),payload={'routine_id':r['id']},not_before=at,expires_at=(datetime.now(UTC)+timedelta(minutes=10)).isoformat(),max_attempts=3,priority=20)
        worker.run_one(at=clock())
        heartbeat.write_text(clock())
        time.sleep(15)

def recover_expired_requests(bridge, scope, state):
    from .native_recovery import pending_requests
    now = int(datetime.fromisoformat(bridge.clock()).timestamp()*1000)
    for txid, request in pending_requests(state):
        if now <= request['transaction']['raw_data']['expiration'] + 60000:
            continue
        try:
            ctx = job_context({'scope': scope, 'job_id': 'prepared-recovery'}, bridge.clock)
            (bridge.stake if request.get('adapter')=='STAKE' else bridge.native).reconcile(ctx, {'txid': txid, 'graph_id': request['graph_id'], 'step_id': request['step_id']})
        except Exception as exc:
            # Failure, visibility or an insufficient solidified head retains
            # the lock. Never cancel solely on elapsed wall-clock time.
            print('Prepared transaction verification pending:', type(exc).__name__, str(exc), flush=True)


if __name__ == '__main__': main()
