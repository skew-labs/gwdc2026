# PR 04 — 결정론적 TRON 배분안 비교와 실행 의도

2026-09-28. PR 03의 상품별 금액 계산 위에 **확정 Mandate + 원문 재생 snapshot + 명시적 가격·비용·출구·위험 가정 → 완전 탐색 → 보수형/성장형 2개 배분안 → prepare-only intent**를 구현했다. Qwen3 32B나 다른 LLM은 이 계산 경로에서 점수·비중·거래 순서를 정하지 않는다. LLM은 자연어를 구조화하는 추론 계층으로 남고, 여기서는 정수 bps grid와 Decimal 회계만 실행한다.

## 실행되는 경계

| 모듈 | 책임 |
| --- | --- |
| `economic_machine/plan_compiler.py` | 최대 4,096개 후보의 완전 열거, 후보 금액별 PR 03 회계 재계산, 전역 조건 검사, 두 계획 선택, hash/replay, prepare-only intent |
| `finance_service/plan_service.py` | 인증 tenant/owner/wallet/network, 확인된 최신 mandate, exact scoped `LIVE_READ` snapshot, session 만료, 미정산 capital hold를 검사하는 읽기 경계 |
| `scripts/verify_economic_plans.py` | Cherry에 보존된 실제 공개 snapshot을 합성 조건으로 두 번 재생하고 comparison/intent 동일성을 확인 |

`compare_plans(...)`는 모든 비중 조합을 빠짐없이 열거할 수 있을 때만 실행한다. 후보 수가 4,096개를 넘으면 일부만 계산해 최적이라고 부르지 않고 거절한다. 보수형은 최악 스트레스 손실을 먼저 최소화하고 같은 손실에서는 순수익을 최대화한다. 성장형은 순수익을 최대화하되 보수형과 요청된 L1 거리 이상 달라야 한다. 하나의 상품과 현금만으로도 서로 다른 두 적격 계획을 만들 수 있으며, 상품 수를 과거 2~8개 같은 임의 범위로 제한하지 않는다.

각 비중의 금액은 한 단위 견적을 선형 보간하지 않고 PR 03 계산기로 다시 계산한다. sTRX exit tranche는 후보 원금에 맞게 보존적으로 재배율하고, Vault는 담보 원금을 TRX/USDT 실제 ilk로 구분한 뒤 quote basis에 따라 발행 부채와 운용 USDD도 함께 재배율한다. 가격·금리·고정 수수료·대기 시간·자원 window는 임의로 배율하지 않는다.

## 다시 검사하는 사용자 조건

- 즉시 현금과 시점별 최소 출금 가능액
- 단건 금액, 전체 원금, 전체 비용
- 일 손실과 scenario별 스트레스 손실
- 허용 action, 차입 동의와 부채 한도, Vault 담보/청산 조건
- protocol 총 비중과 Vault에서 발행 USDD를 넣는 JustLend 목적지 비중
- TRX·USDD 등 가격 노출 상한
- 상품별 명시적 상한과 현재 비중에서의 turnover 측정

USDD Vault는 단순 예치 APY 상품으로 취급하지 않는다. 담보 → USDD 부채 발행 → 지정 운용처 공급 → 미래 부채/상환 준비금 → 담보 회수의 경로다. 차입 미동의면 상품을 목록에서 제거하지 않고 `BORROWING_NOT_CONSENTED` 횟수를 배분 결과에 남긴다. 두 계획을 만들 수 없으면 `NO_TWO_VIABLE_PLANS`와 조절 가능한 조건을 반환하며 intent를 만들지 않는다.

`compile_plan_intent(...)`는 선택한 plan, mandate policy/draft/revision, snapshot, 상품 identity/capability, 금액과 cashflow assumption을 hash로 묶는다. 결과는 `INTENT_PREPARED`, `execution_authority=NONE`, `signature_status=NOT_REQUESTED`, `chain_status=NOT_SUBMITTED`다. live quote/risk assumption, PR 06 transaction graph, PR 07 사용자 지갑 서명이 blocker로 남는다. 실행 capability도 현재 `UNSUPPORTED`이므로 상품별 blocker를 추가한다.

## 수정한 오류와 실패 처리

