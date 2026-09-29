> Historical record. Current product, setup and verified scope: [faat README](../../README.md).

# GWDC TRON B × Furiosa A: 고객 노드형 FS1 대회 빌드

> 2026-09-28 역사 문서. BYON/고객 노드 가입은 최신 사용자 지시로 제외됐다. 현재 [통합 웹서비스 설계](TRON_FURIOSA_UNIFIED_ARCHITECTURE_20260928.md)와 [10개 PR](TRON_FURIOSA_10PR_PLAN_20260928.md)이 우선한다. 아래는 9/25의 구현·관측 기록이다.

2026-09-25. 이 문서는 [전체 FS1/BYON 설계](FS1_NODE_GWDC_ARCHITECTURE_20260925.md)를 이번 대회의 실제 코드·Cherry 배포·검증 항목으로 좁힌다. **컴퓨팅 소유자 = 사용자**다. 이번 한 사용자가 제공한 Cherry 999573은 설치·재현 시험 대상일 뿐, 우리 회사가 모든 고객의 금융 판단을 대신 처리하는 공용 노드가 아니다. 대회 밖 제품에서는 다른 고객의 Linux VM/서버에도 같은 runner를 설치하거나 동일 runner API에 연결한다.

## 실제 대회 배치

```text
심사자가 보는 Fomo/Grok Bot형 웹 화면 (ALPHA / VAULT / WATCH)
   │ ① 자연어 입력 / 조건 수정                 ▲ 계획 카드·근거·영수증·상태
   ▼                                          │
Qwen 32B/Kiln 해석 어댑터 ── typed Need 초안 ─┤  느린 경로: 사용자 요청 때만 토큰
   │                                         │
   ▼ 사용자 확인 → PolicyIR(version, hash) ──┘
   │
   ▼ SSH 설치 또는 고객 runner API (대회 노드: Cherry 999573)
┌──────────────────────── 고객 소유 노드 ──────────────────────────┐
│ TRON/JustLend/USDD 읽기 10분 → 원문+정규화 SQLite               │
│     → immutable change-case 배치 + 원천 hash 검증               │
│     → source/subject/metric 역색인 → 영향 정책만 선택            │
│     → 정확 비교/유보 → 무서명 DecisionReceipt + 계획 카드         │
│     → 승인 요구 경계 → 사용자 지갑 서명 → TRON testnet            │
│     → 확정 거래/이벤트 영수증                                    │
└───────────────────────────────────────────────────────────────────┘
```

**지금 실제 연결된 실선:** TRON 공식 공개 원천의 10분 수집, 시간당 검증된 change-case 생성, 고객 노드의 읽기 전용 정책 runner, 영향 정책 선별, 연구 비교와 유보 결정, 결정 해시/원천 참조/실행 금지 영수증. **현재 점선:** Qwen 실제 제공 endpoint 호출, 사용자 확인 PolicyIR 화면, SSH/API 가입 제품화, 정식 두 개 실행 가능 계획, 고객 승인·지갑 서명·TRON 테스트넷 전송/확정. 점선을 데모 성공으로 세지 않는다.

## 현재 노드에서 돌아가는 코드 경로

| 경계 | 실제 파일·상태 | 대회에서 증명하는 것 |
| --- | --- | --- |
| 수집 | `src/finagent/collect.py`, `store.py`, `gwdc-finance-collector.service` | TRON JustLend·USDD 공개 원천의 원문 해시, 수집 시각, 정확 단위, 유효 0/결측 분리. 10분 주기. |
| 변화 | `research/fdc/auto_cases.py`, `gwdc-finance-auto-cases.timer` | 스냅샷 간 정형 변화만 immutable JSONL로 기록. 같은 값 재수집은 빈 배치. 시간당 실행. |
| 고객 노드 실행 | `src/finagent/fs1_runner.py`, `gwdc-finance-fs1-runner.timer` | 원천 배치 해시 확인, PolicyIR v1 제한형, 역색인, 관련 사례만 재계획, 품질/신선도 실패시 `PAUSED`, 영수증. 시간당 :25 실행. |
| 배분 오라클 | `src/finagent/planner.py`, `research/fdc/formal_optima.py` | 정확 금액·현금 보유 조건 아래 **연구용** 대안/가정 제한 최적화. 수수료·출금·실행 가능성이 없으면 거래 판단을 내리지 않음. |
| 언어 모델 | `src/finagent/qwen.py`, `server.py` | 사용자 자연어를 `Need` 초안으로만 파싱; 키/정확한 endpoint 미설정 시 `MODEL_UNAVAILABLE`. 정기 감시에서 호출 0. |
| 실행 | 없음; `server.py /api/approve` 403 | 동의/지갑 서명/거래 경로 없이 실제 입금·상환·리밸런스가 나가지 않음. |

