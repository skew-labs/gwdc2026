> Historical record. Current product, setup and verified scope: [faat README](../../README.md).

# Economic Machine — 실행 코어 v0.28

> 제품 배치 변경(2026-09-28): 이 문서의 코드·검증 결과는 유지한다. 고객 소유 노드 제품 방향은 사용자 지시로 제외되었으며 코어는 [Qwen3 32B 통합 웹서비스](TRON_FURIOSA_UNIFIED_ARCHITECTURE_20260928.md)의 서버 내부 모듈로 재사용한다. 아래 고객 노드 표현은 기존 코드 설계 맥락이고 신규 BYON 구현 지시가 아니다.

2026-09-28. 이 구현은 사용자가 제시한 **Economic CPU / ISA / State / Invariant Kernel / Transition / Receipt / Capital Sandbox / 외부 Settlement Bus**를 데이터셋 수집기와 분리한 참조 실행계다. 고객 소유 노드에서 동작하도록 Python 경제 코어는 표준 라이브러리만 사용하고, 서명 검증의 낮은 빈도 경로에서 해시 고정된 OpenSSL 3.0 실행 파일을 별도로 요구한다. Cherry 999573은 코드/테스트를 돌린 사용자 제공 노드이며 공용 고객 실행 서버가 아니다. 자연어 모델 호출·시장 데이터 학습 수집기와 결합하지 않았다. 모델 출력 심사, 버전 있는 ISA·적합성 벡터, 타입이 있는 SPOT/PERP/LENDING 상품 입력과 고정 공개키 서명 검증, 제한된 격자 최적화, 배분안의 비실행 commitment, 서명된 원본을 보존하는 v2 commitment·체인 바인딩, 타입이 있는 TRON 레지스트리 바인딩, 지정 대상의 1회 소비, 정책별 검증자 증명 요구와 키 epoch, 정책별 및 동일 자산의 정책 간 basket 예약·누적 한도와 고객 노드의 읽기 전용 레지스트리·거래 소비 관측 경계가 있다.

## 지금 실제로 실행되는 경계

```text
LLM의 비신뢰 추론 초안 ─→ InferenceScope + 현재 State Root 검증
                          ├→ REJECTED / ABSTAIN
                          └→ REVIEW_REQUIRED (자동 등록·실행 없음)
                                       ↓ 별도 로컬 등록
검토된 EconomicProgram JSON
  → compile_program: 타입·순서·권한·ISA 정적 분석 / program_hash
  → EconomicRuntime: 고객 노드의 프로그램 버전·StateDelta·이벤트 저널
  → EconomicKernel: OBSERVE/PRICE/ASSERT/GUARD/BRANCH/QUOTE/SCORE/ALLOCATE
                   → SIMULATE → VERIFY → PREPARE → SETTLE gate
  → Capital Sandbox: 동일 자산/직원의 미결 의도와 실행 전 잠금까지 검사
  → DecisionReceipt: 상태·정책·예약·명령 결과의 결정적 hash
  → 선택적 EXECUTION_LOCKED 자본 동결
  → 외부 검증 증거: AUTHORIZE → SUBMIT → FINALIZE → RECONCILE
  → 확인된 계정 상태가 기대값과 같을 때만 잠금 해제.
  → 검증된 실패·부분체결·확정 취소: DISPUTED → 새 판단 전역 중지.
    고객 노드 서명기·chain adapter 없음. 별도 미배포 금고 소스는
    소유자 서명·배분 증명·입출력 잔액 조건으로 자본 이동을 제한.

ESCALATED receipt → 중복 없는 예외 요청함 → 외부 해석자에게 노출
                                     (런타임의 LLM 호출 0)
```

`src/economic_machine/`는 `finagent` 또는 `research/fdc`를 import하지 않는다. 입력은 caller가 가져온 typed `EconomicState`/`StateDelta`다. 외부 값의 진실성은 caller와 향후 Data Oracle adapter 책임이며, SHA-256 source hash만으로 진실이나 독립성을 증명하지 않는다. `cases/economic_*_demo.json`은 가짜 `economic-testnet-sim` 세계다. JustLend 입금 또는 TRON 테스트넷 거래라고 해석하지 않는다.

