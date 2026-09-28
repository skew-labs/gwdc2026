"""Cherry-only offline replay of PR02 observations with explicit scenario inputs.

No HTTP, wallet reads, signing, worker loops or model calls. The mandate and
prices below are a synthetic calculation example, not a customer's mandate.
"""

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from economic_machine.mandate import draft_hash, normalize_mandate, policy_hash
from economic_machine.snapshot_assembly import SnapshotAssembler
from economic_machine.tron_cashflow import VERSION, calculate_tron_cashflows, rate, verify_tron_cashflows
from economic_machine.values import canonical
from economic_machine.vault_accounting import vault_cashflow
from economic_machine.yield_math import YEAR, period_yield


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--snapshot',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    if root != Path('/srv/skew/gwdc-financial-agent-20260924'):
        raise SystemExit('Run verification only in the authorized Cherry project')
    source, output=args.snapshot.resolve(),args.output.resolve()
    if not source.is_relative_to(root/'data/verification') or source.stat().st_size>16000000:
        raise SystemExit('Bounded prior project snapshot required')
    if not output.is_relative_to(root/'data/verification') or output.exists():
        raise SystemExit('Use a new project verification directory')
    snapshot=json.loads(source.read_text())
    assembler=SnapshotAssembler(json.loads((root/'config/tron_product_registry.json').read_text()))
    snapshot=assembler.verify(snapshot)
    at=snapshot['as_of']
    raw=json.loads((root/'cases/economic_mandate_demo.json').read_text())
    raw['scope']['network']=snapshot['network']
    terms=raw['terms']
    terms.update(effective_at=at,expires_at=(datetime.fromisoformat(at)+timedelta(hours=1)).isoformat(),
                 withdrawals=[],price_exposure_caps_bps={'TRX':5000,'USDD':5000},
                 protocol_caps_bps={'justlend':7000,'usdd':5000,'tron-native':5000})
    terms['borrowing'].update(consent=True,max_debt={'asset':'USDT','amount':'2000'})
    terms['allowed_actions']+=['STAKE','MINT_USDD']
    text='SYNTHETIC_VERIFICATION_CONDITIONS '+json.dumps(terms,sort_keys=True)
    raw['source_messages']=[{'message_id':'synthetic-verification','text':text}]
    raw['source_refs']={key:[{'message_id':'synthetic-verification','quote':text}] for key in terms}
    record={'mandate':normalize_mandate(raw),'status':'CONFIRMED',
            'draft_hash':draft_hash(raw),'policy_hash':policy_hash(raw)}
    quotes=[]
    for name in ('justlend.v1.jUSDT','justlend.v1.jUSDD','justlend.strx','tron.native.stake','usdd.vault.TRX-A'):
        q=dict(product_id=name,principal='1000',prices_base={'USDT':'1','USDD':'0.99','TRX':'0.25'},
               costs_base=dict(entry='1',exit='1',conversion='0',network='1'),redemption_seconds=30,
               reward_claim_seconds=0,reward_haircut_bps=2500,exit_tranches=None,native=None,vault=None,resources=None)
        if name=='justlend.strx':
            q['exit_tranches']=[{'amount':'1000','after_seconds':None}]
        if name=='tron.native.stake':
            q['native']=dict(voting_rate=rate('0.04','SIMPLE_APR'),lock_remaining_seconds=3600,
                             resource_recovery_seconds=0,window=None,rental=None)
        if name.startswith('usdd.vault.'):
            q['principal']='16000'
            q['vault']=dict(deployed_usdd='1000',stored_debt_usdd='1000',fee_age_seconds=0,
                fee_convention='EFFECTIVE_APY',destination_id='justlend.v1.jUSDD',
                destination_redemption_seconds=30,collateral_release_seconds=10,
                shocks=[dict(name='fall',collateral_change_bps=-100,usdd_change_bps=100,deployment_loss_bps=500)])
        quotes.append(q)
    assumptions=dict(schema_version=VERSION,snapshot_hash=snapshot['snapshot_hash'],quotes=quotes)
    result=calculate_tron_cashflows(record,snapshot,assumptions,assembler=assembler,at=at)
    assert verify_tron_cashflows(result,record,snapshot,assumptions,assembler=assembler,at=at)
    vectors={'apr_half_year':period_yield('1000',rate('0.21','SIMPLE_APR'),YEAR//2),
             'apy_half_year':period_yield('1000',rate('0.21'),YEAR//2),
             'negative_apy_half_year':period_yield('1000',rate('-0.19'),YEAR//2),
             'vault':vault_cashflow(collateral_base='1000',deployed_usdd='400',stored_debt_usdd='400',
                fee_rate=rate('0.1','SIMPLE_APR'),fee_age_seconds=0,horizon_seconds=YEAR,
                destination_rate=rate('0.2','SIMPLE_APR'),usdd_price_base='1',min_ratio='1.5',buffer_bps=0,
                shocks=[dict(name='fall',collateral_change_bps=-1000,usdd_change_bps=0,deployment_loss_bps=0)])}
    output.mkdir(parents=True)
    for name, value in [('conditions.json',record),('assumptions.json',assumptions),
                        ('calculation.json',result),('arithmetic-vectors.json',vectors)]:
        (output/name).write_bytes(canonical(value))
    summary={'verified_at':datetime.now(timezone.utc).isoformat(),'source_snapshot_as_of':at,
             'source_snapshot_hash':snapshot['snapshot_hash'],'calculation_hash':result['calculation_hash'],
             'evidence_kind':'HISTORICAL_PUBLIC_CAPTURE_REPLAY_WITH_SYNTHETIC_CONDITIONS',
             'execution_authority':'NONE','quotes':[{key:q[key] for key in ('product_id','status','reason_codes')}
                | {'net_income_base':q.get('accounting',{}).get('net_income_base')} for q in result['quotes']]}
    (output/'replay-summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':
    main()
