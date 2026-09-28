# PR 03 — TRON 기간 순수익·출금·Energy·Vault 회계

2026-09-28. PR 02 위에 **확인된 Mandate + 원문 재생한 snapshot + 명시적 시나리오 가정 → 상품별 금액 계산**을 추가했다. PR 04의 배분 최적화에 제공할 계산 계층이며, 결과는 서로 독립적인 단일 포지션 비교다. 여러 행을 합산해 포트폴리오나 두 적격 배분안으로 해석하면 안 된다.

## 실행되는 코드

| 모듈 | 입력과 계산 |
| --- | --- |
| `yield_math.py` | ACT/365F의 초 단위 APR/APY, 수익 내림·부채/비용 올림, 보상 claim 시점·할인, 진입/출구/전환/네트워크 비용 |
| `liquidity.py` | 원금을 보존하는 회수 tranche와 누적 회수 곡선, 시장 cash 부족·알 수 없는 queue, 위임 lock→자원 복구→unstake 대기, 부채/비용 차감 |
| `resource_cost.py` | 유한한 Energy window의 사용·예약·임대·수령 용량, 순차 거래 소비, 전체 Bandwidth quota 선택 또는 TRX burn, fee_limit와 현금 burn 예산 분리 |
| `vault_accounting.py` | 담보 + 운용 USDD − 부채, 이전 누적 이후 수수료 추정과 기간 수수료, USDD 양쪽 가격 반영, 담보비율·완충·스트레스·상환 부족 |
| `tron_cashflow.py` | PR 01 조건과 PR 02 raw replay 연결, 상품·목적지 식별/단위/hash 바인딩, 예산·차입·노출·수수료·유동성 조건 점검, 결과 재생 |
| `scripts/verify_tron_cashflows.py` | Cherry에 보존된 실제 공개 응답을 합성 조건으로 재계산. 네트워크/서명/스케줄러 없이 실행 |

공개 호출은 `calculate_tron_cashflows(record, snapshot, assumptions, assembler=..., at=...)`와 `verify_tron_cashflows(...)`다. `record`는 repository에서 확인된 상태여야 하고 raw HTTP/LLM JSON이 확인 상태를 부여해서는 안 된다. 함수에는 거래 권한이 없으며 `execution_authority=NONE`을 반환한다. 웹/API 연결은 후속 PR 범위다.

계산은 별도 Decimal context(96자리)에서 수행한다. 입력은 최대 10^30, 소수 36자리의 유한 문자열로 제한한다. 투자 금액에는 underlying의 최소 단위를 적용한다. 금리의 명시적 convention이 없거나 연율이 -100% 이하이면 거절한다. 현재 비용 기준 통화는 USDT다. APR/APY의 연율 고정과 복리 가정은 미래 수익 보장이 아니다.

## 산식과 재계산 가능한 벡터

`t = 보유 초 / (365 × 86400)`.

- APR 수익: `P × r × t`.
- APY 수익: `P × ((1+r)^t − 1)`.
- 순수익: `기초자산 수익 + 기간 내 claim 가능한 할인 보상 − 전체 비용`.
- Vault NAV: `담보 기준가치 + 운용 USDD × USDD 가격 − 부채 USDD × USDD 가격`.
- Vault 기간 손익: `종료 NAV − 시작 NAV`; 발행 원금을 수익으로 더하지 않는다.

| 입력 | 기대 결과 |
| --- | --- |
| 1,000, APR 21%, 182.5일 | 수익 105 |
| 1,000, APY 21%, 182.5일 | 수익 100 |
| 1,000, APY -19%, 182.5일 | 수익 -100 |
| 담보 1,000 + 운용/부채 각 400, 운용 APR 20%, 부채 APR 10%, 1년 | 시작 NAV 1,000, 종료 NAV 1,040, 순수익 40 |
| 같은 조건에서 기존 부채 440 | 시작 NAV 960, 종료 NAV 996, 순수익 36, 상환 부족 4 |
| Energy 용량 100, 사용 10, 예약 10, 외부 임대 60 | 자기 거래에 쓸 수 있는 Energy는 20 |

`artifacts/pr03/arithmetic-vectors.json`에 실제 계산 출력이 있다. APR 테스트는 별도 Fraction 산식으로 대조하고 APY는 제곱근으로 직접 확인 가능한 벡터를 사용했다. 출금 곡선은 양수 예상 이익을 현금으로 쓰지 않는다. 음수 기초 수익은 회수 원금에서도 차감한다.

## 조건·원천 연결과 수정한 오류

