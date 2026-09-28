# PR 06 — TRON 거래 DAG와 fail-closed preflight

2026-09-28. PR 04의 `PlanIntentV1`을 잔액 예약, 필요 자산 전환, TRC20 승인, 공급·스테이킹, 지연 회수·claim, Vault 상환으로 이어지는 **서명 전 거래 그래프**로 컴파일한다. 결과는 항상 `ORDERED_MULTI_TRANSACTION_NON_ATOMIC`이며 고객 서명, transaction build, broadcast, 체인 실행 권한을 만들지 않는다.

## 책임 경계

```text
PR04 exact PlanIntentV1
  -> account balance/reservation snapshot
  -> intent/snapshot/wallet에 묶인 실행 quote
  -> 필요하면 ABI·code·capacity 증거가 있는 funding route
  -> ExecutionGraphV1
  -> graph 생성 이후의 fresh account snapshot
  -> 단계별 simulation 및 protocol return 검증
  -> PreflightManifestV1 (unsigned, expected state only)
```

`ExecutionGraphV1`은 입력·예상 출력·fee limit·TTL·postcondition·의존 단계와 모든 commitment를 보존한다. 전환 단계의 예상 출력은 `DEPENDENCY_OUTPUT_UNCONFIRMED`이며 wallet 잔액에 더하지 않는다. approve가 simulation에 성공해도 해당 거래가 확정되고 allowance를 다시 관측하기 전에는 supply가 실행 가능해지지 않는다.

## 고정 TRON action

`tron_actions.py`는 다음 동작의 ABI나 TRON system-contract shape만 고정한다.

- TRC20 `approve(address,uint256)`은 `true` 반환을 요구한다.
- JustLend `mint(uint256)`, `redeem(uint256)`, `redeemUnderlying(uint256)`은 프로토콜 오류 코드 `0`을 요구한다.
- sTRX `deposit()`, `withdraw(uint256)`, `claimAll()`은 TRX 6 decimals 입력과 sTRX 18 decimals 출력을 구분한다.
- TRON Stake 2.0의 freeze/unfreeze/delegate/undelegate/withdraw-expired는 smart-contract calldata로 위장하지 않고 native system operation으로 표현한다.
- USDD Vault open/add/mint/repay/withdraw lifecycle은 보존하지만 정확한 user proxy, join/exit, vault id, collateral/debt delta가 없으면 `UNVERIFIED_COMPOSITE`로 차단한다.

공식 ABI 이름은 [JustLend MCP ABI catalogue](https://github.com/justlend/mcp-server-justlend/blob/main/src/core/abis.ts), sTRX와 Stake 2.0 호출 흐름은 [JustLend sTRX service](https://github.com/justlend/mcp-server-justlend/blob/main/src/core/services/strx-staking.ts)와 [staking service](https://github.com/justlend/mcp-server-justlend/blob/main/src/core/services/staking.ts), native resource 동작은 [TRON Account Resources API](https://developers.tron.network/reference/account-resources)를 기준으로 했다. USDD `frob`은 [Vat 문서](https://docs.usdd.io/developers/core-contracts/vat)에 존재하지만, [Proxy](https://docs.usdd.io/developers/core-contracts/proxy-contract)와 [배포 주소](https://docs.usdd.io/developers/deployment-addresses)를 함께 바인딩하지 않은 단독 `frob`을 고객 실행 경로로 만들지 않았다.

ABI를 안다는 사실과 배포 계약을 확인했다는 사실은 다르다. `VERIFIED` action은 product capability hash, ABI evidence hash, contract code hash, network, target이 모두 일치해야 한다. 다른 상품 capability의 binding, route quote replay, 다른 wallet/snapshot/intent의 funding route는 거부한다.

## Preflight

preflight는 graph 생성 이후에 관측한 동일 scope의 fresh account snapshot만 받는다. 각 on-chain 단계는 graph의 exact `action_hash`에 묶인 simulation evidence가 있어야 하며 다음을 독립적으로 검사한다.

- RPC 성공과 contract return 성공을 분리한다. `REVERTED`, `RPC_ERROR`, JustLend nonzero 오류 코드, TRC20 `false`를 서로 다른 reason으로 남긴다.
- simulation fee가 step fee limit와 graph budget을 넘는지 검사한다.
- projected output이 minimum보다 작은지, postcondition이 모두 증명됐는지 검사한다.
- 시장 유동성, position share/underlying, Vault collateral/debt, USDD 상환 잔액을 fresh snapshot으로 다시 검사한다.
- unstake와 claim을 별도 단계로 두며 claim 시각과 앞 거래의 확정·재관측을 요구한다.
- 결과는 `EXPECTED_ONLY_UNCONFIRMED`다. constant call 성공을 실제 지분·부채·잔액 변화로 승격하지 않는다.

기존 `EconomicCapitalVault` batch는 단일 입력 자산의 2–8개 order를 한 atomic call로 처리하는 별도 경계다. PR 06의 순차 TRON graph는 변환·approve·지연 claim을 포함하므로 `assess_execution_graph_compatibility`가 항상 비호환으로 반환하고 자동 변환하지 않는다.

## 검증 범위

사용자 요청대로 이미 통과한 전체 suite를 반복하지 않았다. Cherry Servers `999573 / charmed-weasel`의 publish worktree에서 신규 코드와 직접 연결만 검사했다.

- fixed ABI와 native/smart transport, uint/int precision, unverified Vault 차단
- PR 04 실제 `PlanService` intent → PR 06 graph replay 연결
- `ProductCapabilityV1` hash/stage와 action ABI/code binding 연결
- 1,000 USDD 부족, verified route가 있을 때만 전환, route replay 거부
- approve 성공/supply revert, false/nonzero protocol return, fee 급등
- expected route output을 observed balance로 사용하지 않는 경계
- sTRX TRX→sTRX 단위, delayed unstake→claim dependency
- redeem 시 fresh liquidity 감소, USDD repay balance 부족
- 기존 atomic vault batch와의 명시적 비호환

최종 결과와 명령은 `artifacts/pr06/targeted-tests.log`, 파일 hash와 의미 줄 수는 `artifacts/pr06/source-manifest.json`, 검증 요약은 `artifacts/pr06/verification-summary.json`에 보존한다.

## 아직 실행되지 않는 것

현재 product registry는 `execution_enabled=false`이고 실제 상품 capability의 `simulate/execute`가 `SUPPORTED`가 아니다. 그러므로 fixture에서 verified binding을 주는 테스트와 달리 현재 live 상품 graph는 차단된다. live RPC simulation adapter, contract code 조회와 신뢰 가능한 ABI evidence 발급, PSM/DEX route provider, Energy/Bandwidth 실시간 산정, exact USDD proxy/join/exit compiler는 아직 없다.

PR 07 전에는 사용자 승인·TronLink 서명·signed payload 검증이 없고, PR 08 전에는 broadcast·확정 receipt·실제 지분/부채/잔액 대조가 없다. 이번 PR은 고객 서명, 계약 배포, 체인 전송, 실자산 거래, 예약 작업을 수행하지 않았다.
