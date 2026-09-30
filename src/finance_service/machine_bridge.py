"""Machine UI adapter. Trusted gateway authentication precedes all scope use.

A single CAS record commits UI projections and canonical mandate aggregates
atomically through the PR11/12 repository and its scoped audit journal.
"""
import hmac
import json
import os
import secrets
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from economic_machine.mandate import TERM_KEYS, normalize_terms
from economic_machine.tron_sources import address_hex
from economic_machine.values import MachineError, canonical, digest, ident, utc
from .context import AuthenticatedContext
from .mandate_service import MandateService
from .repository import VersionConflict


def empty_workspace(network):
    return dict(revision=0, network=network, mode="LIVE", messages=[], mandate=None,
        comparison=None, graph=None, approval=None, execution=None, positions=[],
        performance=None, balances=[], snapshots=[], routines=[], evidence=[],
        activity=[], jobs=[], intent=None, notifications=[], portfolio_review=None, usdd_review=None, usdd_workflow=None, stake_workflow=None, stake_position=None)


class StateMandates:
    """Transaction-local mandate port; the outer repository commits the whole state."""
    def __init__(self, state):
        self.state = state

    def read(self, scope, mandate_id):
        row = self.state["mandates"].get(mandate_id)
        if row is None or row["scope"] != scope:
            raise MachineError("mandate not found in authenticated scope")
        return deepcopy(row)

    def transact(self, scope, mandate_id, expected_version, mutation):
        row = self.state["mandates"].get(mandate_id)
        if row is not None and row["scope"] != scope:
            raise MachineError("mandate not found in authenticated scope")
        if (row["version"] if row else 0) != expected_version:
            raise VersionConflict("mandate aggregate changed; reload before retry")
        candidate = mutation(deepcopy(row))
        if candidate["scope"] != scope or candidate["mandate_id"] != mandate_id:
            raise MachineError("repository mutation changed account identity")
        candidate["version"] = expected_version + 1
        self.state["mandates"][mandate_id] = deepcopy(candidate)
        return candidate