- Vault 운용처의 독립 `destination_apy_bps` 입력을 새 경로에 두지 않았다. `justlend.v1.jUSDD`의 snapshot 금리와 계약 identity/hash를 사용한다. 운용처 관측이 바뀌면 Vault 수익 및 계산 hash가 바뀐다. 현재 USDD Earn 실행 운용처는 지원하지 않아 이유를 남긴다.
- 차입 미동의, 부채 상한, 최소 부채, 상품 전체 부채 상한, 담보 안전 여유, 스트레스 손실, TRX/USDD 가격 노출, protocol 비중과 목적지 공급 허용을 확인한다. Vault의 USDD 노출은 운용 자산과 부채의 **총액**으로 보수적으로 센다.
- 즉시 현금, 출금 기한, 왕복 비용과 Vault 상환 차액을 함께 예약한다. 예상 이자로 수수료나 상환 준비금을 미리 충당하지 않는다. 두 건 이상을 합친 전역 제약과 이미 미결된 거래의 누적 한도는 PR 04/09가 담당한다.
- sTRX 집계 수익에 native voting/rental 항목을 더하면 거절한다. 실제 0인 sTRX 수익률을 오류로 처리하던 legacy `tron_yield.py`도 수정했다. v1의 선형 proxy/연구 요청과 새 금액 계산 계약은 구별한다.
- Energy 외부 임대분을 자기 소비 가능량에서 뺀다. 임대 수입과 거래 비용 계산은 같은 window를 사용해야 한다. 수령한 Energy를 자기 소유 용량처럼 다시 임대하는 입력은 거절한다. 용량과 occupancy는 견적 가정이며 자동 재생성/임대 갱신을 가정하지 않는다.
- `fee_limit`은 자기 stake로 소비한 Energy도 포함한다. 추가적인 현금 burn 상한은 별도로 검사한다. 기존 계정의 보통 거래를 대상으로 하며 새 계정 활성화, 특수 토큰 수수료, deployer sponsorship 산정은 후속 preflight에서 처리한다. 입력 `caller_energy`에는 동적 Energy와 최종 caller 부담이 포함되어야 한다.
- API와 RPC가 충돌한 필드는 API를 선택하는 우회도 막는다. 원문 재생/단위 검증, 호출 시점의 snapshot·원천 receipt·registry 만료, mandate scope/hash를 확인한다. 없는 지표를 0으로 바꾸지 않는다.
- 관측 parser는 공급/차입 연율에만 음수를 허용하도록 수정했다. 현금·부채 등 수량에 음수를 허용하지 않는다. 음수 금리의 기간 계산도 동일 경로에서 가능하다.

## 검증과 증거의 범위

Cherry `/srv/skew/gwdc-financial-agent-20260924`에서 신규 52개를 포함해 총 **303개 통과**, 실패·오류·skip 0이다. 코어/서비스/서버 293개와 기존 원천 pipeline 10개이며 최종 종료는 2026-09-28 11:32:01 UTC다. `artifacts/pr03/core-regression-summary.json`과 로그를 참조한다. 로컬 Mac에서는 편집과 원격 조작만 했다.

PR 02의 `2026-09-28 10:49:17 UTC` 공개 snapshot을 원문에서 다시 확인해 계산했다. snapshot hash는 `3756e04d7320715f0cca9667193720db26693845d4c13dd9691ccd4f4f489d06`이다. 이번 검증은 **과거 실제 공개 응답 + 합성 사용자 조건·가격·비용/출구 가정**이며 새 실시간 견적이나 실제 고객 포지션이 아니다. 비교 예시는 30일이며 비용 차감 후 음수인 수익도 그대로 보존했다.

검증 입력·전체 출력은 Cherry `data/verification/pr03-20260928/`에 보존한다. 검토용 작은 요약, 산식 벡터, 파일 hash, 줄 수와 테스트 로그만 `artifacts/pr03/`에 올린다. 빈 줄·주석·docstring을 제외한 신규 소스 560줄, 테스트 422줄, 검증 도구 80줄이다. 기존 두 파일의 수정은 hash로 추적하며 그 파일 전체를 신규 소스 줄 수로 더하지 않았다.

아직 없는 것은 실시간 가격/exit quote, 개인 Vault의 정확한 현재 상환 금액, 모든 포지션을 합친 optimizer·정책 재검증, 거래 구성·서명·정산이다. Vault fee는 관측 연율의 명시적 forecast convention을 적용한 추정이며 정확한 온체인 per-second 누적/반올림 결과를 대신하지 않는다. API가 원천 블록/시각을 제공하지 않는 사실은 출력에 계속 남고, 계산 통과로 해당 관측을 실행 가능한 상태로 승격하지 않는다. 새로운 학습·수집 타이머나 Codex 예약작업은 만들지 않았다.

## 공식 참고

- [JustLend 연율/API 의미](https://docs.justlend.org/developers/apis/): 공급 이자와 별도 mining 보상, 각 수량의 단위를 구별하는 근거.
- [TRON 자원 과금과 fee_limit](https://developers.tron.network/docs/paying-for-resources): caller 부담과 자원/현금 한도, 동적 Energy의 구별.
- [USDD 공개 API](https://docs.usdd.io/developers/usdd-public-api): stability fee, 부채, 담보비율, dust와 ceiling의 의미. 개인 부채에 전체 시장 부채를 대입하지 않는다.
