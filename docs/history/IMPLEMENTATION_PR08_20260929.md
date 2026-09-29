> Historical record. Current product, setup and verified scope: [faat README](../../README.md).

# PR 08 — 동일 txid 제출 상태, 실제 포지션 대조, 성과 원장

2026-09-29. PR 07에서 검증한 한 개의 서명 payload를 새 거래로 다시 만들지 않고 제출·조회·solidified 실행·post-state·성과로 이어가는 결정론적 코어를 구현한다. 이번 변경은 네트워크 broadcast adapter를 호출하거나 고객 서명·자산 이동을 수행하지 않는다. 모든 node/계정/가격 입력은 replay evidence다.

## 제출과 확정 상태

```text
SIGNED_PAYLOAD_VERIFIED
  -> READY_TO_SUBMIT
  -> BEFORE_SEND_FAILURE | AFTER_SEND_UNKNOWN | NODE_RESPONSE
  -> 같은 raw_data_hex / signature / txid만 재처리
  -> solidified body + receipt + inclusion block
  -> SOLID_EXECUTED_PENDING_POST_STATE
  -> exact post-state evidence
  -> RECONCILED | DISPUTED
```

`tron_execution.py`는 PR 07의 결과 hash만 믿지 않는다. 포함된 원본 `WalletSignatureRequestV1`을 다시 검사하고, `raw_data_hex`의 SHA-256, canonical protobuf, low-s secp256k1 signer, owner/target/data/fee, plan/graph/step/action/request 연결을 제출 준비 시 다시 확인한다. 전송 timeout은 `SUBMISSION_UNKNOWN`, 노드의 accepted 응답은 `NODE_ACCEPTED_UNCONFIRMED`, reject 응답도 체인 부재가 입증되지 않은 `NODE_REJECTED_UNCONFIRMED_ABSENCE`다.