class MachineBridge:
    def __init__(self, repository, clock, intent_service, observations=None):
        self.repository, self.clock, self.intents = repository, clock, intent_service
        self.observations = observations
        from .nile_funding import NileFunding
        self.funding = NileFunding(repository, clock, observations.request) if observations else None
        from .native_execution import NativeExecution
        self.native = NativeExecution(self, observations.request) if observations else None
        from .portfolio_review import PortfolioReviews
        self.reviews = PortfolioReviews(self)
        from .usdd_review import UsddReviews
        self.usdd = UsddReviews(self)
        from .usdd_execution import UsddExecution
        self.usdd_execution = UsddExecution(self)
        from .stake_execution import StakeExecution
        self.stake = StakeExecution(self)

    def load(self, context):
        scope = context.authorize(self.clock())
        try:
            row = self.repository.get_record(scope, "MACHINE_STATE", "current")
            return row["version"], row["body"]
        except MachineError as exc:
            if str(exc) != "service record not found in authenticated scope":
                raise
            return 0, {"workspace": empty_workspace(scope["network"].removeprefix("tron-")),
                       "mandates": {}, "requests": {}}

    def commit(self, context, version, state):
        state["workspace"]["revision"] = version + 1
        return self.repository.put_record(context.scope, "MACHINE_STATE", "current",
            state, expected_version=version, at=self.clock())

    def workspace(self, context):
        _, state = self.load(context)
        w = state["workspace"]
        # Rebuild display bindings from durable native records, including older
        # workspaces. Reading does not renew a review or change its approval.
        self.stake.project_workflow(state)
        from .stake_comparison import project_evidence
        project_evidence(state)
        from .product_catalog import catalog
        w['product_catalog']=catalog(context.network)
        from .portfolio_review import active_mandate, withdrawal_policy
        w['active_mandate']=active_mandate(state)
        w['withdrawal_policy']=withdrawal_policy(state)
        from .native_recovery import recovery_projection
        w['prepared_transactions'] = recovery_projection(state, context)
        w['stake_transaction_receipts'] = [{'txid':txid,'graph_id':row['graph_id'],'step_id':row['step_id'],'status':state['stake_receipts'][txid]['status']} for txid,row in state.get('native_requests',{}).items() if row.get('adapter')=='STAKE' and txid in state.get('stake_receipts',{})]
        w['transaction_resolutions'] = [r['resolution'] for r in state.get('native_requests', {}).values() if r.get('resolution')]
        heartbeat = Path("/var/lib/machine-finance/worker-heartbeat")
        connected = False
        try:
            age = (datetime.fromisoformat(self.clock()) - datetime.fromisoformat(heartbeat.read_text().strip())).total_seconds()
            connected = 0 <= age < 120
        except (OSError, ValueError):
            pass
        w["planning_assumptions"] = state.get("planning_assumptions")
        for routine in w["routines"]:
            routine["worker_status"] = "CONNECTED" if connected else "OFFLINE"
        w["jobs"] = [{"id": j["job_id"], "label": j["job_kind"].replace("_", " ").title(),
            "status": {"PENDING": "QUEUED", "RETRY_WAIT": "QUEUED", "LEASED": "RUNNING", "EXPIRED": "FAILED", "HELD_EXPIRED": "FAILED"}.get(j["status"], j["status"]),
            "error": j.get("error_code")} for j in self.repository.list_jobs(context.scope, limit=30)]
        return w

    @staticmethod
    def projection(aggregate, constraints, source_text):
        record = aggregate["revisions"][-1]
        raw = record["mandate"]
        return dict(id=aggregate["mandate_id"], version=aggregate["version"],
            hash=record["draft_hash"] if record["status"] == "DRAFT" else record["policy_hash"],
            network=raw["scope"]["network"].removeprefix("tron-"),
            status=record["status"], source_text=source_text, constraints=constraints,
            missing_fields=[], confirmed_at=record.get("confirmed_at"), terms=raw["terms"])

    def mutate(self, context, method, path, payload, request_key):
        version, state = self.load(context)
        request_hash = digest({"method": method, "path": path, "payload": payload})
        key = digest({"key": request_key})
        prior = state["requests"].get(key)
        if prior:
            if prior["hash"] != request_hash:
                raise MachineError("request key was reused with different content")
            return prior["response"]
        w = state["workspace"]
        from .native_recovery import pending_requests
        if pending_requests(state) and (path.startswith('/v1/mandates') or path in ('/v1/plan-comparisons','/v1/execution-graphs','/v1/portfolio-adjustments','/v1/usdd-workflows','/v1/approvals')):
            raise MachineError('Your previous wallet request is still being checked. Open the transaction to continue; expired requests unlock automatically after chain verification.')
        if w.get("execution") and w["execution"]["status"] not in {"POSITION_RECONCILED", "FAILED"} and (path.startswith("/v1/mandates") or path in {"/v1/plan-comparisons", "/v1/execution-graphs", "/v1/portfolio-adjustments", "/v1/approvals"}):
            raise MachineError("Reconcile the recorded transaction before changing its plan or approval.")
        active_usdd=state.get('usdd_execution')
        if active_usdd and active_usdd['status'] in ('AWAITING_SIGNATURE','SUBMISSION_UNKNOWN','AWAITING_NEXT_REVIEW','DISPUTED'):
            current=active_usdd['steps'][active_usdd['cursor']]
            can_revise=active_usdd['status'] in ('AWAITING_SIGNATURE','AWAITING_NEXT_REVIEW') and not current.get('transaction') and not current.get('submission')
            if (path.startswith('/v1/mandates') and not can_revise) or path in ('/v1/plan-comparisons','/v1/execution-graphs','/v1/portfolio-adjustments'):
                raise MachineError('Reconcile the prepared USDD transaction before changing its conditions. Completed steps require a recovery review under any new conditions.')
        active_stake=state.get('stake_workflow')
        can_revise_stake=active_stake and not active_stake['steps'][active_stake['cursor']].get('transaction') and not active_stake['steps'][active_stake['cursor']].get('submission')
        if active_stake and active_stake['status'] not in ('COMPLETE','CANCELLED','FAILED') and ((path.startswith('/v1/mandates') and not can_revise_stake) or path in ('/v1/plan-comparisons','/v1/execution-graphs','/v1/portfolio-adjustments','/v1/usdd-workflows')):
            raise MachineError('Finish the native stake review, cancel its unprepared draft, or reconcile its transaction before changing conditions.')
        mandates = MandateService(StateMandates(state), self.clock)
        result = {"accepted": True, "job_id": None}
        if method == "POST" and path == "/v1/agent-intent":
            from .agent_intent import QUESTIONS
            prior = w.get("intent") or {}
            terms = (w.get("mandate") or {}).get("terms") or {}
            baseline = {k: terms[k] for k in QUESTIONS if k in terms}
            if "borrowing" in terms:
                baseline.update(borrowing_consent=terms["borrowing"]["consent"], max_debt=terms["borrowing"]["max_debt"])
            current = {**baseline, **prior.get("patch", {})}
            result = self.intents.propose(context, payload["message"], current)
            merged = {**prior.get("patch", {}), **result["patch"]}
            known = {**baseline, **merged}
            questions = [{"field": k, "question": v} for k, v in QUESTIONS.items()
                         if (k not in known or (k == "max_debt" and known.get("borrowing_consent") is True and Decimal(str(known.get(k, {}).get("amount", "0"))) <= 0)) and (k != "max_debt" or known.get("borrowing_consent") is True)]
            history = prior.get("source_messages", [])[-7:] + [payload["message"]]
            # Retain the message supporting each field even when the recent history rolls over.
            sources = {**prior.get("field_sources", {}), **{k: payload["message"] for k in result["patch"]}}
            source_text = "\n".join(dict.fromkeys([*sources.values(), *history]))
            proposal = {**result, "patch": merged, "questions": questions, "known": known,
                "evidence": {**prior.get("evidence", {}), **result.get("evidence", {})},
                "field_sources": sources, "source_messages": history, "source_text": source_text, "created_at": self.clock(),
                "agent_id": payload.get("agent_id"), "trigger_message_id": payload.get("message_id")}
            if result["patch"]:
                proposal["status"] = "NEEDS_INFORMATION" if questions else "DRAFT_READY"
                w["intent"] = proposal
                result = proposal
            else:
                # A portfolio/read-only message must not re-open stale conditions.
                result = {**result, "known": current, "questions": questions, "pending_proposal": bool(prior.get("patch"))}
        elif method == 'POST' and path == '/v1/stake-workflows/next':
            self.stake.next(context,state)
        elif method == 'POST' and path == '/v1/stake-workflows/cancel':
            self.stake.cancel(context,state)
        elif method == 'POST' and path == '/v1/stake-workflows/lifecycle':
            self.stake.lifecycle(context,state,payload)
        elif method == 'POST' and path == '/v1/stake-workflows/refresh':
            self.stake.refresh_position(context,state)
        elif method == "POST" and path == "/v1/usdd-workflows":
            self.usdd_execution.create(context,state,payload)
        elif method == "POST" and path == "/v1/usdd-workflows/next":
            self.usdd_execution.next(context,state)
        elif method == "POST" and path == "/v1/usdd-workflows/recover":
            self.usdd_execution.recover(context,state,payload)
        elif method == "POST" and path == "/v1/usdd-workflows/rewards":
            self.usdd_execution.rewards(context,state)
        elif method == "POST" and path == "/v1/usdd-workflows/cancel":
            self.usdd_execution.cancel(context,state)
        elif method == "POST" and path == "/v1/usdd-reviews":
            self.usdd.refresh(context, state)
        elif method == "POST" and path == "/v1/portfolio-reviews":
            self.reviews.refresh(context, state)
        elif method == "POST" and path == "/v1/performance/periods":
            from .native_performance import start_period
            result = start_period(self, context, state, payload)
        elif method == "POST" and path == "/v1/portfolio-adjustments":
            from .native_adjustments import review_adjustment
            review_adjustment(self, context, state, payload)
        elif method == "POST" and path.startswith("/v1/notifications/") and path.endswith("/read"):
            notice = next((n for n in w.get("notifications", []) if n["id"] == path.split("/")[3]), None)
            if notice is None:
                raise MachineError("notification not found in authenticated scope")
            notice["read_at"] = notice.get("read_at") or self.clock()
        elif method == "POST" and path == "/v1/mandates":
            from .portfolio_review import confirmed_policy
            confirmed_policy(state)  # preserve confirmed inputs before a new draft replaces them
            terms = normalize_terms(payload["terms"])
            # A submitted form is a user source, distinct from model-inferred quotes.
            source = canonical(terms).decode()
            mid = payload.get("previous_id") or "mandate-" + secrets.token_hex(12)
            previous = state["mandates"].get(mid)
            if payload.get("previous_id") and previous is None:
                raise MachineError("previous mandate not found")
            raw = dict(schema_version="economic-mandate-1", mandate_id=mid,
                revision=len(previous["revisions"]) + 1 if previous else 1,
                scope=context.scope, trace_id=context.trace_id, terms=terms,
                source_messages=[{"message_id": "confirmed-form", "text": source}],
                source_refs={field: [{"message_id": "confirmed-form", "quote":
                    canonical(terms[field]).decode()}] for field in TERM_KEYS})
            aggregate = mandates.revise(context, raw, expected_version=previous["version"]) if previous else mandates.create(context, raw)
            constraints = deepcopy(payload["constraints"])
            expected = {"capital": {"value": terms["capital"][0]["amount"], "symbol": terms["base_asset"], "decimals": 18 if terms["base_asset"]=="USDD" else 6},
                "horizon_days": terms["horizon_seconds"] // 86400,
                "min_cash_bps": terms["immediate_cash"].get("value"),
                "max_trx_exposure_bps": terms["price_exposure_caps_bps"].get("TRX"),
                "allow_debt": terms["borrowing"]["consent"]}
            if any(constraints.get(k) != v for k, v in expected.items()):
                raise MachineError("displayed constraints differ from canonical policy")
            state["planning_assumptions"] = payload.get("assumptions")
            if w.get("intent"):
                state["mandate_intent"] = w["intent"]
            w["intent"] = None
            w["mandate"] = self.projection(aggregate, constraints, payload["source_text"])
            for field in ("comparison", "graph", "approval"):
                w[field] = None
        elif method == "POST" and path.startswith("/v1/mandates/") and path.endswith("/confirm"):
            mid = path.split("/")[3]
            if not w["mandate"] or w["mandate"]["id"] != mid:
                raise MachineError("current mandate changed; review again")
            aggregate = mandates.confirm(context, mid, expected_version=payload["version"],
                expected_draft_hash=payload["hash"])
            w["mandate"] = self.projection(aggregate, w["mandate"]["constraints"], w["mandate"]["source_text"])
            state.setdefault("confirmed_review_inputs", {})[w["mandate"]["hash"]] = deepcopy({"assumptions": state.get("planning_assumptions"), "constraints": w["mandate"]["constraints"]})
        elif method == "POST" and path == "/v1/observations/refresh":
            if self.observations is None:
                raise MachineError("live observations are unavailable")
            observation = self.observations.read(context, (w.get("mandate") or {}).get("terms", {}).get("base_asset", "USDT"))
            w.update({k: observation[k] for k in ("balances", "snapshots")})
            state["observation"] = observation
        elif method == "POST" and path == "/v1/plan-comparisons":
            if not w["mandate"] or w["mandate"]["status"] != "CONFIRMED":
                raise MachineError("confirm your mandate before comparing plans")
            if payload["mandate_id"] != w["mandate"]["id"] or payload["mandate_hash"] != w["mandate"]["hash"]:
                raise MachineError("mandate changed; reload before comparing")
            if self.observations is None:
                raise MachineError("live observations are unavailable")
            workflow_comparison = w["mandate"]["terms"]["base_asset"] == "USDD" or (context.network == "tron-mainnet" and w["mandate"]["terms"]["base_asset"] == "TRX")
            native_staking = context.network=='tron-nile' and w['mandate']['terms']['base_asset']=='TRX' and w['mandate']['terms']['protocol_caps_bps'].get('tron-native',0)>0
            if native_staking:
                from .stake_comparison import compare as compare_stake
                record=mandates.get(context,payload['mandate_id'])['revisions'][-1]
                comparison,core=compare_stake(self,context,state,record)
                observation=state['observation']
            elif workflow_comparison:
                from .usdd_comparison import compare as compare_usdd
                comparison, core = compare_usdd(self, context, state)
                observation = {"snapshot": core.get("inputs"), "observed_at": self.clock()}
            else:
                if payload.get("snapshot_root"):
                    observation = state.get("observation")
                    if not observation or not observation.get("snapshot") or observation["snapshot"]["snapshot_hash"] != payload["snapshot_root"]:
                        raise MachineError("requested snapshot is not available in this workspace")
                else:
                    observation = self.observations.read(context, (w.get("mandate") or {}).get("terms", {}).get("base_asset", "USDT"))
                w.update({k: observation[k] for k in ("balances", "snapshots")})
                state["observation"] = observation
                aggregate = mandates.get(context, payload["mandate_id"])
                comparison, core = self.observations.compare(context, aggregate, observation, state.get("planning_assumptions"))
                if w["mandate"]["terms"]["borrowing"]["consent"]:
                    review = self.usdd.refresh(context, state)
                    comparison["usdd_vault"] = {"status": "UNAVAILABLE", "reason": review["reason"]}
            w["comparison"], state["comparison_core"] = comparison, core
            w["graph"], w["approval"] = None, None
            flows, model = [], None
            usage_id = (state.get("mandate_intent") or {}).get("usage_event_id")
            if usage_id:
                event = self.repository.get_record(context.scope, "MODEL_USAGE", "usage-" + usage_id)["body"]
                model = event["model_id"]
                flows.append({"name": event["flow"], "llm_calls": event["attempts"],
                    "input_tokens": event["input_tokens"], "output_tokens": event["output_tokens"], "latency_ms": event["latency_ms"]})
            w["evidence"].append({"id": "run-" + secrets.token_hex(12), "network": w["network"],
                "provenance": "SIMULATION", "mandate_hash": w["mandate"]["hash"],
                "snapshot_root": comparison["snapshot_root"], "plan_hash": None,
                "graph_hash": None, "approval_id": None, "txids": [],
                "outcome": "Live market observations, user-specified assumptions, deterministic " + comparison["status"] + ". No transaction submitted.",
                "model_id": model, "model_approval_evidence": None, "flows": flows,
                "energy": {"status": "UNAVAILABLE", "wh": None, "basis": "No physical energy measurement supplied by the provider."},
                "inputs": {"mandate": deepcopy(w["mandate"]), "comparison": deepcopy(comparison), "graph": None, "approval": None, "performance": None},
                "calculation": {"core": core, "snapshot": observation.get("snapshot"), "assumptions": state.get("planning_assumptions")}})
            w["evidence"] = w["evidence"][-20:]
        elif method == "POST" and path == "/v1/execution-graphs":
            from economic_machine.plan_compiler import compile_plan_intent
            from economic_machine.tron_sources import address_base58
            data = state.get("comparison_core")
            comparison = w.get("comparison")
            if not data or not comparison or not w["mandate"]:
                raise MachineError("a current confirmed comparison is required")
            if payload["mandate_hash"] != w["mandate"]["hash"] or payload["mandate_hash"] != comparison["mandate_hash"]:
                raise MachineError("mandate changed; compare again")
            plan = next((p for p in comparison["plans"] if p["id"] == payload["plan_id"] and p["hash"] == payload["plan_hash"]), None)
            if not plan:
                raise MachineError("selected plan differs from the reviewed comparison")
            if data.get("kind") == "USDD_WORKFLOW":
                selected = data.get("payloads", {}).get(plan["id"])
                if not selected or selected["hash"] != plan["hash"] or comparison["status"] != "READY" or datetime.fromisoformat(self.clock()) >= datetime.fromisoformat(comparison["expires_at"]):
                    raise MachineError("The executable USDD comparison expired or changed. Compare again.")
                self.usdd_execution.create(context, state, selected["payload"])
                state["requests"][key] = {"hash": request_hash, "response": result}
                self.commit(context, version, state)
                return result
            snapshot = state["observation"]["snapshot"]
            record = mandates.get(context, w["mandate"]["id"])["revisions"][-1]
            assembler=self.observations.assembler_for(context.network, record['mandate']['terms']['base_asset'])
            if data.get('kind')=='NATIVE_STAKE':
                from .stake_market import StakeAssembler
                assembler=StakeAssembler(assembler)
            intent = compile_plan_intent(data["comparison"], record, snapshot, data["request"],
                selected_plan=plan["id"], assembler=assembler,
                at=self.clock(), valid_until=comparison["expires_at"])
            if record["mandate"]["terms"]["base_asset"] == "TRX" and context.network == "tron-nile":
                if data.get('kind')=='NATIVE_STAKE':self.stake.create(context,state,plan,data)
                else:self.native.review(context, state, intent, plan, snapshot)
                state["plan_intent"] = intent
                state["requests"][key] = {"hash": request_hash, "response": result}
                if len(state["requests"]) > 256: state["requests"].pop(next(iter(state["requests"])))
                self.commit(context, version, state)
                return result
            steps = []
            for i, leg in enumerate(intent["legs"]):
                cap = snapshot["products"][leg["product_id"]]["capability"]
                steps.append({"id": "readiness-" + str(i+1), "title": "Verify " + leg["product_id"],
                    "action": "SUPPLY", "amount": {"value": leg["principal_amount"], "symbol": leg["principal_asset"], "decimals": cap["token"]["decimals"]},
                    "recipient": address_base58(cap["contract"]), "depends_on": [], "status": "BLOCKED", "txid": None,
                    "fee_cap": w["mandate"]["constraints"]["max_fee"], "allowance_remaining": None,
                    "error": "Live token balance, allowance, ABI/code binding, simulation and an execution quote must be verified before this step can request a signature."})
            graph = {"id": "review-" + intent["intent_hash"][:24], "plan_id": plan["id"], "plan_hash": plan["hash"], "mandate_hash": w["mandate"]["hash"], "network": w["network"], "account": address_base58(context.wallet), "expires_at": comparison["expires_at"], "enforcement_scope": "DIRECT_PROTOCOL", "steps": steps}
            graph["hash"] = digest(graph)
            w["graph"], w["approval"] = graph, None
            state["plan_intent"] = intent
        elif method == "POST" and path == "/v1/approvals":
            if state.get('stake_workflow') and (w.get('graph') or {}).get('id','').startswith('stake-'):
                self.stake.approve(context,state,payload)
            elif state.get('usdd_execution') and (w.get('graph') or {}).get('id','').startswith('usdd-'):
                self.usdd_execution.approve(context,state,payload)
            elif state.get("native_execution") and w.get("graph", {}).get("id", "").startswith("native-"):
                self.native.approve(context, state, payload)
            else:
                raise MachineError("Execution readiness is blocked. No transaction approval can be issued until every live verification passes.")
        elif path == "/v1/routines" and method == "POST" or path.startswith("/v1/routines/") and method == "PATCH":
            from .machine_worker import next_run
            if type(payload.get("enabled")) is not bool:
                raise MachineError("routine enabled must be boolean")
            due = next_run(self.clock(), payload["time"], payload["timezone"])
            allowed = {"Liquidity changes", "Plan expiry", "Transaction status", "Portfolio review"}
            if not isinstance(payload.get("notify_on"), list) or not set(payload["notify_on"]).issubset(allowed):
                raise MachineError("unsupported notification condition")
            if method == "POST":
                if len(w["routines"]) >= 12:
                    raise MachineError("maximum 12 routines per wallet and network")
                name = payload.get("name", "").strip()
                if not 1 <= len(name) <= 80:
                    raise MachineError("routine name must be 1 to 80 characters")
                routine = dict(id="routine-" + secrets.token_hex(12), name=name,
                    last_success_at=None, valid_until=None, worker_status="OFFLINE")
                w["routines"].append(routine)
            else:
                routine = next((r for r in w["routines"] if r["id"] == path.split("/")[-1]), None)
                if routine is None:
                    raise MachineError("routine not found in authenticated scope")
            routine.update({k: payload[k] for k in ("enabled", "time", "timezone", "notify_on")})
            routine["next_due_at"] = due
        else:
            raise MachineError("this operation requires verified execution capability and a current bound quote")
        state["requests"][key] = {"hash": request_hash, "response": result}
        if len(state["requests"]) > 256:
            state["requests"].pop(next(iter(state["requests"])))
        self.commit(context, version, state)
        return result


