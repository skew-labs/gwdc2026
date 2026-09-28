# TRON 실관측 데이터 릴리스와 학습 경계

2026-09-24. 데이터셋·아키텍처 작업만 수행했다. Cherry 격리 경로
`/srv/skew/gwdc-financial-agent-20260924`에서 공개 읽기 API를 조회했다.
사용자 지갑, 서명, 구매, 거래는 다루지 않았다.

## 확인된 릴리스

릴리스 ID: `2ae352127c264661d1f57df6cd5364f004b79337286215518c12bd8c21292565`

| 원천 | 실제 관측 | 범위와 의미 |
|---|---:|---|
| [USDD TRON 연간 이력](https://docs.usdd.io/developers/usdd-public-api) | 366개 일별 관측 | 2025-09-23 16:00–2026-09-23 16:00 UTC. 담보가치(USD), 부채·발행량·공급량(USDD), Earn APY(연율 소수) 등. `statisticTime`은 공급자가 기록한 통계 시각이다. |
| [TronGrid 확정 계약 이벤트](https://developers.tron.network/reference/get-events-by-contract-address) | JustLend jUSDT 400개, jUSDD 400개 | jUSDT 2026-09-20 16:05–09-24 04:57 UTC, jUSDD 09-21 11:25–09-24 04:30 UTC. `JTokenStatus.totalCash` 원시 정수를 공식 JustLend 상품 목록의 underlying decimals로 변환했다. `only_confirmed=true` 필터로 조회했으나 자체 노드/영수증의 독립 확정 검증은 아니다. |
| 기존 [JustLend 공식 API 및 계약 목록](https://docs.justlend.org/developers/apis/) | 백그라운드 주기 수집 계속 | 상품 주소·상태·underlying 주소/decimals, 공급 연율·시장 현금. API의 수집 완료 시각과 실제 시장 사건 시각은 구분한다. |

총 **1,166개 실관측 행**을 `data/real-tron/<릴리스 ID>/observations.jsonl`에 저장했고,
동일 디렉터리의 `raw/`에 응답 원문 5개와 `manifest.json`을 보존했다.
[`validate_real_tron.py`](fdc/validate_real_tron.py)가 **1,166/1,166개 행**을
[`실관측 스키마`](../contracts/tron_observation_v1.schema.json) 및
응답 해시·JSON 위치·단위·계약 주소·시각과 다시 대조했다. 같은 상품의 이벤트
중복도 차단한다. 각 행은 `event_time`과 `source_available_at`을 따로 담는다.
연간 이력은 2026-09-24에 처음 내려받은 과거 데이터이므로, 과거 날짜에
에이전트가 이 값들을 실제로 알고 있었다는 의미로 쓰지 않는다.

TRON 관측 수집 코드는 [`real_tron.py`](fdc/real_tron.py)다. 공식 JustLend
목록에서 활성 jUSDT·jUSDD 주소와 underlying 자릿수를 먼저 확인한 뒤,
USDD TRON 일별 이력과 두 계약의 이벤트를 한정된 페이지 수로 읽는다.
원천의 0은 0으로, 단위는 USD·USDD·USDT·연율 소수로 구별한다.
공개 조회 가능 여부와 재배포/학습 권한은 다르므로 권한 상태는 `unknown`이다.

## 모델에 실제로 들어간 부분

[`train_real_temporal.py`](fdc/train_real_temporal.py)는 USDD 일별 366개 중
14일 입력으로 다음 날 담보가치·부채·공급량을 예측하는 자기지도 과제로
FDC의 **시계열 인코더만** 학습했다. 날짜순 246/53/53개의
학습/개발/홀드아웃 표본을 사용하고 정규화는 학습 구간에서만 계산했다.
체리의 `data/runs/real-tron-temporal-20260924`에 체크포인트와 보고서가 있다.
홀드아웃 상대평균절대오차는 담보가치 0.013215, 부채 0.021927,
공급량 0.040657이었다. 직전 값을 그대로 쓰는 기준은 각각
0.006675, 0.007993, 0.008042로 **더 좋았다**. 이 결과는 실제 데이터로
학습 경로를 실행했다는 증거이지 예측 개선의 증거가 아니다. 이력 전체를
한 번에 백필한 데이터이므로 원본의 과거 가용성·수정 이력도 알 수 없다.

금융 입력부의 실제 JustLend 후보에는 이제 `tron_mainnet`, `justlend_v1`,
underlying 주소와 decimals를 명시한다. [`api_draft_v3.py`](fdc/api_draft_v3.py)로
새로 만든 실데이터 사례는 기존대로 `excluded`, `unreviewed`, `rights_unknown`,
`abstain`이다. API에서 출금 약관과 공급 연율의 시장 사건 시각을 확인하지
못했으므로 배분 정답이나 구매 제안으로 승격하지 않았다. 실제 관측 1,166개에
**사람이 검토한 배분 정답은 0개**다. 다중 계획, 근거·질문·유보 헤드의 학습은
여전히 합성 데이터에 의존한다. 이번 실데이터 체크포인트를 해당 헤드에
합쳐 실전 성능이 개선됐다고 주장하지 않는다.

다음 실데이터 단계는 JustLend/USDD 상품별 출금·수수료·실행 경로의 공식
근거, 실제 사용자 조건(동의된 자료), 시점별 결과, 독립 검토자의 배분/유보
판정을 확보하는 것이다. GWDC 전용 API나 데이터 권한이 제공되면 원천 ID와
약관·시점 계약을 확인해 별도 릴리스로 추가한다. 그전에는 합성 배분 라벨을
TRON 실측 정답으로 바꾸지 않는다.