재시도는 같은 서명 bytes와 같은 txid만 반환한다. expiration 이후에는 broadcast 준비를 거절하고, `NOT_OBSERVED`만으로 원 거래가 없었다고 판정하거나 replacement transaction을 만들지 않는다. TRON 공식 문서도 broadcast의 `result: true`를 포함·실행·solidification 증거로 보지 않고 원 txid를 계속 조회하도록 요구한다. 기준은 [Sign and broadcast workflow](https://developers.tron.network/docs/api-signature-and-broadcast-flow)와 [Transactions](https://developers.tron.network/docs/tron-protocol-transaction)다.

기존 `tron_consumption_read.py`는 v2 observation에서 solidified receipt의 total fee, Energy fee/usage, Bandwidth fee/usage를 문자열 SUN 단위로 보존한다. 원천에 필드가 없으면 `null`이며 0으로 바꾸지 않는다. 저장된 v1 observation은 읽기 호환을 유지한다.

## 실행 그래프와 실패

`execution_ledger.py`는 PR 06의 `ORDERED_MULTI_TRANSACTION_NON_ATOMIC` 의미를 그대로 사용한다. 원장 시작에는 scope·graph hash·모든 off-chain reservation step hash·잠금 유효시간·원천 hash를 묶은 `CapitalReservationEvidenceV1`이 필요하다. `reservation_id` 문자열만으로 예약을 성공 처리하지 않는다. 각 단계는 `PENDING → SUBMISSION_UNCERTAIN → EXECUTED_PENDING_RECONCILIATION → RECONCILED`로 진행한다.

- failed solidified receipt 또는 post-state 불일치는 graph를 `HALTED`로 만들고 뒤 단계를 차단한다.
- approve 단계 대조는 allowance만 증명하며 공급 성공이나 자본 잠금 해제를 만들지 않는다.
- terminal 금융 단계와 실제 post-state가 모두 맞아야 `RELEASE_ELIGIBLE`이다. 실제 잠금 해제 I/O는 아직 없다.
- 완료 후 reorg evidence가 원 txid·receipt hash·block number/id·reconciliation hash와 일치하면 다시 `LOCKED/HALTED`로 전환한다.
- timeout, partial/mismatch, reorg에서 새 거래를 자동 생성하지 않는다.

## 실제 포지션 대조

`position_reconciliation.py`는 approval 당시 fresh account hash와 solidified execution을 새 `PostStateEvidenceV1`에 연결한다. post-state는 같은 wallet/network/txid, receipt block부터 최대 64 block 이내, 완전한 required field read, source hash를 요구한다. 누락 position은 0으로 해석하지 않는다.

현재 검증 가능한 동작은 다음과 같다.

- `TRC20_APPROVE`: exact allowance. 이것만으로 deposit을 주장하지 않는다.
- `JUSTLEND_SUPPLY`: 입력 TRC20 잔액의 exact 감소와 최소 share 증가.
- `JUSTLEND_REDEEM_SHARES`: exact share 감소와 최소 underlying 잔액 증가.
- `JUSTLEND_REDEEM_UNDERLYING`: underlying position 감소와 최소 잔액 증가.
- `STRX_STAKE`: 최소 share 증가와 TRX 감소. 실제 fee는 별도 receipt 원장 항목이다.
- Vault repay/withdraw: 같은 vault id의 debt/collateral 감소 의미를 구현했지만 PR 06에서 exact proxy/join/exit calldata가 차단되어 live 실행 가능 상태가 아니다.

sTRX/TRON delayed queue·claim, native Stake 2.0 system transaction protobuf, Vault live proxy는 아직 post-state adapter가 없다. 성공 receipt만 있거나 지분이 없거나 input 잔액이 줄지 않으면 `DISPUTED`이며 자본은 잠긴다.

## 실제 성과와 forecast 비교

`performance.py`는 모든 값을 base-asset 최소 단위 정수로 계산한다.

```text
investment PnL
  = closing NAV - opening NAV - deposits + withdrawals

attributed PnL
  = interest + rewards + price PnL + realized PnL
    - network fee - protocol fee - debt cost
```

두 값이 정확히 같지 않으면 원장을 만들지 않는다. 입금은 수익이 아니고, 출금은 손실이 아니며, 부채 원금 감소는 balance-sheet movement다. solidified TRON receipt의 SUN fee는 TRX 가격 evidence에 묶어 base 단위로 올림 변환한다. 예상 수익은 같은 wallet/network/기간/base asset의 실제 원장과만 비교하며 `ASSUMPTION_NOT_GROUND_TRUTH`를 유지한다.

## 검증 범위

Cherry Servers의 exact branch에서 변경 코드와 직접 연결만 검사한다.

- PR 07 signed protobuf 검증 5개
- 신규 submission/reader 연결 9개
- 신규 post-state 대조 6개
- 신규 성과 원장 6개
- 신규 non-atomic graph 진행·실패·reorg·예약 증거 5개
- 수정한 solidified reader fee 추출 1개

총 32개다. 기존 reader 전체 suite와 그 밖의 이미 통과한 suite는 반복하지 않는다. 재현 출력은 `artifacts/pr08/targeted-tests.log`, 파일 hash와 의미 줄 수는 `artifacts/pr08/source-manifest.json`, 경계 요약은 `artifacts/pr08/verification-summary.json`에 보존한다.

## 남은 경계

실제 broadcast port, 영구 DB/outbox, worker restart 복구, chain provider 인증 정책, live post-state reader, 모든 프로토콜 event decoder, Vault/queue adapter, 장기간 PnL valuation, 회계 감사는 아직 없다. 이 코드는 replay에서 상태 전이를 증명하지만 실제 거래 성공을 만들지 않는다. 고객 서명 0건, broadcast 0건, 배포 0건, 자산 이동 0건이다. PR 09가 이 record들을 tenant별 저장소·outbox·worker lease·감시 작업에 연결한다.