def router_for(bridge, gateway_secret):
    if not isinstance(gateway_secret, str) or len(gateway_secret) < 32:
        raise RuntimeError("Machine gateway secret must contain at least 32 characters")
    router = APIRouter()

    @router.api_route("/v1/machine/{route:path}", methods=["GET", "POST", "PATCH"])
    async def dispatch(route: str, request: Request):
        # This router is loopback-only at deployment. Authenticate even on loopback.
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, "Bearer " + gateway_secret):
            return JSONResponse(status_code=401, content={"error": {"code": "GATEWAY_AUTH_REQUIRED", "message": "Gateway authentication failed."}})
        try:
            payload = await request.json() if request.method != "GET" else {}
            network = request.query_params.get("network") if request.method == "GET" else payload.get("network")
            if network not in {"nile", "mainnet"}:
                raise MachineError("explicit supported network required")
            workspace = "workspace-" + request.headers.get("x-machine-workspace", "")
            ident(workspace, "workspace")
            wallet = request.headers.get("x-verified-wallet", "")
            if not wallet:
                if request.method == "GET" and route == "workspace":
                    return empty_workspace(network)
                return JSONResponse(status_code=401, content={"error": {"code": "WALLET_REQUIRED", "message": "Connect and verify your wallet first."}})
            at = utc(bridge.clock())
            context = AuthenticatedContext(tenant_id="machine", owner_id=workspace,
                wallet=address_hex(wallet), network="tron-" + network,
                session_id="gateway-" + workspace, trace_id="request-" + secrets.token_hex(12),
                issued_at=at, expires_at=(datetime.fromisoformat(at) + timedelta(minutes=5)).isoformat())
            from starlette.concurrency import run_in_threadpool
            if route.startswith("funding/"):
                if bridge.funding is None:
                    raise MachineError("Nile funding service is unavailable.")
                if request.method == "GET" and route == "funding/state":
                    return await run_in_threadpool(bridge.funding.state, context)
                if request.method == "POST":
                    if route == "funding/quote":
                        return await run_in_threadpool(bridge.funding.prepare, context, payload.get("amount"))
                    if route == "funding/submit":
                        return await run_in_threadpool(bridge.funding.submit, context, payload.get("signed_transaction", {}))
                    if route == "funding/reconcile":
                        return {"funding": await run_in_threadpool(bridge.funding.reconcile, context)}
                raise MachineError("Unknown Nile funding operation.")
            if request.method == "GET" and route == "workspace":
                return await run_in_threadpool(bridge.workspace, context)
            if request.method == "GET" and route == "evidence":
                return bridge.workspace(context)["evidence"]
            key = request.headers.get("idempotency-key", "")
            if not 1 <= len(key) <= 200:
                raise MachineError("bounded idempotency key required")
            usdd_pointer=route.split('/')[1] if route.startswith('execution-graphs/') else payload.get('graph_id','')
            if request.method == 'POST' and isinstance(usdd_pointer,str) and usdd_pointer.startswith('stake-'):
                if route.startswith('execution-graphs/') and route.endswith('/preflight'):
                    return await run_in_threadpool(bridge.stake.prepare,context,usdd_pointer,payload)
                if route=='executions':return await run_in_threadpool(bridge.stake.submit,context,payload)
                if route=='executions/reconcile':return await run_in_threadpool(bridge.stake.reconcile,context,payload)
            if request.method == 'POST' and isinstance(usdd_pointer,str) and usdd_pointer.startswith('usdd-'):
                if route.startswith('execution-graphs/') and route.endswith('/preflight'):
                    return await run_in_threadpool(bridge.usdd_execution.prepare,context,usdd_pointer,payload)
                if route=='executions':return await run_in_threadpool(bridge.usdd_execution.submit,context,payload)
                if route=='executions/reconcile':return await run_in_threadpool(bridge.usdd_execution.reconcile,context,payload)
            if request.method == "POST" and bridge.native:
                if route.startswith("execution-graphs/") and route.endswith("/preflight"):
                    return await run_in_threadpool(bridge.native.prepare, context, route.split("/")[1], payload)
                if route == "executions":
                    return await run_in_threadpool(bridge.native.submit, context, payload)
                if route == "executions/reconcile":
                    return await run_in_threadpool(bridge.native.reconcile, context, payload)
            # Blocking model/source calls use a thread so health/auth remain responsive.
            from starlette.concurrency import run_in_threadpool
            return await run_in_threadpool(bridge.mutate, context, request.method,
                "/v1/" + route, payload, key)
        except (MachineError, KeyError, TypeError, ValueError) as exc:
            message = str(exc) if isinstance(exc, MachineError) else "Request fields are missing or invalid."
            return JSONResponse(status_code=409, content={"error": {"code": "FINANCE_PRECONDITION", "message": message}})

    return router