현재 `PolicyIR`의 실행 가능한 부분은 `policy_id`, `node_id`, `tron-mainnet-read`, `DEMO_SCENARIO`, `READ_ONLY`, `Need(asset, amount, liquid_reserve, horizon_days, risk)`, 버전/해시/의존성이다. 대회 정책 예시는 `cases/fs1_demo_policy.json`의 가상 1,000 USDT·500 USDT 즉시 유동성·30일이다. 이는 사용자 보유나 승인이 아니다. 정책을 다시 등록할 때 동일 본문은 idempotent, 500→800처럼 내용이 바뀌면 v2가 되고 v1의 현재 결정을 무효화한다. 정식 제품의 권한·출금 시점·비용 상한·상품 허용 집합과 사용자 확인 증거는 이 제한형 IR에 추가해야 한다.

runner의 첫 실행은 이전 원천 배치 전체를 **해시 확인 후 소비**하지만 정책 등록 전 사건으로 재계획하지 않는다. 이후 새 배치마다 `source_id / subject_id / metric_id`가 실제 정책 의존성과 겹치는지만 본다. 겹치는 사건 여러 건은 정책당 한 번으로 합친다. `run_once`마다 원천 신선도와 감시 대상 사실의 원문 일치를 검사해 지연/손상/품질 악화 때 `PAUSED` 영수증을 만든다. 현재 계획 비교는 `RESEARCH_COMPARISON_ONLY`; `execution_permitted=false`, `UNSIGNED_LOCAL`, `NOT_SUBMITTED`가 고정이다. 결정 해시는 체인 증명이나 서명이 아니다.

## 관측 증거와 성능 주장 범위

2026-09-25 Cherry 실측: 초기 1,067개 immutable 배치/12,820개 사례를 198ms에 소비, 정책 초기 비교 1회, 정기 LLM 호출 0회. 바로 다음 시스템 서비스 실행은 신규 배치 0/재계획 0/LLM 0, 93ms. 원천 miner를 한 번 수동 실행해 생성된 신규 30개 배치/358개 사례를 runner가 98ms에 소비했으며 **등록 이후 실제 관련 사례는 0개**라 재계획도 0회였다. 이는 단일 호스트의 그때 그 호출 시간이며 시장→결정의 end-to-end 지연이나 TPS 벤치마크가 아니다. 최근 24시간 과거 배치 표본은 828개/9,682 사례 중 이 데모 정책의 네 watched metric에 해당하는 것이 110 사례/55 배치였다. 이 과거 사건은 현재 정책의 실제 결정으로 다시 표시하지 않는다.

12:25 UTC 자동 timer에서는 새 배치 6개/사례 77개 중 **등록 이후 관련 사례 2개**가 정책 하나에 걸려 `SOURCE_CHANGED` 재계획 1회, LLM 0회, runner 내부 97ms를 기록했다. 새 DecisionReceipt `fa11ac90...`는 연구용 계획 `c64752da...`와 네 source snapshot을 연결했고 `UNSIGNED_LOCAL`·`NOT_SUBMITTED`·`execution_permitted=false`를 유지했다. 이것은 실제 원천 변화에 대한 작동 증거다. 전체 시장 반응 지연은 hourly miner와 API 수집 주기를 포함해 별도 측정해야 한다.

검증된 테스트는 무변화 0 재계획, 무관 상품 변화 0 재계획, 관련 변화 1 재계획, 결측으로 일시 중지/회복, 정책 v2 이전 결정 무효화, 배치 변조 차단, 거래 모드 요청 거부다. 테스트는 Cherry에서 실행했으며 Mac에서는 빌드·테스트하지 않았다.

## 대회 기준별 현실적인 데모 경계