- Vault 담보 원금을 USDT로 고정하던 오류를 없애고 `TRX-A/B/C`와 `USDT-A`의 담보 자산을 구분했다.
- capability의 유효 상태 집합은 `SUPPORTED`인데 존재하지 않는 `VERIFIED`와 비교하던 오류를 수정했다.
- sTRX 원금만 바꾸고 출금 tranche를 바꾸지 않아 원금 보존 검사가 실패할 수 있던 경로를 수정했다.
- mandate의 일 손실 한도가 plan 단계에서 빠져 있던 것을 별도 계산·거절 사유로 추가했다. 음수 기초 수익의 원금 감소도 스트레스 손실 하한에 포함한다.
- snapshot에 적격 상품이 없더라도 snapshot/registry 최신성 검사가 생략되지 않는다.
- fixture나 다른 계정 scope, draft mandate, 만료 session, session보다 긴 intent, 미정산 capital hold를 서비스 단계에서 거절한다.
- snapshot, 비용, Vault 부채, mandate revision, plan/intent를 변경하면 replay가 실패한다. 입력에 LLM score 같은 추가 필드를 넣어도 거절한다.

## Cherry 검증

Cherry `/srv/skew/gwdc-financial-agent-20260924`에서 코어·계약·서비스 **312개**, 기존 pipeline **10개**, 총 **322개**가 실패·오류·skip 없이 통과했다. 계약 검증에는 프로젝트 cache의 고정 TRON/EVM solc 0.8.20을 썼고 SHA-256은 각각 `fa95cdeb...a4e14`, `0479d44f...da782`다. Mac에서는 소스 편집과 작은 검사·원격 조작만 했다.

실제 공개 응답은 PR 02의 `2026-09-28T10:49:17.903850+00:00` snapshot을 사용했다. snapshot hash는 `3756e04d7320715f0cca9667193720db26693845d4c13dd9691ccd4f4f489d06`이다. 합성 사용자 조건과 가격·비용·스트레스 가정에서 56개 후보를 전부 계산해 7개가 적격이었다.

| 계획 | 비중 | 현금 | 30일 순수익 가정 | 일 손실 가정 | 최악 스트레스 |
| --- | --- | ---: | ---: | ---: | ---: |
| Conservative | jUSDT 20% | 7,997 USDT | 0.54155 USDT | 5 USDT | 20 USDT |
| Growth | jUSDT 20% + jUSDD 40% | 3,994 USDT | 7.292508 USDT | 45 USDT | 180 USDT |

Vault는 차입 미동의 때문에 25개 후보에서 제외됐고 그 이유가 결과에 남았다. 두 번의 독립 실행에서 conditions/request/comparison/intent byte가 모두 같았다. comparison hash는 `7bdd4e78d96ff06bdd0ea54e9289ca011915fd0db1b08919a72aa5f3beb4e3e7`, intent hash는 `092784d7d0f246b91af6b091fe0055880aab0509302e8eeb5e9280713ce7892f`다.

작은 검토 증거는 `artifacts/pr04/`에 있다. 전체 입력·두 replay와 로그는 Cherry `data/verification/pr04-20260928/`에 보존했다. 신규 의미 코드 481줄, 테스트 311줄, 검증 도구 122줄이다.

## 아직 구현하지 않은 것

이번 결과의 가격·비용·출구·daily/stress bps는 명시적으로 hash에 묶인 **검증되지 않은 가정**이다. 최신 체결 견적이나 독립 risk oracle이 아니며 `assumption_status=EXPLICIT_UNVERIFIED_MARKET_AND_RISK_ASSUMPTIONS`로 표시한다. 현재 비중도 인증된 post-state reconciliation과 아직 연결되지 않았다. plan service는 미정산 hold가 하나라도 있으면 새 계획을 거절한다.

실시간 quote/risk adapter, 모델 후보 점수의 bounded 사용, 목표 비중→최소비용 transaction graph, TronLink 서명, broadcast, post-state/actual yield reconciliation, 영구 DB와 운영 API는 후속 PR 범위다. 이번 PR은 고객 서명·계약 배포·체인 전송·실자산 거래·예약작업을 만들거나 실행하지 않았다.