| 코드 | 현재 의미 |
| --- | --- |
| `spec.py`, `cases/economic_isa_vectors_v1.json` | 명령의 필수 필드·허용 ISA 버전·순서·상태 전이·미구현 예약 상태·한도를 하나의 JSON 호환 명세에 둔다. 시작할 때 명세 자체를 검사하고, CLI `spec`으로 정규화된 명세와 SHA-256을 내보낸다. `spec-vectors`는 모든 구현 명령과 모든 상태 단계의 195개 허용/거부 벡터를 생성한다. 저장된 벡터와 현재 명세가 달라지면 테스트가 실패한다. 새로운 ISA 의미는 새 버전과 새 벡터를 요구한다. |
| `values.py`, `state.py` | 정확한 십진 문자열, canonical JSON/hash, 소유자·네트워크·시퀀스·원천 증거가 있는 경제 상태. 동일 값의 새 관측은 상태를 갱신하지만 정책 재평가 0. 잔액/노출/일일 손실 변화는 해당 프로그램으로 라우팅. |
| `compiler.py` | `econ-isa-1/2/3` 정적 컴파일. 필수 필드·버전·순서·한도는 명세에서 읽고, 의미 검사는 별도로 수행한다. v2 `GUARD`는 거짓일 때 종료하고, v3 `BRANCH`는 서로 다른 배분/보유 경로로 이동한다. v3는 최대 64명령·8분기·256실행 경로, 앞으로만 향하는 정수 인덱스와 모든 도달 가능한 경로의 SSA 레지스터·명령 순서·phase 전이·필수 검증 단계를 검사한다. 뒤로 이동, 도달 불가 명령, 검증 누락 경로는 컴파일 거부. `HEDGE/SWAP/BORROW/REPAY/CANCEL`은 계속 예약 명령이다. |
| `inference.py` | Qwen 등 모델이 제안한 `EconomicProgram`은 비신뢰 초안이다. 요청 해시·상태 root·모델 식별자·시점·정확한 confidence 형식·모호성·로컬 범위의 자본/프로토콜/관측 경로를 검사한다. 유효해도 `REVIEW_REQUIRED`로만 분류하고 프로그램을 자동 등록하지 않는다. 정보 부족은 `ABSTAIN`, 불일치·컴파일 실패는 `REJECTED`. 모델 호출은 여기에도 없다. |
| `portfolio.py` | v1의 비음수 상품 손실 평가를 유지한다. v2는 같은 시나리오의 **부호 있는 상품 손익**을 합산해 헤지의 이익과 손실을 함께 계산하고, 상품이 속한 그룹과 별도로 공통 위험요인의 순·총 노출 한도를 검사한다. 상품 관측 시점 간 최대 차이도 제한한다. 현금·회전율·상품/그룹 한도·유동성·최악 시나리오 손실을 통과한 **주어진 후보**만 결정적으로 순위를 매긴다. 이 계산은 제공된 시나리오/적재치 안에서만 유효하며 외부 위험 모델·수익 예측·체결 권한을 만들지 않는다. |
| `product_adapter.py` | SPOT·PERP·LENDING의 상품 명세, 시장 관측, 위험 모델 가정, 현재 보유비중을 별도 입력으로 받는다. 기초/호가/명목 금액 단위·시각·유효기간·시장 활성 상태·출금 가능 여부를 검사한다. SPOT spread는 보수적으로 올림하고, PERP mark/index basis를 제한하며, LENDING 상환 지연·왕복 수수료를 반영한다. 관측된 명목 수용량을 자본 대비 최대 비중으로 환산하고 위험 가정을 관측 해시에 묶어 v2 배분 요청·판정과 재생 가능한 출처 명세를 만든다. API 수집·외부 서명 검증은 없으므로 `CLAIMED_NOT_VERIFIED`이며 자동 승인/실행도 없다. |
| `signed_evidence.py` | 고객 노드 운영자가 지정한 OpenSSL 실행 파일의 SHA-256, 상품 명세 해시, 역할별 Ed25519 공개키와 유효기간을 고정한다. 시장 관측마다 서로 다른 키 2개 이상, 모델/보유비중은 각각 1개 이상의 서명을 canonical JSON의 도메인·역할·발급자·서명시각·payload에 대해 실제 검증한다. 중복 키·중복 서명·역할 바꿔치기·시각/서명/해시 불일치는 거부한다. 서명된 원본을 `product_adapter.py`와 v2 optimizer로 연결하되 결과는 `PINNED_SIGNATURES_NOT_ECONOMIC_TRUTH`·`execution_authority=NONE`이다. private key·거래 전송 경로가 없다. |
| `grid_search.py` | v1과 v2에서 정해진 bps 간격의 현금 포함 모든 비중 조합을 열거해 `portfolio.py`로 평가한다. 조합 수를 먼저 계산하고 4,096개를 초과하면 부분 탐색을 하지 않고 거절한다. `complete_enumeration`은 **정해진 이산 격자와 제공된 수익·위험 가정 안에서만** 최상위 후보를 찾았다는 뜻이다. 연속 공간·미래 수익률·시나리오 밖 손실에 대한 최적성은 아니다. |
| `tron_yield.py` | Challenge B용 TRON 수익 기회 7종을 타입별로 정규화한다. Energy 임대 견적·언스테이킹 대기, JustLend 수익/인센티브, sTRX 집계 수익의 중복, USDD Vault의 담보·발행 부채·안정화 수수료·청산 완충비율을 검사한다. USDD Vault는 필수 비교 대상으로 요구한다. 보수·성장 배분안과 가능하면 Vault 포함 비교안을 같은 위험/출금 제약 안에서 계산한다. 외부 입력은 서명 없는 주장이고, 결과는 계획 비교일 뿐이다. |
| `basket.py` | 선택 결과를 원래 요청에서 재생한 다음 동일 시각의 `EconomicState`, 정책 ID/해시, NAV 사실, 상품 허용 목록, 상품별 자본 금액과 현금, 만료 시각을 SHA-256 commitment에 묶는다. 정책·상태 시퀀스·상품 금액·원천 해시·만료 시각이 바뀌면 재생 검증이 실패한다. NAV와 비교에 사용한 상품 입력은 commitment 만료 시점까지 신선해야 한다. 사실의 외부 진실성은 입증하지 않는다. 결과는 `NO_BASKET_EXECUTOR`이며 승인·예약·거래가 아니다. |
| `authenticated_basket.py` | 서명된 전체 원본·고정 신뢰 설정을 실제로 재생한 뒤 시장 관측, 위험 가정, 포지션 스냅샷의 신선도가 계획 만료까지 유지되는지 검사한다. 원래 basket 계산과 signed assembly·bundle·trust root·증명 목록 해시를 별도 v2 commitment에 묶는다. v1 검증기는 v2를 거부한다. 서명은 원천 주장에 관한 것이지 시장 진실의 증명이 아니다. |
| `chain_binding.py` | 검증된 basket commitment와 명시된 TVM 20바이트 주소·chain ID·토큰 소수 자릿수에서 `sha256(abi.encode(...))`용 32바이트 필드를 만든다. v2 경로는 서명된 원본·trust root·basket 전체를 먼저 재생한 뒤 v2 commitment 해시를 바인딩한다. `prepare_attestation_message`는 이 원본 재생 뒤 registry 주소·chain ID·binding·authenticated source hash·검증자 epoch의 타입 고정 SHA-256 메시지를 계산한다. 서명 생성·전송은 없다. 토큰 최소단위로 정확히 표현되지 않는 금액과 초 단위가 아닌 만료 시각은 거절한다. Python과 격리 EVM의 binding/attestation 해시가 일치한다. 자산 decimals는 별도 검증이 필요하다. |
| `vault_batch.py` | 서명된 부모 basket의 모든 비현금 상품을 빠짐없이 2~8개 child registry binding으로 컴파일한다. 각 상품별 금액·정책·대상·출력 자산·최소 수령량·calldata 해시·기한을 묶고 binding hash 순으로 정렬해 금고의 `batchDigest`와 같은 SHA-256을 만든다. child는 별도 `economic-vault-leg-registry-binding-1` 타입이며 일반 전체 자본 binding 검증기로 재생할 수 없다. 원본과 plan 전체를 `verify_vault_batch`로 재생해야 한다. 결과는 `NOT_SIGNED`·`NOT_BUILT`·`execution_authority=NONE`이다. |
| `vault_registration.py` | 전체 signed plan을 재생한 다음 각 child의 레지스트리 등록 필드와 검증자 원문 SHA-256 메시지 해시만 준비한다. 검증자 epoch는 호출자가 제공한 미확인 주장이다. 고객 노드에서는 각 child의 레지스트리 상태를 읽기 전용으로 수집하며, 모든 응답의 블록·runtime hash·endpoint·현재 epoch·정지 상태가 같고 60초 안에 수집됐는지 검사한다. 동일 자산의 **배치 총 예약액**, 같은 정책에 속한 child의 합산 예약액도 대조한다. 하나라도 불일치하면 batch 전체를 `WITHHELD`로 분류한다. `OBSERVED_MATCH`도 노드 응답의 내부 일치일 뿐 권한은 `NONE`이다. |
| `tron_registry_read.py` | 고객이 지정한 TRON 노드의 solidified 블록을 읽기 전후에 확인하고, 그 사이의 정책·basket·중복 commitment 링크·정지/활성 상태와 정책별·자산별 예약액·누적 소비액·남은 한도를 서명 없는 constant call로 관측한다. v4 관측은 정책의 증명 필수 여부, binding의 authenticated source hash, 등록 epoch와 현재 검증자 epoch도 읽어 signed/child 바인딩에 대조한다. 현재 검증자 epoch와 다른 과거 증명은 금고 실행 조건과 같이 유보한다. fullnode에서 runtime bytecode를 받아 설정된 해시와 비교한다. 블록 변화·오래된 블록·ABI 오류·VM 실패·불일치는 거부/유보하며 `OBSERVED_MATCH`도 `NODE_RESPONSE_ONLY`·`execution_authority=NONE`이다. fullnode의 코드와 solidified 상태가 같은 높이라는 증거는 아니며 RPC 응답은 Merkle 증명이 아니다. |
| `tron_consumption_read.py` | 고객이 지정한 solidified 노드에서 거래 본문·실행 영수증·포함 블록을 읽는다. 거래 원본 protobuf 바이트의 SHA-256이 `txID`와 일치하는지 본문과 블록 양쪽에서 검사하고, 블록 높이/시각·실행 결과·레지스트리의 `BasketConsumed` 이벤트를 바인딩과 대조한다. `CONSUMPTION_OBSERVED`는 registry 기록 소비 관측일 뿐 체결·자산 이전·사용자 승인·정산이 아니다. 신뢰 표시는 `NODE_RESPONSE_ONLY`, 정산은 항상 `NOT_VERIFIED`, 실행 권한은 `NONE`이다. |
| `kernel.py`, `transitions.py` | 언어 토큰/외부 API/DB 없이 결정적 명령 해석. v3에서 선택한 경로의 실제 명령 인덱스를 trace에 기록한다. 가격 출처 중복·시각·분산, quote TTL/비용, 자본·노출·일일 손실·예상 사후 상태 등 불변조건. 명시적 phase 전이 그래프를 통과해야 하며 같은 프로그램·상태·예약·시각이면 같은 receipt hash. `EXECUTION_LOCKED` 예약은 최초 의도 TTL이 지난 뒤에도 다른 프로그램의 자본 계산에 포함한다. |
| `runtime.py`, `journal.py` | 고객 노드 SQLite 트랜잭션으로 프로그램 버전, 상태, 의도 예약, 이벤트 해시 체인, receipt, 추론 심사 결과를 보관하고 재생한다. 프로그램별 미해결 `ESCALATED` 결과는 예외 요청함에 한 건만 유지하며 회복 시 닫는다. `begin_execution`은 최신 상태·활성 프로그램·유효 의도·저널/receipt 재생을 확인한 뒤 예약을 서명기 호출 전에 `EXECUTION_LOCKED`로 동결한다. `record_execution_evidence`는 단계별 증거를 직렬화·저널화한다. 같은 프로그램은 잠금 중 재배분하지 않고, 타 프로그램은 잠긴 자본을 계속 차감한다. 의도 TTL·프로그램 교체·pause·tick도 이 잠금을 풀지 않는다. 계정 재관측 증거가 독립 검증되고 현재 state root·예상 잔액/노출/일일 손실과 일치해야 `RECONCILED`로 풀린다. `record_execution_exception`은 검증된 실패·부분체결·확정 취소를 `DISPUTED`로 남기고 미전송 예약 취소·새 배분 중지·기존 잠금 유지를 수행한다. 정산 후 재조직이면 원 예약을 다시 잠근다. 이는 한 노드의 로컬 무결성/제어이며 제3자 attestation이나 온체인 취소가 아니다. |
| `execution_evidence.py` | 고객 소유 노드의 체인별 구현이 사용자 승인, 서명 페이로드와 실제 제출, 거래 확정, 계정 스냅샷, 거래 예외의 독립 검증을 제공해야 하는 추상 계약. 기본 구현은 없으며 테스트의 합성 verifier는 신뢰할 수 없다. |
| `settlement.py` | 사용자 서명과 체인 확정 사실을 검증하는 **외부 verifier 계약** 및 기대/실제 사후 상태 비교. 실제 verifier가 없으면 `SETTLED` 결과가 나오지 않는다. 테스트의 FixtureVerifier는 가짜 증거를 이용한 단위 테스트 전용이다. |
| `contracts/EconomicPolicyRegistry.sol` | owner만 정책과 basket 기록을 추가한다. 자산별 한도 설정이 선행되어야 한다. basket은 체인 ID·레지스트리 주소·정책 해시·상태 root·basket 해시·오프체인 commitment·자산/대상 주소·금액·만료 시각에서 타입 고정 SHA-256을 계산한다. `registerAttestedPolicy`로 등록한 정책은 구형 `commitBasket`을 거부하고 `commitBasketAttested`에서 현재 epoch의 서로 다른 검증자 2명이 binding과 authenticated source hash에 서명한 원문 해시를 `ecrecover`로 확인한다. 서명자 순서·low-s·역할·epoch를 검사하고 검증자 변경은 이전 미등록 서명을 무효화한다. 등록한 source hash와 epoch를 공개 조회할 수 있다. 정책별·자산별 예산, 취소·만료 해제, 지정 대상의 1회 소비를 유지한다. **검증자가 자료를 참으로 확인했다는 증명이나 실제 거래 강제는 아니다.** 자금 입금 경로는 의도하지 않았지만 TRON native `TransferContract`가 fallback을 우회하므로 무단 TRX가 묶일 수 있다. |
| `contracts/EconomicCapitalVault.sol` | 고객 소유자가 자산을 입금하는 **미배포·미감사** 자본 경계 소스. 엄격한 2인 증명 정책의 유효 basket만 받아들이며, 현재 epoch와 출처 해시를 확인한다. 소유자의 raw secp256k1 서명은 chain ID·금고·레지스트리·basket binding·입력 자산/금액·고정 대상·출력 자산/최소 수량·정확한 calldata 해시·기한을 묶는다. 여러 상품은 같은 부모 basket hash와 정렬된 child binding을 가진 2~8개 주문 전체를 한 번에 서명하며, `executeBatch`에서 한 주문이라도 실패하면 이전 토큰 이동·소비·실행 표시까지 원복한다. 승인된 토큰/대상의 코드 해시를 검사하고, 입력 금액만 대상에 전송한 뒤 대상이 같은 거래에서 basket을 소비하고 최소 출력 자산을 금고로 반환해야 성공한다. 코드 해시만으로 프록시 내부 구현이나 경제적 정당성을 증명하지 못한다. |
| `scripts/verify_economic_contract.py` | 공식 TRON 0.8.20 컴파일러 SHA-256을 확인한 뒤 재컴파일하고 basket ABI 타입·이벤트 인덱스·비지불 ABI·바이트코드 크기·해시를 대조한다. 배포 명령은 없다. |
| `scripts/verify_economic_vault.py` | 동일 고정 TRON 컴파일러로 금고 소스를 다시 빌드해 ABI·이벤트 인덱스·비지불 속성·크기·해시를 manifest와 대조한다. 배포 명령은 없다. |
| `tests/test_economic_contract_runtime.py` | 같은 Solidity 소스를 공식 Ethereum 0.8.20 컴파일러와 격리 EVM으로 실행해 Python 해시 동등성, owner 제한, 중복·정지·취소·정책 취소·만료, target만 1회 소비할 수 있음을 검사한다. EVM 전용 `MockBasketTarget`이 소비 후 revert하면 소비 상태와 회계도 원복되고, 성공하면 재소비가 거부된다. 다중 basket의 정책별·동일 자산 정책 간 동시 한도, 취소·누적 소비·만료 예약 해제, 한도 축소 금지도 검사한다. TVM 바이너리는 TRON 전용 opcode가 있으므로 EVM에서 실행하지 않으며 이 테스트가 TVM 검증을 대체하지 않는다. |
| `cli.py` | `spec`/`spec-vectors`, `portfolio-select`, signed/unsigned 상품 조립·basket commitment·chain binding·TRON 읽기 전용 관측/평가, `chain-attest-message-signed`, `vault-batch-plan-signed`/`vault-batch-verify-signed`/`vault-batch-attest-signed`/`vault-batch-observe-signed`/`vault-batch-assess-signed`, `grid-search`/`grid-verify`, 입력 파일 컴파일, 가상 상태 설치, 프로그램 등록, 상태 전이 주입, 평가, receipt 재생, 노드 상태 조회, `pause`/`resume`, 읽기 전용 `intent-status`/`escalations`, `begin-execution` 자본 잠금, `assess-inference`/`verify-inference`. signed 경로는 서명 원본부터 basket·binding 또는 batch plan까지 재생 검증한다. 고객 키·지갑·전송 기능은 없다. |