| 심사 순간 | TRON B에 보여줄 것 | Furiosa A에 보여줄 것 | 남은 합격 조건 |
| --- | --- | --- | --- |
| 사용자가 “1,000 USDT, 500 유동성” 입력 | 금액·자산·기간·현금 조건을 분리, 부족값은 질문 | 실제 지정 Qwen 32B/Kiln 호출과 input/output 토큰·시간 로그 | 모델 ID/URL/권한 확인, 사용자 확인 정책 저장 |
| 상품 검색/비교 카드 | JustLend·USDD 원천, 기본 수익과 보상을 별도, 수수료/출금 위험과 두 배분 후보 | 설명만 느린 경로에 사용; 수치·판정은 노드 코드 | 비용·출금 약관·실제 경로를 확인해야 **두 실행 가능한 대안**으로 셈 |
| 조건 500→800 변경 | v2 정책, 두 계획의 전체 재계산과 부족 근거 표시 | 조건이 달라진 두 번째 모델 호출/토큰 로그 | 현재 typed `Need` v2는 구현, 대화/화면과 실모델 연동 미완 |
| 시장 감시 | 노드만 원천 변화 확인·영수증 생성·유보 | 반복 감시 LLM 0 토큰, 사용 흐름/전력 추정 근거 | 현재 시간당 case→runner 경계의 end-to-end 지연 측정 |
| 승인/실행 | `PREPARE_*`는 현금·비용·계약·지갑/네트워크 재검사 뒤에만 | TRON testnet 거래와 노드 로그/영수증 동일성 | 현재 없음. 실제 고객 서명 없으면 `APPROVE`는 403. 기록형 거래를 자산 예치라고 표시 금지 |
| 보유/수익 추적 | txid·확정 이벤트·전/후 잔액·기대/실제 차이 | 실행까지의 정확한 흐름 증거 | 실행 경로 뒤에야 가능; 지금은 역사/시뮬레이션 표기만 가능 |

심사 시 내부 상태는 상품 전면 UI와 분리한다. 사용자는 `ALPHA` 대화와 계획 카드, `VAULT` 승인/자금, `WATCH` 근거/상태를 보고, 내부 `source_id`, case hash, decision hash는 펼치기 가능한 증거 패널로 본다. 화면의 “구매”는 실제 서명 가능한 거래와 확정 검증이 연결되기 전에는 사용하지 않는다. Cherry 연결은 이번 사용자 소유 노드 한 개를 SSH로 제어한 사례다. 다른 고객의 SSH/API 셀프 온보딩 UI와 mTLS 연결은 아직 없다.

## 9월 30일 제출 전 기술 순서

1. **정식 PlanEvidence:** JustLend/USDD의 상품별 수익 단위·보상 조건·출금 제한·네트워크 수수료·승인 경로·주소를 정본 출처로 검증한다. 모르는 항목은 차단한다. 여기서 두 대안이 실제 가능한지 결정된다.
2. **Qwen 실계측:** 참가자 제공 Qwen 32B/Kiln endpoint, 모델 ID와 권한을 확인한 뒤 `interpret`의 실제 성공 호출을 남긴다. 사용자 문장을 typed IR 초안에 연결하고 확인 UI를 붙인다. token/latency는 계측, energy는 제공 수치 또는 명시적 가정으로 분리한다.
3. **BYON 가입 제품 경계:** SSH는 설치/진단 전용 제한 계정으로 쓰고 설치 후 고객 노드가 outbound 상태/명령 채널을 연다. 이미 설치된 runner는 범위 제한 API 키로 연결한다. node_id, revocation, heartbeat, `PAUSED` 상태를 UI에 노출한다. Cherry 단일 노드는 가입 검증 1건이지 다중 attestation 네트워크가 아니다.
4. **테스트넷 승인 경로:** 계획/정책/상태 해시와 실행 전 재검사를 연결한 unsigned tx를 만들고, 사용자 지갑이 서명한 후 broadcast 및 confirmed receipt/event를 검증한다. 실제 JustLend/USDD 테스트넷 예치가 불가능하면 별도 기록 계약 거래와 모의 포지션으로 정확히 구분한다.
5. **수치 벤치마크:** 10분 수집/시간당 사례 생성/runner 반응 시간을 분리해 p50/p95를 기록하고, 변경 없는 기간의 background token 0과 LLM 전체 루프 기준선을 비교한다. 공유 Cherry에 AF_XDP·코어 고정·FPGA를 적용하지 않는다. 고객 전용 노드에서 병목이 측정되었을 때만 타일/하드웨어 최적화를 옵션으로 낸다.

이 순서는 대회 제출을 위해 실행할 작업 명세다. 현재 설치된 것은 1~5번의 완료가 아니며, 자산 이동·정책 권한 확대는 하지 않는다.

## 출처

- [GWDC 행사/일정](https://luma.com/be2le0l0), `GWDC Korea Hackathon_ TRON Challenge Brief.pdf` 2쪽, [Furiosa A 공개 브리프](https://docs.google.com/document/d/13qh7oePGl7Flrl-Zh_A6hfr02L266PvS/edit).
- [TRON 서명/브로드캐스트 단계](https://developers.tron.network/docs/api-signature-and-broadcast-flow), [확정 이벤트 API](https://developers.tron.network/reference/get-events-by-contract-address).
