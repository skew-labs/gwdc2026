# PR 07 — 사용자 승인, 서명 payload 검증, 제한 실행 Guard

2026-09-29. PR 06의 `ExecutionGraphV1`과 `PreflightManifestV1`을 한 사용자·네트워크·계정 스냅샷·단계·금액·최소 출력·수수료·만료에 묶는다. 승인, 지갑 서명 요청, 서명된 payload 검증은 서로 다른 상태다. 이 PR은 고객 서명, broadcast, 배포, 체인 실행을 수행하지 않는다.

## 승인과 서명 경계

```text
READY preflight step
  -> ApprovalV1 / PENDING
  -> 사용자의 명시적 approval confirmation
  -> APPROVED_UNSIGNED
  -> 최근 TRON reference block에 묶인 WalletSignatureRequestV1
  -> 지갑이 반환한 raw_data_hex + signature
  -> 독립 protobuf decode + txID + signer recovery + exact field compare
  -> SIGNED_PAYLOAD_VERIFIED / NOT_SUBMITTED
```

`ApprovalV1`은 policy scope, plan/graph/step/action, 원본 snapshot, fresh account, preflight manifest, 입력·최소 출력·recipient·fee limit·nonce·TTL을 함께 commit한다. 지갑 연결은 승인도 서명도 아니며, 조건 확인과 계획 선택도 거래 승인이 아니다. 세션, 인증 context, wallet, network, 만료가 바뀌면 기존 카드는 사용할 수 없다.

직접 지갑 경로는 고정 ABI call 전체를 승인한다. Guard 경로는 서버가 관리하는 만료형 trust policy에 등록된 guard 주소·adapter 주소·code hash·owner·market·token·evidence 조합만 허용한다. 여기에 **Guard 자체를 실행한 simulation**이 exact envelope, 최소 출력, 사용자 잔액 변화, fee cap, allowance reset, Guard/Adapter 잔액 0을 증명해야 카드를 만들 수 있다. PR 06에서 시장 call만 simulation한 결과를 Guard 실행 성공으로 재사용하지 않는다. 현재 실제 배포 registry entry는 없으므로 테스트 fixture 밖의 Guard 경로는 활성 상태가 아니다.

## 지갑 반환값 독립 검증

`signed_tx_validation.py`는 지갑 JSON의 해석 결과를 신뢰하지 않는다. bounded protobuf parser가 `raw_data_hex`에서 TAPOS, timestamp, expiration, fee limit, 단일 `TriggerSmartContract`, permission, owner, target, call value와 calldata를 다시 읽는다. 그 bytes의 SHA-256을 `txID`와 대조하고 65-byte canonical low-s secp256k1 signature에서 TRON 주소를 복구한다. 요청과 필드 하나라도 다르거나 approval/request/txid가 사용 기록에 있으면 거절한다.

TRON 공식 문서는 `txID = SHA-256(raw_data)`, 65-byte secp256k1 서명, TAPOS block number `[6,8)`와 block ID `[8,16)`을 규정한다. 또한 broadcast 응답은 확정이나 실행 성공 증거가 아니라고 구분한다. 구현 기준은 [Transactions](https://developers.tron.network/docs/tron-protocol-transaction)와 [Sign and broadcast workflow](https://developers.tron.network/docs/api-signature-and-broadcast-flow)다.

현재 검증기는 한 개의 owner key와 `permission_id=0`만 지원한다. 다중서명·account permission threshold는 안전하게 지원하기 전까지 거절한다. 검증 결과도 `EXACT_SIGNED_PAYLOAD_ONLY / NOT_SUBMITTED`이며 전송 권한이나 체인 성공으로 승격하지 않는다.

## 고정 JustLend 실행 경로

`EconomicExecutionGuardV1`은 owner와 adapter를 constructor에서 고정하고 adapter, underlying, market/share token의 배포 code hash를 실행마다 확인한다. supply/redeem에는 다음 제한을 둔다.

- owner만 exact calldata를 호출하며 arbitrary target, arbitrary recipient, fallback, payable, upgrade, withdrawal, relayer signature path가 없다.
- plan/graph/step hash, amount, minimum output, max fee, 순차 nonce, deadline이 calldata에 포함된다.
- supply 단건·누적 한도와 redeem share 한도를 검사한다.
- token transfer의 실제 balance delta, 실제 position share/underlying delta를 검사한다.
- Guard와 adapter는 실행 전후 잔액 0, allowance는 사용 뒤 0을 요구한다.
- false-return token, fee-on-transfer token, nonzero JustLend protocol code, 부족 출력, 호출 성공이지만 지분 미발행, 잔류 잔액, 재진입을 모두 rollback한다.

adapter는 공식 JustLend V1 `mint(uint256)` / `redeem(uint256)`의 uint error-code `0` 의미를 사용한다. ABI 이름은 [JustLend 공식 MCP ABI catalogue](https://github.com/justlend/mcp-server-justlend/blob/main/src/core/abis.ts)를 기준으로 고정했다. 허용 adapter registry나 실제 code binding evidence가 아직 없으므로 계약 소스가 존재한다는 사실만으로 고객 경로가 활성화되지 않는다.

`maxFeeSun`은 signed calldata와 approval commitment에 포함되지만 계약이 트랜잭션의 실제 TRON fee limit를 읽을 수는 없다. 그래서 wallet-returned protobuf의 `fee_limit`을 같은 값으로 독립 검증한다. 온체인 계약 한 계층만으로 이 제한을 증명한다고 주장하지 않는다.

## 컴파일과 실행 검증

Cherry Servers의 격리 worktree에서 신규/직접 연결 범위만 검증했다.

- 공식 `tronprotocol/solc-bin`의 `solc.tron 0.8.20+commit.5f1834bc`를 SHA-256과 GPG 서명으로 확인했다. repository는 [TRON solc-bin](https://github.com/tronprotocol/solc-bin)이다.
- pinned TRON compiler로 두 deployable artifact의 exact ABI, constructor, event, 금지 primitive, payable/fallback 부재, bytecode size를 검사했다.
- 별도 pinned upstream EVM solc와 PyEVM 격리 환경에서 공급·상환·owner/nonce/TTL/한도·재사용·protocol error·false return·fee token·부족 position·잔류 잔액·재진입·allowance reset을 실행했다.
- Python 경계 테스트는 승인 분리, 서버 trust policy allowlist, Guard simulation, TAPOS, raw protobuf, txID, low-s signer, amount/owner/target/fee/deadline/data 변조, 다른 wallet/network/session, replay를 검사했다.

격리 EVM 통과는 TVM 실행 증거가 아니다. 공식 TRON compiler 호환은 확인했지만 실제 TVM 실행은 `NOT_TESTED_ON_TVM`, 배포는 `NONE`, 외부 감사는 `NONE`으로 남긴다. 재현 명령과 출력은 `artifacts/pr07/targeted-tests.log`, compiler 결과는 `artifacts/pr07/tron-compiler-manifest.json`, 도구 공급망 정보는 `artifacts/pr07/toolchain-manifest.json`에 보존한다.

## 아직 실행되지 않는 것

TronLink/browser adapter 구현, live RPC transaction builder/simulation, 검증된 배포 code/address binding 발급, 다중서명 permission 검증, TVM runtime test, 계약 배포, 고객 서명, broadcast, solidified receipt와 실제 position 대조는 아직 없다. 제출·확정·실제 포지션·부채·성과 대조는 PR 08의 책임이다. 따라서 이 PR이 만드는 최종 상태는 서명 payload의 정합성 검증까지이며 자산 예치 성공이 아니다.
