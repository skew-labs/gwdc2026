# PR 05 — Qwen3 32B 의도 초안과 사용량 경계

2026-09-28. PR 01의 확인된 Mandate와 PR 04의 결정론적 계획 사이에 **호스팅 Qwen3 32B 의도 초안 계층**을 추가했다. 모델은 사용자 문장에서 금액·단위·기간·즉시 유동성·출금 조건·명시적 차입 동의와 부채 한도를 구조화할 수 있다. 배분 비중, 상품 선택, 리스크 한도 확대, 사용자 확인, 서명, 전송은 할 수 없다.

## 흐름

```text
한국어 사용자 입력
  -> Qwen3 32B hosted provider (bounded request)
  -> strict FinancialIntentDraftV1 validation
  -> 원문에 실제 존재하는 quote 확인
  -> 기존 confirmed Mandate에 적용 가능한 candidate 구성
  -> 사용자에게 candidate hash와 누락 질문 제시
  -> 기존 MandateService에서 별도 revise + exact-hash confirm
  -> PR04 deterministic plan recomputation
```

`IntentService.propose_revision(...)`은 저장소의 최신 **CONFIRMED** mandate와 인증된 tenant/owner/wallet/network 범위를 읽는다. 모델 요청 hash는 scope, mandate ID/revision, policy hash, 사용자 문장을 함께 묶는다. 같은 요청은 메모리 cache에서 재사용하여 두 번째 provider 호출이 0회다. PR 09 전까지 cache와 usage store는 단일 프로세스 reference adapter이며 다중 인스턴스 영속성은 주장하지 않는다.

모델 응답이 유효해도 결과는 `DRAFT_READY`, `execution_authority=NONE`, `chain_status=NOT_SUBMITTED`다. 저장소의 기존 confirmed mandate는 바뀌지 않는다. 사용자가 정확한 candidate hash를 확인한 뒤 기존 `MandateService.revise`와 `confirm`을 통과해야만 PR 04 계획 입력이 바뀐다.

## 모델 출력 계약

허용 patch는 다음뿐이다.

- `capital`: 자산과 정확한 decimal string 금액
- `base_asset`
- `risk_profile`
- `horizon_seconds`
- `immediate_cash`: `AMOUNT` 또는 `BPS`
- `withdrawals`: 시점과 최소 회수 가능액
- `borrowing_consent`
- `max_debt`: 자산과 정확한 decimal string 금액

각 patch field에는 사용자 원문에 그대로 존재하는 quote가 필요하다. 없는 숫자를 채우거나 quote를 꾸미면 거절한다. 차입 허용인데 최대 부채가 없으면 candidate를 만들지 않고 고정된 질문을 반환한다. 서명·전송·도구 호출·allocation 같은 추가 필드는 exact-key 검증에서 거절된다. 모델이 반환한 candidate가 기존 한도·자산·기간과 충돌하면 `CANDIDATE_REJECTED`이며 저장소는 변하지 않는다.

## provider와 비용 경계

`OpenAICompatibleQwenProvider`는 서비스 소유 `GWDC_QWEN_BASE_URL`, `GWDC_QWEN_API_KEY`, `GWDC_QWEN_MODEL_ID`만 읽을 수 있는 구성 객체를 제공한다. HTTPS, Qwen3 32B 모델명, 35초 이하 timeout, 최대 1회 retry, 1,600 이하 output token budget, 128KB 응답 한도를 검사한다. 429, timeout/network, malformed response, tool call, 지원하지 않는 모델을 구별해 bounded failure로 반환한다.

사용량 receipt는 provider/model/revision/request ID, request/policy/response hash, nullable input/output token, latency, attempt, cache 상태, energy 계측 상태만 가진다. prompt, completion, hidden thinking, credential은 저장하지 않는다. provider가 usage를 주지 않거나 잘못된 형식으로 주면 `null`이며 `0 token 성공`으로 바꾸지 않는다. 에너지는 `MEASURED`, `ESTIMATED`, `UNMEASURED`를 구별하고 현재 provider 경로는 `UNMEASURED`다.

## ModelOpinionV1

`economic_machine.inference`에는 장래의 예측 모델을 위한 `ModelOpinionV1` 계약을 추가했다. 실행 모드는 `SHADOW` 또는 `NOT_USED`뿐이다. input root와 validity를 검사해도 `optimizer_effect=NONE`, `execution_authority=NONE`이다. deterministic baseline은 명시적으로 abstain하며 expected return/risk/liquidity 값을 모두 `null`로 둔다. 위험 부재를 위험 0으로 바꾸지 않는다.

## 검증 범위

사용자 요청에 따라 이미 통과한 전체 322개를 반복하지 않았다. Cherry에서 다음만 검사했다.

- PR 05 신규/수정: Qwen strict schema, provider failure/retry, usage receipt/cache, ModelOpinion, IntentService
- PR 01 직접 연결: mandate revision/confirmation/tenant isolation/hold lifecycle
- PR 04 직접 연결: confirmed condition 변경 뒤 plan commitment와 계획 내용 변경, 기존 plan service 경계
- 기존 inference 직접 연결: model proposal이 등록·실행 권한을 얻지 못하는 경계

최종 명령과 결과는 `artifacts/pr05/targeted-tests.log`, 파일 hash와 의미 줄 수는 `artifacts/pr05/source-manifest.json`, 결과 요약과 live gate는 `artifacts/pr05/verification-summary.json`에 보존한다.

## 아직 충족하지 않은 것

Cherry 환경에는 `GWDC_QWEN_*`, `KILN_*`, `FURIOSA_*` 구성 키 이름이 없었다. 따라서 실제 Kiln/Qwen3 32B 요청 ID·token·latency를 가진 성공 trace는 이번 PR에서 만들지 않았고 **live acceptance는 미달**이다. mock 성공은 transport/contract 검증일 뿐 실제 provider 성공 증거가 아니다. `contracts/model_binding.template.json`도 endpoint, credential env name, model revision을 null로 유지한다.

신규 모델 학습, NPU energy 계측, 외부 read tool, transaction graph, 고객 서명, 계약 배포, broadcast, 실자산 거래, 예약작업은 수행하지 않았다. 실제 서비스 HTTP 연결과 영구 DB/분산 cache는 후속 PR 범위다.