Man AHL의 [AI 운용 사례](https://www.man.com/insights/AI-asset-management-lightbulb-moment)는 데이터 보강, 특징 생성, 추출, 포트폴리오 구성에서 AI가 기여할 수 있다고 설명한다. 이 프로젝트는 모델의 입력/출력과 자본 판단·실행 권한을 별도 계약으로 두는 방향을 택한다. 이는 Man Group의 내부 시스템을 재현했다는 뜻이 아니다. TRON의 [공식 Solidity 문서](https://developers.tron.network/docs/smart-contract-language)는 새 컨트랙트에 0.8.x를 권장하며, [공식 solc-bin](https://github.com/tronprotocol/solc-bin)의 0.8.20 바이너리를 해시 검증해 사용했다.

## LLM은 추론 초안만 제출한다

`economic-inference-draft-1`은 모델 ID·모델 해시·요청 해시·상태 root·생성/만료 시각·confidence·부족한 정보·제안 프로그램을 포함한다. `economic-inference-scope-1`은 로컬 운영자가 지정하는 프로그램/소유자/직원/네트워크, 허용 자산·자본·비용·손실·프로토콜·관측 경로의 상한이다. 초안을 현재 상태와 scope에 묶어 평가하고 SHA-256 심사 결과를 저널에 기록한다. 프로그램을 `REVIEW_REQUIRED`로 분류해도 `active_programs`·자본 예약·거래는 증가하지 않는다. 모호한 초안은 `ABSTAIN`으로 남기며 사실을 임의로 StateDelta에 쓰지 않는다.

이 scope는 **암호학적 사용자 승인 증거가 아니다.** 모델 해시는 제출된 식별자일 뿐 실제 모델 바이너리의 증명은 아니며, 요청 해시는 자연어 의미의 정합성을 입증하지 않는다. 유효 초안을 실제 사용자 지시와 대조·승인하는 계약, 인증된 상태 소스, 서명·전송 포트는 여전히 필요하다. 예외 요청함은 외부 해석자가 읽을 수 있는 항목만 만들고 모델을 직접 호출하지 않는다.

## 서명기 이전의 자본 잠금

`begin-execution`은 이미 `AWAITING_AUTHORIZATION`인 결정 receipt를 다시 계산하고, 저널·현재 state root·활성 프로그램 버전·의도 만료를 확인한 뒤 SQLite 트랜잭션 한 번으로 해당 예약을 `EXECUTION_LOCKED`로 바꾼다. 반환값은 잠금 이벤트 해시와 `execution_authority=NONE`뿐이다. 서명이나 거래를 만들지 않는다. 같은 receipt의 동시 요청은 하나의 이벤트만 남긴다.

```text
PYTHONPATH=src python -m economic_machine.cli begin-execution \
  --db <고객-노드-runtime.sqlite3> --receipt-hash <결정-receipt-hash> \
  --at <UTC-시각>
PYTHONPATH=src python -m economic_machine.cli intent-status \
  --db <고객-노드-runtime.sqlite3> --receipt-hash <결정-receipt-hash> \
  --at <UTC-시각>
```

이 잠금은 서명 후 전송이 지연되거나 결과가 불명확한 상황을 대비해 **기존 의도 TTL, 프로그램 교체·일시정지, 주기적 `tick`으로 자동 해제되지 않는다.** 다른 프로그램은 잠긴 금액을 계속 예약 자본으로 계산한다. 상태 변경은 저널에 남되 잠긴 프로그램의 재배분은 건너뛴다. `status.active_reservations`는 미전송 예약과 실행 잠금의 합이며 별도 `unsent_reservations`·`execution_locked_reservations`·`reconciled_reservations`가 각각의 수를 보인다.

잠금 후의 `record_execution_evidence`는 체인별 `ExecutionEvidenceVerifier`를 주입받아 `AUTHORIZE → SUBMIT → FINALIZE → RECONCILE` 순서로만 진행한다. 사용자·의도 해시·네트워크·거래 ID·유효시각·확정 블록과 증거 해시를 대조한다. `FINALIZE`만으로 잠금을 풀지 않는다. 최종 `RECONCILE`은 확정 뒤 관측한 최신 `EconomicState`의 root·sequence 및 잔액/노출/일일 손실이 원래 intent의 사후조건과 정확히 일치하고, 외부 계정 증거 검증도 통과할 때만 예약을 해제한다. 동일 증거의 재제출은 멱등이고 다른 증거·단계 역행·저널/예약 불일치는 거부한다. 이 API는 서명·전송·체인 조회를 수행하지 않는다. **실제 verifier, 실패/부분체결/재조직 복구, 고객 노드와 금고의 연결이 없어 고객 자금 운용에는 사용할 수 없다.** 실패나 불일치는 보수적으로 잠금을 유지하며 운영자 복구 절차도 미구현이다.

`record_execution_exception`은 제출 후의 `REVERTED`·`PARTIAL_FILL`·`REORG`를 외부 검증자와 원 거래·의도·네트워크·확정 영수증에 묶어 기록한다. 기록되면 모든 미전송 예약을 취소하고 새 평가·실행 시작·프로그램 재개를 막는다. 상태 관측은 계속 저널에 받을 수 있지만 새 kernel 판단은 생성하지 않는다. 이미 `RECONCILED`였던 거래의 재조직도 원 예약을 다시 `EXECUTION_LOCKED`로 바꾸고 전체 런타임을 정지한다. 이것은 과거에 이미 전송된 거래를 취소하거나 과거 자본 사용을 소급 무효화하지 않는다. 현재는 분쟁 해소의 독립 증거와 재개 전이가 없어 정지 상태를 수동으로 우회할 수 없다.

## 선택된 배분안의 상태·정책 묶음

`grid-search`는 후보를 손으로 나열하지 않고 이산 비중 조합을 모두 만든다. 두 상품, 500bps 간격인 합성 사례는 현금 조합을 포함해 231개를 전부 평가한다. 제공된 가정 아래 `lend_A=30%`, `stable_B=60%`, 현금 `10%`를 골랐고 순예상수익은 `474bps`, 최대 시나리오 손실은 한도와 같은 `150 USDT`였다. 이 수치는 합성 입력의 계산 결과일 뿐 실제 수익 전망이 아니다. 100bps 간격처럼 4,096개 한도를 넘는 요청은 중도 종료 후 불완전한 최적값을 반환하지 않고 거절한다. `grid-verify`는 요청에서 전체 격자와 선택 결과를 다시 계산한다. 선택 결과의 `selection_request`와 `portfolio_verdict`는 아래 `basket-commit`에 바로 전달할 수 있다. 이 경로도 상품 수익률·스트레스 수치가 참인지 검증하지 않는다.

```text
PYTHONPATH=src python -m economic_machine.cli grid-search \
  --request cases/economic_portfolio_grid_demo.json > grid.json
PYTHONPATH=src python -m economic_machine.cli grid-verify \
  --request cases/economic_portfolio_grid_demo.json --grid-verdict grid.json
```

v2의 `scenario_pnl_bps`는 손실만 적는 v1 입력과 달리 같은 충격에서 헤지의 양(+) 손익도 적는다. 이 입력은 **거래비용 제외 시나리오 손익**이어야 한다. 각 시나리오의 포트폴리오 손익은 `Σ(상품 비중 bps × 상품 손익 bps) / 10,000`; 최대 손실은 `자본 × max(0, 추정 거래비용 bps - 최저 시나리오 손익 bps) / 10,000`이다. 각 상품의 `factor_loadings_bps`를 같은 방식으로 합산해 순노출을, 절댓값을 합산해 총노출을 구한다. `factor_net_bounds_bps`와 `factor_gross_caps_bps`는 서로 다른 상품 그룹이 같은 기초 위험을 쌓는 경우도 제한한다. `max_source_skew_ms`는 최신·최고령 상품 관측 간의 시간 차이를 제한한다. 부호 있는 수치와 비중은 정수 bps, 자본 계산은 정확한 십진수만 허용한다. v1 입출력 계약과 해시는 그대로 유지하고 v2는 별도 schema를 쓴다.

합성 v2 사례 `cases/economic_portfolio_hedged_demo.json`에서는 `tron_lend 35% + tron_short 35% + stable_yield 20% + 현금 10%`가 선택된다. TRX 충격에서 순 TRX 노출은 `0bps`지만 총노출은 `7,000bps`로 남고, 가장 나쁜 시나리오에 추정 거래비용을 더한 계산 손실은 `81.65 USDT`다. 원금 쏠림 후보는 스트레스·순노출 한도를, 과도한 양방향 포지션 후보는 총노출 한도를 위반한다. hedge의 funding 악화 가정 하나만 바꾸면 모든 후보가 탈락해 `ABSTAIN`이 된다. 2,500bps 격자에서는 35개 후보를 완전 열거하며, 그 격자의 선택은 세 상품 각각 25%와 현금 25%다. 이 값은 **합성된 시나리오 입력 안에서만** 계산한 결과이며 헤지 효율, 미래 상관, 실제 거래 가능성의 증거가 아니다.

```text
PYTHONPATH=src python -m economic_machine.cli portfolio-select \
  --request cases/economic_portfolio_hedged_demo.json
```

상품별 입력 조립은 `cases/economic_portfolio_template_demo.json`과 `cases/economic_product_bundle_demo.json`에서 시작한다. template는 자본·한도·후보를, bundle은 상품 명세·시장 관측·위험 모델 가정·현재 보유비중을 각각 담는다. SPOT은 `호가자산/기초자산` 단위의 bid/ask에서 spread를 **올림** 계산한다. PERP는 mark/index basis를 상한과 대조하고 funding 관측값을 모델 가정 해시에 묶는다. LENDING은 실제 상환 가능 표시, 상환 지연, 진입/퇴장 수수료를 검사한다. 모든 상품의 `tradable_notional`은 template 자본 자산 단위여야 하며, 이 수용량보다 큰 후보 비중은 탈락한다. APY나 funding 숫자를 자동으로 기대수익으로 변환하지 않는다. 해당 기간·포지션 방향·변동위험을 반영한 `expected_return_bps`와 `scenario_pnl_bps`는 별도 위험 모델 입력이다.

```text
PYTHONPATH=src python -m economic_machine.cli portfolio-assemble \
  --request cases/economic_portfolio_template_demo.json \
  --bundle cases/economic_product_bundle_demo.json > assembly.json
PYTHONPATH=src python -m economic_machine.cli portfolio-assemble-verify \
  --request cases/economic_portfolio_template_demo.json \
  --bundle cases/economic_product_bundle_demo.json --observation assembly.json
```

출력에는 v2 `selection_request`·`portfolio_verdict`와 각각의 상품 명세/관측/가정 해시가 있다. 두 필드를 `basket.py`에 전달하면 원본 요청을 재계산한 **비실행** commitment를 만들 수 있고, 어느 상품 입력 해시라도 바뀌면 재생은 실패한다. 반면 `source_record_hash`, `model_hash`, 현재 보유비중 출처는 제공자가 주장한 값이다. 조립 해시와 `verified_replay`는 JSON 변조 감지와 계산 재현성만 증명한다. 이 sidecar를 버리고 `selection_request`만 떼어 쓰면 `CLAIMED_NOT_VERIFIED`라는 원천 상태도 사라지므로 향후 실행 포트는 원본 bundle의 별도 인증·재검증을 요구해야 한다.

별도의 signed 경로는 고객 노드가 소유한 `economic-evidence-trust-roots-1` 설정에서 OpenSSL 실행 파일 경로/SHA-256, 상품 명세 해시, 역할별 Ed25519 공개키·네트워크·자산·유효기간·취소 표시와 최소 서명 수를 지정한다. 시장 관측은 서로 다른 공개키 **최소 2개**, 위험 모델 가정과 보유비중은 각각 최소 1개의 서명이 필요하다. 각 발급자는 `domain=ECONOMIC_SIGNED_EVIDENCE_V1`, 역할, 발급자 ID, 서명시각, 원본 payload를 포함한 canonical JSON 바이트에 서명한다. 서명 시각은 해당 관측/모델 생성 이후, 배분 시점 이전이어야 한다. [OpenSSL 3.0의 Ed25519 `pkeyutl` 사양](https://docs.openssl.org/3.0/man1/openssl-pkeyutl/)에 따라 원문 `-rawin` 검증을 사용하며, 실행 파일에는 shell을 거치지 않는 인자 배열만 전달한다. 매 조립 때 [RFC 8032의 Ed25519 검증 벡터](https://www.rfc-editor.org/rfc/rfc8032.html#section-7.1)를 올바르게 승인하고 바뀐 메시지를 거부하는 양·음성 자체 검사를 통과해야 한다. production 코드에는 private key·sign 명령이 없다. 테스트만 임시 키를 만들고 사용 후 폐기한다.

```text
PYTHONPATH=src python -m economic_machine.cli portfolio-assemble-signed \
  --request cases/economic_portfolio_template_demo.json \
  --bundle <서명된-상품-bundle.json> --trust-roots <고객-노드-신뢰설정.json> \
  > signed-assembly.json
PYTHONPATH=src python -m economic_machine.cli portfolio-assemble-signed-verify \
  --request cases/economic_portfolio_template_demo.json \
  --bundle <서명된-상품-bundle.json> --trust-roots <고객-노드-신뢰설정.json> \
  --observation signed-assembly.json
```

이 경로는 **설정된 키가 특정 값을 주장했음**을 확인한다. 서로 다른 키가 실제로 독립된 기관에서 운영되는지, 공급자가 올바른 가격과 포지션을 제공했는지, risk model 바이너리 해시가 진짜 모델을 가리키는지 증명하지 않는다. 신뢰 설정 파일과 OpenSSL 공유 라이브러리의 무결성도 고객 노드 운영 경계에 남는다. 원천 API의 인증·권리·교차 검증과 온체인 상태 증명을 별도로 붙이기 전에는 결과를 거래 허가로 바꿀 수 없다. 서명된 조립 결과 안의 일반 `assembly`도 원본 bundle만 보면 `CLAIMED_NOT_VERIFIED`로 남으며, signed wrapper의 검증 증거를 버리고 하위 배분안만 실행 경로에 전달해서는 안 된다.

`basket-commit-signed`는 이 wrapper를 버리지 않는다. 원본 signed bundle과 trust roots를 재검증하고 v1 basket 계산을 재생한 다음, 서명된 입력·증명 해시를 `economic-basket-commitment-2`에 포함한다. 시장 관측과 모델 가정의 `valid_until`, 포지션 관측의 최대 허용 연령이 basket 만료까지 충분하지 않으면 거부한다. `chain-bind-signed`는 v2 commitment 전체를 다시 검증하고 `economic-basket-registry-binding-2`에 연결한다. `tron-observe-signed`와 `tron-basket-observe-signed`는 원본·commitment·binding 재생 검증이 실패하면 노드 요청 전에 끝난다. 대응 `*-verify-signed`/`*-assess-signed` 명령도 같은 원본을 요구한다. 이 경로에 쓰는 정책은 signed bundle에 있는 모든 선택 가능 상품을 명시적으로 허용해야 한다.

```text
PYTHONPATH=src python -m economic_machine.cli basket-commit-signed \
  --request <portfolio-template.json> --bundle <signed-bundle.json> \
  --trust-roots <trust-roots.json> --state <state.json> --policy <policy.json> \
  --valid-until 2026-09-25T12:01:45+00:00 > signed-basket.json
PYTHONPATH=src python -m economic_machine.cli chain-bind-signed \
  --request <portfolio-template.json> --bundle <signed-bundle.json> \
  --trust-roots <trust-roots.json> --state <state.json> --policy <policy.json> \
  --commitment signed-basket.json --chain-context <registry-context.json> \
  > signed-binding.json
```

새 레지스트리의 `registerAttestedPolicy`는 같은 policy ID에서 구형 `commitBasket`을 영구 차단한다. `setAttestor`로 owner/guardian과 다른 주소의 검증자 키를 두 개 이상 고정하고, `commitBasketAttested`는 **현재 검증자 epoch**·레지스트리 주소·체인 ID·binding hash·authenticated source hash에 대해 서로 다른 검증자 2명의 secp256k1 원문 해시 서명을 요구한다. 서명 순서는 주소 오름차순이고 `v=27/28`, low-s 형식만 허용한다. `chain-attest-message-signed`는 고객 노드가 원본 bundle을 다시 검증한 뒤 서명할 메시지 **해시만** 준비하며 실제 서명은 외부 검증자의 책임이다. `--attestor-epoch`는 레지스트리의 읽기 전용 관측값과 대조해야 하며, 키 변경 후 이전 epoch의 아직 등록되지 않은 서명은 사용할 수 없다.

```text
PYTHONPATH=src python -m economic_machine.cli chain-attest-message-signed \
  --request <portfolio-template.json> --bundle <signed-bundle.json> \
  --trust-roots <trust-roots.json> --state <state.json> --policy <policy.json> \
  --commitment signed-basket.json --chain-context <registry-context.json> \
  --chain-binding signed-binding.json --attestor-epoch <onchain-epoch>
```

이 정책 모드는 **특정 정책 ID의 unsigned 우회**를 차단하지만 모든 legacy 정책의 등록을 없애지는 않았다. owner가 다른 legacy 정책을 만들거나 검증자 키를 임의로 지정하는 것은 여전히 가능한 신뢰 경계다. 컨트랙트는 Ed25519 원천 서명을 직접 검증하지 않는다. 두 검증자가 binding과 source hash의 관계를 검토했다고 서명한 사실만 강제하며, 시장 가격·모델 가정·독립성·사용자 승인·체결을 증명하지 않는다. 기존에 등록된 basket은 검증자 키 변경만으로 자동 취소되지 않으므로 guardian의 `pause`·`revokeBasket` 운용 규칙이 필요하다. 레지스트리 자체는 target 코드나 실제 자본을 강제하지 않는다. 별도 금고 소스의 조건은 아래에서 구분한다.

`basket-commit`은 포트폴리오 verdict를 원래 요청으로 다시 계산해 같음을 확인한다. 적격 후보가 없으면 commitment도 만들지 않는다. 같은 시각·소유자·네트워크의 상태에 정책을 적용하고, `portfolio.nav` 같은 정책 지정 자본 사실이 요청 자본 이상이며 단위·신선도가 맞는지 검사한다. 선택된 비중의 각 상품 금액과 현금 금액의 합은 정확히 요청 자본이어야 한다. 이 결과와 정책 해시·상태 root·요청/선택 해시·유효 기간을 단일 commitment로 묶는다. 데모에서는 1,000 USDT 중 `lend_A=200`, `stable_B=700`, 현금 `100`이다.

```text
PYTHONPATH=src python -m economic_machine.cli basket-commit \
  --request cases/economic_portfolio_candidates_demo.json \
  --state cases/economic_basket_state_demo.json \
  --policy cases/economic_basket_policy_demo.json \
  --valid-until 2026-09-25T12:03:00+00:00 > basket.json
PYTHONPATH=src python -m economic_machine.cli basket-verify \
  --request cases/economic_portfolio_candidates_demo.json \
  --state cases/economic_basket_state_demo.json \
  --policy cases/economic_basket_policy_demo.json \
  --commitment basket.json
```

이 명령은 과거 시점의 합성 사례를 재생한다. `verified_replay=true`는 현재 유효한 주문이나 서명을 뜻하지 않는다. 레지스트리의 `commitBasket`은 오프체인 commitment **해시와 타입이 있는 연결 필드**를 다시 해시한다. 그러나 오프체인 JSON·시장 사실·상품별 금액을 온체인에서 재계산하지 않는다. 고객 소유 노드의 RPC 관측 adapter와 정책별 basket 회계는 이제 있으나, 관측 결과만으로 거래를 허용하지 않는다. 다중 상품 batch executor 소스와 서명 전 plan compiler는 추가했으나, child 정책의 검증자 실제 증명·등록, 시장 원천의 진실성, 전역 동시 예약, 개별 상품 체결/부분체결, 실제 체인 확정 검증이 붙기 전에는 배분안을 실행할 수 없다. 단일 상품 `ALLOCATE` ISA로 다중 상품 basket을 가장하지 않는다.

`chain-bind`는 `--request`·`--state`·`--policy`·`--commitment`·`--chain-context`를 받아 준비 전용 바인딩 해시를 만든다. `chain-bind-verify`는 같은 입력과 `--chain-binding`을 받아 재생한다. `cases/economic_basket_registry_context_demo.json`의 chain ID·주소는 합성값이다. TRON의 [TVM/EVM 차이 문서](https://developers.tron.network/re/docs/tvm-vs-evm)에 따르면 TVM 내부 주소는 20바이트지만 외부 표시에는 `0x41` 접두사가 붙으며, 체인 ID와 native TRX 전송도 EVM과 차이가 있다. 실제 네트워크 컨텍스트를 하드코딩된 예제로 추정해서는 안 된다.

`tron-observe`는 위 5개 원본 파일과 `--chain-binding`, `--reader-config`를 받는다. 원본에서 바인딩을 먼저 재생해 같지 않으면 네트워크를 읽지 않는다. 설정은 `economic-tron-registry-reader-1` 버전, 고객이 통제/선택한 `solidity_url`·`fullnode_url`, 20바이트 `owner_address`, 외부 배포 증거와 대조해 지정한 `expected_runtime_sha256`, `max_block_age_ms`, API 키의 **환경변수 이름**인 `api_key_env`(없으면 `null`)를 정확히 포함한다. 키 자체는 설정 파일에 넣지 않는다. URL은 HTTPS 또는 loopback HTTP로 제한하고 redirect를 거부한다. 아래 명령은 실제로 등록된 배포 주소/정책/바인딩이 있을 때만 관측을 반환한다. 현재 합성 데모 주소로는 성공할 수 없다.

```text
PYTHONPATH=src python -m economic_machine.cli tron-observe \
  --request request.json --state state.json --policy policy.json \
  --commitment basket.json --chain-context context.json \
  --chain-binding binding.json --reader-config reader.json > observation-report.json
PYTHONPATH=src python -m economic_machine.cli tron-assess \
  --request request.json --state state.json --policy policy.json \
  --commitment basket.json --chain-context context.json \
  --chain-binding binding.json --observation observation-report.json
```

읽기 호출은 TRON [solidified `getnowblock`](https://developers.tron.network/reference/getnowblock)과 [solidified `triggerconstantcontract`](https://developers.tron.network/reference/triggerconstantcontract-1)를 사용한다. constant call의 API 성공 표시와 TVM 실행 성공 표시를 모두 확인한다. 컨트랙트 runtime code는 [fullnode `getcontractinfo`](https://developers.tron.network/reference/getcontractinfo)에서 가져오므로 동일한 solidified 높이에 묶인 증거가 아니다. 읽기 전후의 solid block ID가 같다는 검사는 로드밸런서 뒤 여러 노드의 응답이 실제로 한 상태에서 나온 것까지 증명하지 않는다. 운영자가 예상 코드 해시를 잘못 입력하거나 악의적인 RPC를 믿으면 비교 결과도 틀릴 수 있다. 배포 영수증과 code hash·chain ID·주소를 별도로 검증하고, 여러 독립 노드의 일치와 고정 블록 기반 상태 증거를 추가해야 실행 경계에 연결할 수 있다. `OBSERVED_MATCH`는 이 한계를 명시한 조사 결과일 뿐 승인·체결·정산이 아니다.

`tron-tx-observe --txid <64자리 hex> --reader-config tx-reader.json`은 거래 자체를 읽는다. `tron-basket-observe`는 위 5개 원본 파일·`--chain-binding`·`--reader-config`·`--txid`를 받아 **원본 바인딩을 재생한 뒤** 거래를 읽어 `BasketConsumed`를 대조한다. `tron-basket-assess`는 동일한 원본과 저장된 `--observation`을 오프라인에서 다시 대조한다. 거래 리더 설정은 `economic-tron-transaction-reader-1`, `solidity_url`, `api_key_env`, `max_block_age_ms`, `network_anchor_height`, `network_anchor_block_id`를 요구한다. anchor의 블록 ID는 고객이 별도 경로에서 확인한 고정 값이어야 한다. 같은 RPC에서 즉석 조회해 채우는 것은 연결 테스트일 뿐 신뢰 앵커가 아니다. 읽기 경로는 [확정 거래 본문](https://developers.tron.network/reference/gettransactionbyid), [확정 실행 영수증](https://developers.tron.network/re/reference/gettransactioninfobyid-1), [확정 블록 거래 목록](https://developers.tron.network/reference/getblockbynum)을 대조하고, TRON의 [`txID` 계산 규칙](https://developers.tron.network/docs/encoding)에 따라 `raw_data_hex`의 SHA-256을 검사한다. 누락된 거래는 실패라고 단정하지 않고 `NOT_OBSERVED`, 본문만 있으면 `SOLID_BODY_RECEIPT_MISSING`, 성공/실패 실행은 각각 `SOLID_EXECUTED`/`SOLID_EXECUTION_FAILED`로 구분한다.

이 대조는 **노드 응답의 내부 일치 검사**다. `observation_hash`는 저장 레코드의 변경 감지용이며, 악의적 노드가 위조한 블록·영수증을 배제하는 증명은 아니다. 이벤트가 정확해도 `CONSUMPTION_OBSERVED` 이상으로 승격하지 않는다. 실제 레지스트리 배포 및 고객 basket 소비 사례는 아직 없으므로 이벤트 대조는 합성 테스트로 검증했고, Shasta 공개 거래에서는 원본 거래·블록·영수증 경로만 시험했다. 실제 자산 이동, 체결 수량, 사후 잔액, 사용자 승인 증거와 독립 확정 증거가 갖춰질 때까지 settlement verifier에 연결하지 않는다.

레지스트리에서 `COMMITTED`인 basket은 정지·정책 취소·개별 취소·만료 전까지 지정된 `policy.target`만 `consumeBasket`으로 `CONSUMED`로 바꿀 수 있다. 소비 표시를 먼저 기록하고 이벤트를 내며, 해당 target의 같은 바깥 거래가 revert하면 표시도 원복된다. 같은 오프체인 commitment 해시는 이 레지스트리에서 두 binding으로 등록할 수 없다. `maxAmount`는 **해당 policy의 basket 기록 한도**다. 별도 `assetBudget.limit`는 동일 자산의 여러 policy에서 발생한 basket 예약·누적 소비를 합산하지만 지갑 잔액이나 실제 노출을 읽지 않는다. owner는 사용 중인 금액 아래로 한도를 낮출 수 없지만 높일 수 있다. 누적 소비액은 거래 종료 후에도 자동으로 재사용되지 않는다. 해지·정산을 검증해 자본을 다시 열어 주는 별도 전이가 없기 때문이다. 한도를 우회하던 레거시 `commitIntent`·`activeIntent` ABI는 배포 전 계약에서 제거했다. 새 의도 의미론은 basket과 같은 예약·취소·소비 회계가 설계될 때만 추가한다. 만료된 basket 예약은 자동 해제되지 않으며 누구나 `releaseExpiredBasket`을 호출해 명시적으로 해제할 수 있다. 이때 binding과 오프체인 commitment의 재사용은 여전히 금지된다. 하지만 target이 EOA이거나 거래를 수행하지 않는 계약이면 소비만 하고 끝낼 수 있다. `BasketConsumed`는 **체결·정산 영수증이 아니다.** 별도 금고 소스에서 코드 해시와 토큰 잔액 조건을 추가했지만, 실자산 보증에는 TVM 실행·토큰/대상 신뢰·거래별 사후 상태·체인 확정 검증과 감사가 더 필요하다. 트랜잭션 실패 시 상태 원복은 [TRON VM 예외 처리 문서](https://developers.tron.network/docs/vm-exception-handling)의 원자성 설명과도 일치하며, 현재 검증 증거는 격리 EVM 테스트뿐이다.

대회에서 고른 하나의 선형 프로그램 경로는 `risk.coverage_ratio >= 1.5`를 확인하고, 유효한 두 quote의 총비용 `5`와 `3` 중 `3`을 선택한다. 200 USDT 가상 배분은 1,000 잔액, 100 기노출, 5 기존 일일 손실에 대해 예상 잔액 `797`, 예상 노출 `300`, 예상 손실 `8`을 계산한다. 16개 불변조건이 통과해도 결과는 `AWAITING_AUTHORIZATION`, `NOT_SIGNED`, `NOT_SUBMITTED`다. 실제 지갑·프로토콜 상태를 바꾸지 않는다.

v3 가상 프로그램 `cases/economic_program_graph_demo.json`은 같은 위험 비율이 `1.5` 이상이면 200 USDT, 그 아래면 100 USDT 배분 경로로 이동한다. 두 경로 각각 `QUOTE→SCORE→ALLOCATE→SIMULATE→VERIFY→PREPARE→SETTLE`을 거쳐야 한다. 중첩 분기로 세 번째 `HOLD` 종료를 추가한 테스트도 통과했다. 이 배분 금액은 금융 적합성이나 최적성을 뜻하는 값이 아니라 분기 의미론을 검증하는 합성 사례다.

## 미배포 자본 금고와 실자산 경계

`EconomicCapitalVault`는 기록 전용 레지스트리와 달리 토큰을 직접 보관하고 원자적 실행 조건을 검사하는 별도 소스다. 고객 owner만 입금·본인 주소로 출금할 수 있고 guardian은 실행을 멈출 수 있다. 고객이 허용한 토큰과 대상의 현재 코드 해시를 고정한다. 실행에는 레지스트리의 **증명 필수** 정책, 현재 검증자 epoch, 유효 basket과 출처 해시가 모두 필요하다. owner의 raw secp256k1 서명이 금고/체인/레지스트리/binding/입력 금액/대상/출력 자산/최소 수령량/경로 바이트 해시/기한을 정확히 묶는다. 누구나 그 서명된 경로를 중계할 수 있지만 바꿀 수 없다.

금고는 입력 토큰을 정확한 basket 금액만 대상에 보내고 allowance는 주지 않는다. 대상은 같은 거래에서 레지스트리 basket을 소비하고 출력 토큰을 금고에 보내야 한다. 입력 잔액 감소와 출력 잔액 증가를 검사하며, 대상 revert·소비 누락·최소 출력 미달이면 거래 전체가 원복된다. 서명은 고객 소유자가 외부에서 생성해야 하며 이 저장소는 사용자 키, 지갑 UI, 서명기나 체인 송신기를 제공하지 않는다. 실행 대상이 안전하고 경제적으로 타당한 경로를 수행하는지, 토큰/대상이 프록시를 통해 내부 구현을 바꾸지 않는지, 실제 TRC20이 모의 토큰처럼 동작하는지, TVM에서 같은 결과가 나는지는 아직 확인되지 않았다. TRON native 강제 전송으로 들어온 TRX의 회수 경로도 없다. 감사·TVM 실행 검증·배포 전에는 실자산을 넣을 수 없다.

`vault-batch-plan-signed`는 서명된 부모 배분안을 원본부터 재생해 선택된 2~8개 비현금 상품 전부를 child registry binding과 금고 주문으로 바꾼다. 각 child는 **부모 basket hash**를 공유하지만 자기 상품 금액, 정책, 대상, 출력 자산, 최소 수령량과 경로를 가진다. 계약의 `executeBatch`는 binding hash 오름차순의 모든 child가 같은 부모를 가리키는지 확인하고, 고객 소유자가 정확한 주문 집합의 batch digest에 raw secp256k1 서명한 경우에만 원자적으로 실행한다. `vault-batch-verify-signed`는 같은 원본과 경로 사양으로 plan을 재생한다. 이 명령들은 레지스트리 상태 조회, child 등록, 고객 서명, 거래 생성·전송을 수행하지 않는다. child의 명세 타입은 기존 전체 자본 binding과 별개다. child의 등록 증명자는 원본 서명과 부모 basket의 해당 상품이 실제 일치하는지 독립적으로 검사해야 한다. 현재 그 증명·등록 워크플로는 구현되지 않았다.

```text
PYTHONPATH=src python -m economic_machine.cli vault-batch-plan-signed \
  --request <portfolio-template.json> --bundle <signed-bundle.json> \
  --trust-roots <trust-roots.json> --state <state.json> --policy <policy.json> \
  --commitment <signed-parent-basket.json> --vault-context <vault-context.json> \
  --leg-specs <leg-specs.json> --at <UTC-ISO-8601> \
  --batch-deadline <UTC-epoch-seconds> > unsigned-batch-plan.json
```

이어서 `vault-batch-attest-signed --vault-plan unsigned-batch-plan.json --attestor-epoch <현재-epoch>`는 각 child의 레지스트리 등록 필드와 외부 검증자가 서명할 **메시지 해시만** 준비한다. epoch 입력값은 체인에서 확인된 값으로 승격하지 않는다. `vault-batch-observe-signed --vault-plan ... --reader-config ...`는 전체 signed plan을 재생한 뒤 각 child를 읽고 묶음 평가를 함께 출력한다. 저장된 관측은 `vault-batch-assess-signed --vault-plan ... --observation ...`으로 재평가할 수 있다. 세 명령 모두 계획 생성 때의 `--request`, `--bundle`, `--trust-roots`, `--state`, `--policy`, `--commitment`, `--vault-context`, `--leg-specs`, `--at`, `--batch-deadline`을 다시 요구한다. 관측 블록이나 코드 해시가 서로 다르거나 배치 합계보다 자산/정책 예약액이 적으면 전체를 유보한다. 동일 블록의 RPC 응답도 독립된 상태 증명은 아니며, 금고의 코드·잔액·사용자 서명을 확인하지 않았으므로 `execution_authority=NONE`이다. 검증자 서명 수집·child 등록·owner 승인·실제 거래는 계속 미구현이다.

## 고객 노드 프로그램 정지

`economic-machine pause --db <node.sqlite3> --program-id <id> --reason MANUAL_RISK_STOP`은 노드 로컬 운영 명령이다. 같은 SQLite 트랜잭션에서 현재 프로그램을 `PAUSED`로 기록하고 그 프로그램의 활성 예약을 `REVOKED`로 바꾼다. 기존 DecisionReceipt는 과거 판단의 증거로 재생되지만 `intent-status`의 `locally_pending`은 거짓이 된다. 일시정지 중 StateDelta는 계속 저장하되 해당 프로그램의 커널 호출은 0회다. 동일 프로그램을 개정해도 `PAUSED`가 유지되며 `resume`을 명시적으로 실행해야 재개된다. 재개 후에도 폐기된 동일 receipt를 다시 예약할 수 없다.

`intent-status`는 읽는 순간의 **로컬 상태 스냅샷**이며 승인이나 서명이 아니다. 정지 이전에 노드 밖으로 전달된 의도나 이미 서명·전송된 거래를 취소하지 못한다. 실제 서명/전송 포트를 붙일 때는 포트가 같은 트랜잭션 경계에서 프로그램 상태·예약·영수증을 다시 확인하고, 외부 권한·온체인 상태를 별도로 검증해야 한다. 현재 그 포트는 설치되지 않았다. 여기의 정지는 ISA의 `CANCEL` 거래 명령과도 별개다.

## 컴퓨터 구조와 사용자 원문 1~20 대응

| 원문 초점 | 지금의 코드 / 남은 경계 |
| --- | --- |
| 1 Agent→Machine | `compile_program`이 자유 문장을 받지 않고 검토된 IR만 실행. 매 이벤트 모델 호출 없음. |
| 2 Economic CPU/ISA | v1 명령에 v2 `GUARD`, v3 `BRANCH`와 명시적 `HOLD`를 추가했다. 명령 계약·전이 그래프는 버전 있는 `spec.py`에 모으고 195개 적합성 벡터를 생성했다. `HEDGE/SWAP/BORROW/REPAY/CANCEL`은 예약 후 거부. |
| 3 Compiler→Machine | 모델 제안→비신뢰 추론 심사→별도 로컬 등록→정적 컴파일, 상태, 커널, 불변조건, 시뮬레이션, 영수증 연결. 서명된 다중 상품 basket에서 서명 전 금고 batch 인자로 내리는 compiler는 구현. 자연어 충실성 검증·거래 인코딩·실제 전송은 미구현. |
| 4 State graph/StateDelta | `EconomicState`와 시퀀스·이전 root로 연결되는 `StateDelta`. 소스 adapter와 세계 규모 state graph 미구현. |
| 5 자유도 축소 | 타입·시각·단위·출처·비용·정해진 전이 검사; 출처 진실성과 체결 불확실성은 제거됐다고 주장하지 않음. |
| 6 Kernel 중심 | 추론 초안은 자본/프로토콜/관측 범위 검증을 거쳐도 검토 대상으로만 남음. 예측 모델을 결재권자로 두지 않고 Kernel이 등록된 프로그램과 불변조건을 평가. |
| 7 Invariants | 잔액·소유자·네트워크·기간·프로토콜·자본·노출·손실·가격/quote 신선도·사후 수식 검사. 체인 adapter 미검증 상태의 실행 자체는 차단. |
| 8 Micro-function | 정규화·컴파일·가격 선택·비용·시뮬레이션·검증·저널·정산 경계를 별도 모듈로 시작. |
| 9 Primitive library 확장 | 고급 opcode는 명시적으로 reserved; 구현/검증/버전 없이 동작하게 만들지 않음. |
| 10 AI의 세 역할 | 모델 출력을 검토하는 typed gateway와 미해결 상태 예외 요청함을 구현. 자연어 intent parser, 실제 Qwen 호출, unknown-state resolver worker, 예측 model plug-in은 미연결. 핫패스 LLM 호출 0. |
| 11 CPU/OS 비유 | 커널·ISA·상태 저장소·프로그램/직원·권한 sandbox·인터럽트형 delta·영수증으로 각각 코드 경계 형성. |
| 12 Blockchain settlement bus | `settlement.py`의 비지속형 비교와 별도로 `runtime.py`의 영속 승인·제출·확정·계정 재관측·분쟁 중지 전이를 구현. 진짜 체인 송신/확정 verifier 없음. |
| 13 Agent=프로세스 | `agent_id`별 exposure/loss와 프로그램 버전·예약. ALPHA/VAULT/WATCH 다중 직원은 같은 런타임 계약을 쓸 수 있음. |
| 14 Capital Sandbox | 자본·비용·노출·일일 손실 및 미결 의도의 동시 예약. 노드 로컬 정지 시 미전송 의도 예약 폐기. 온체인 키 탈취 격리는 아직 미구현이므로 실제 자본 격리 보증은 아님. |
| 15 Transition graph | v2 `GUARD`는 거짓일 때 `HELD/ABORTED/ESCALATED`로 종료한다. v3 `BRANCH`는 앞으로만 이동하는 분기형 DAG에서 두 배분안 또는 `HOLD`를 고른다. 선택되지 않은 경로는 실행하지 않는다. 관련 상태가 갱신되면 재평가한다. 일반 반복·병렬 실행 그래프는 미구현. |
| 16 Observe→Transition→Verify | StateDelta→활성 프로그램 선택→kernel→receipt. 무변화 또는 일시정지 프로그램에는 kernel 0호출. 결측/오류로 ESCALATED 되면 프로그램별 예외 항목을 중복 없이 남기고, 유효 상태 회복 시 닫는다. |
| 17 인간 선택의 코드화 | 유효 후보 중 최저 비용과 불변조건 판정만 코드화. 비용 목적의 경제적 적합성은 별도 문제. |
| 18 수천만 줄/버전 성장 | 기존 primitive의 의미를 바꾸지 않고 ISA 명세에서 벡터를 생성하는 첫 경로를 구현했다. 프로토콜 adapter·새 invariant는 별도 명세와 독립 증거를 붙여 확장해야 한다. 줄 수를 인위적으로 늘리지 않는다. |
| 19 Jev 비교 | 모델은 선택적 입력, 실행 의미론과 제약은 커널에 고정. |
| 20 이름/정체성 | `economic_machine` 패키지와 `economic-machine` CLI로 분리. |

## Challenge B: TRON 수익 계획과 USDD Vault

사용자가 제시한 Challenge B 비교 범위에 따라 `TRX_STAKE`, `TRX_STAKE_ENERGY`, `JUSTLEND_STRX`, `JUSTLEND_USDT`, `JUSTLEND_USDD`, `USDD_EARN`, `USDD_VAULT_STRATEGY`를 구별한다. Energy 임대·JustLend USDT·JustLend USDD·USDD Vault 종류가 입력 universe에서 누락되면 `ABSTAIN`한다. 상품이 존재해도 견적 만료, 출금 불가, 청산 위험, 부채 한도, 유동성 조건 때문에 제외될 수 있으며 그 사유를 돌려준다. **USDD Vault는 챌린지 필수 비교 대상이면서 담보를 맡기고 USDD를 발행·상환하는 부채 경로다.** [USDD 공식 Vault 열기](https://docs.usdd.io/user-guide/open-a-vault), [관리·상환](https://docs.usdd.io/user-guide/manage-a-vault).

Vault의 `minted_usdd`, 누적 수수료 USDD, USDD/USD 가격과 표시 부채가 정확히 일치해야 한다. 배치할 수 있는 발행 USDD 원금의 가치에 목적지 APY를 곱하고, 미상환 부채 가치에 안정화 수수료율을 곱해 뺀 다음 담보 가치로 나눈다. `net annualized bps = floor((deployed USDD value × destination APY bps − debt value × stability fee bps) / collateral value)`. 담보/부채 비율은 현재와 모든 입력 시나리오에서 청산 비율+안전 완충보다 커야 한다. 시나리오 손실 입력이 담보 하락 또는 부채 가격 상승을 숨기면 거부한다. 청산 패널티는 화면에 드러내지만 청산 상황의 기대 손익을 임의로 수익화하지 않고 그 기회를 제외한다. 발행 USDD를 JustLend USDD나 USDD Earn에 배치하는 것은 **별도의 합성 전략 가정**이다. Vault 자체가 그 APY를 지급한다는 뜻이 아니다.

`CONSERVATIVE`는 허용 후보 중 최악 스트레스 손실이 가장 낮은 안, `GROWTH`는 최소 배분 거리 조건을 충족하면서 추정 순수익이 가장 높은 안이다. 적격 Vault 포함 후보가 별도로 있으면 `USDD_VAULT_COMPARISON`을 추가하며, 없으면 `vault_comparison_status`와 제외 사유를 표시한다. USDD Vault가 모든 사용자 배분안에 들어가야 한다는 뜻은 아니다. `JUSTLEND_STRX`의 집계 APY에 Energy 임대 수익을 다시 더하지 않는다. 네이티브 TRX 스테이킹 출금일은 입력 체인 파라미터의 언스테이킹 대기일보다 짧게 표시하지 않는다. 이 파라미터와 Energy 견적도 신선도를 검사하지만 공급자 진실성은 인증하지 않는다.

`cases/economic_tron_yield_demo.json`은 10,000 USDT/30일의 **전부 합성된 TRON_SIM 입력**이다. Cherry에서 `PYTHONPATH=src:. .venv/bin/python -m economic_machine.cli tron-yield-plan --request cases/economic_tron_yield_demo.json`으로 배분안과 Vault 비교를 재현한다. 현재 계산은 단순 연율의 기간 선형 근사, 사용자가 제공한 상품 수익·비용·시나리오·용량의 제한된 격자 최적화다. TRON/JustLend/USDD 실시간 피드, 출처 서명, Vault 계약 읽기, 매수·발행·상환, 실제 수익 추적은 미구현이다. 특히 `USDD_EARN`의 TRON 경로와 조건은 검증되지 않았으므로 가상 입력 외에 고객 상품으로 표시해서는 안 된다.

## Cherry 검증

Cherry의 주 프로젝트 환경에서 저장소 전체 `unittest discover -s tests`는 **187개 실행, 실패 0개, 24개 건너뜀**이었다. 격리 계약 환경의 `test_economic*.py`는 **142개 모두 성공**했다. 격리 환경에는 데이터셋 테스트용 `torch`·`jsonschema`가 없어 전체 저장소 테스트는 주 환경에서 따로 실행했다. 서명된 3상품 배분안을 3개 child 주문으로 변환하는 CLI 왕복, Python/온체인 주문·batch digest 일치, 2개 주문의 원자적 성공·실패 원복·재실행 거부, 현재 검증자 epoch와 다른 child의 관측 유보를 확인했다. TRON_SIM 7종 상품의 330개 격자 후보 중 31개가 사용자 제약을 통과했고, 보수·성장·Vault 포함 비교안 3개가 나왔다. 이 수익/위험 수치는 합성 입력을 재생한 결과다. 격리 EVM 테스트는 같은 Solidity 소스를 실행하지만 TRON TVM 실행을 대체하지 않는다.

고정 SHA-256의 공식 TRON 0.8.20 컴파일러로 금고를 재컴파일해 **19개 함수·8개 이벤트·10,126바이트** TVM bytecode와 manifest를 검증했다. 금고 source SHA-256은 `ba93e25423a949da339ec30b0948ed549e88c15e0c11f735c9a9ed09b241c513`, bytecode SHA-256은 `a299b29092dd520eacf07d5f859a8e6f4e69cf7bf94048c725d9026a823e6eb5`다. 컴파일러 SHA-256은 `fa95cdeb30aaf521df75b87f57c6ffc1ee6fef5de190981a69cdb878da3a4e14`다. **TVM 실행 테스트·계약 감사·배포는 아직 없다.** 원격 노드의 레지스트리 관측은 자체 계약의 배포 상태를 증명하지 못한다. 과거 공개 Shasta 거래 관측은 이 프로젝트의 거래나 실금융 검증이 아니다.

현재 경제 코어는 Python 25개 파일 **5,591물리 줄 / 공백·`#` 주석행 제외 5,171줄**, Solidity 2개 파일 **753 / 공백·`//` 주석행 제외 639줄**, 대응 테스트 18개 파일 **4,318 / 공백·`#` 주석행 제외 3,976줄**이다. 계약 검증 스크립트 2개는 **273 / 공백·`#` 주석행 제외 255줄**이다. 이 줄 수는 규모 기록이며 품질·보안성 증명이 아니다.

## 다음 의미론 우선순위

1. **취소·재시도:** v3는 bounded forward-only control-flow DAG를 구현했다. 루프/병렬/동적 호출은 미지원. `CANCEL/REPAY/BORROW/SWAP/HEDGE`는 상품별 전제조건과 실행 증거가 생길 때 순서대로 구현.
2. **포트폴리오→프로그램 접합:** 명시된 격자의 완전 열거와 선택 결과를 정책·상태·원천 식별자·상품별 금액에 묶는 비실행 commitment는 추가했다. v2는 주어진 시나리오의 hedge 손익과 공통 factor 노출 한도를 계산한다. 이를 실제 다중 상품 `EconomicProgram`으로 내리는 ISA/컴파일러, 사용자 승인 증명, 실행별 자본 예약, 원천 진실성 검증은 필요하다. 수익률·위험 입력의 진실성, 시나리오 밖의 상관 변화·tail risk, derivative margin/funding 경로, 연속 공간 최적화는 미해결이다.
3. **정산 상태 접합:** durable `EXECUTION_LOCKED`와 검증 증거별 `AUTHORIZE/SUBMIT/FINALIZE/RECONCILE` 전이, 계정 대조 후의 로컬 잠금 해제, 실패·부분체결·재조직의 `DISPUTED` 전역 중지를 구현했다. 다음에는 실제 체인별 verifier, unsigned tx와 서명 페이로드 바인딩, 분쟁 해소의 독립 증거와 재개 전이를 구현해야 한다. 타입이 있는 basket 기록과 target의 1회 소비, policy별·자산별 basket 누적 한도, 레지스트리 RPC 관측은 추가했으나 오프체인 원본의 진실성, RPC 상태 증명, target이 실제 거래를 했는지, 서로 다른 자산의 환산 총량·지갑 전체 한도는 강제하지 못한다. 미배포 금고의 대상/자산 코드 해시 고정과 원자적 토큰 잔액 검사는 추가했지만, 격리 TVM 실행 테스트, 실제 체인별 verifier와 감사가 별도로 필요하다.
4. **상품 adapter 명세와 인증:** ISA와 같은 방식으로 상품별 상태 필드·단위·quote·시뮬레이션·불변조건을 선언하고, 그 명세에서 adapter 계약 및 적합성 벡터를 생성한다. 그 다음 source/quote/chain/verifier의 허가·시간·원천 독립성을 검증하고 vendor-specific state를 경제 상태로 변환. SPOT/PERP/LENDING typed adapter의 공통 입력 조립은 구현했고, 고정 공개키의 Ed25519 서명 검증은 추가했지만 공급자 실체·시장 진실성 검증, 어댑터 자동 생성·적합성 벡터는 아직 미구현. 모델 adapter와 자연어-IR 정합성 검증도 아직 미연결. 데이터셋 구축은 이 코어 범위 밖.
5. **성능 구현:** 이 Python 버전으로 의미론/테스트 벡터를 고정한 뒤 고객 전용 노드에서 병목을 측정하고, 그때 Rust/고정 레이아웃/ring/tile/NIC 최적화를 같은 replay corpus에 대해 구현. 공유 Cherry의 NIC/코어 설정은 바꾸지 않는다.
6. **실제 자본 권한:** 미배포 금고 소스는 basket별 고객 서명을 요구해 노드 단독 자본 이동을 제한하지만 지갑 탈취, 서명 UI 기만, 악성 대상/프록시 업데이트를 막았다는 증거가 아니다. 실제 owner 서명 통합과 감사, 제한 세션 키의 별도 설계가 필요하다.
7. **Challenge B 상품 연결:** TRON 직접 스테이킹·Energy 임대·JustLend·USDD Vault의 읽기 전용 공식/온체인 소스를 상품별 타입 입력으로 연결하고 가격·APY·수수료·출금 조건·신선도를 독립 대조해야 한다. Vault의 실제 담보 유형별 청산 비율/안정화 수수료/누적 부채, USDD 발행·배치·상환 경로, 실행 전 시뮬레이션과 실제 수익 영수증은 아직 없다. 실데이터의 적법한 사용권과 신뢰 경계를 확인하기 전에는 `CALLER_CLAIMS_NOT_VERIFIED` 표시를 유지한다.

수천만 줄 목표는 이 primitive/adapter/invariant/transition corpus가 장기간 확장될 때 의미가 있다. 현재 v0.28은 금융 실행 제품 완료 상태가 아니며, 진실성·체인 권한·사후 확인 없이 자산 이동을 허용하지 않는다.
