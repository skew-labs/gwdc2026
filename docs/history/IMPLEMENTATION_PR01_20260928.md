> Historical record. Current product, setup and verified scope: [faat README](../../README.md).

# PR 01 — 확인된 사용자 조건과 서비스 계약

2026-09-28. **구현·Cherry 회귀 검증 완료.** GitHub 저장소는 [skew-labs/gwdc2026](https://github.com/skew-labs/gwdc2026)이며, 기존 구현은 `main`, 이번 변경은 `pr01-confirmed-mandates`에서 검토한다. 이 파일 묶음은 10PR 계획의 첫 변경 단위이며 나머지 9개 PR이나 실운영 완료를 뜻하지 않는다.

## 문제와 결과

기존 `Need`, `InferenceScope`, `BasketPolicy`, sandbox는 서로 다른 입력에서 만들어졌다. 이제 `MandateV1`에 금액·비율·시간·차입 동의·제약과 원문 참조를 저장하고, 사용자가 정확한 draft hash를 확인한 뒤에만 읽기용 호환 계약을 만든다. `3000 USDT`는 고정 금액이고 `3000 bps`는 원금의 30%라서 원금 변경 뒤에도 의미가 섞이지 않는다.

정책 수정은 새 revision과 `DRAFT`를 만들고 이전 revision을 `SUPERSEDED`로 바꾼다. 기존 unsigned 검토안을 같은 저장소 트랜잭션에서 무효화한다. 이미 자금 hold가 있다면 revision 변경·취소·만료로 해제하지 않는다. 실제 지갑 호출이나 체인 전송은 추가하지 않았다.

## 변경 파일

| 파일 | 책임 |
| --- | --- |
| `src/economic_machine/mandate.py` | 엄격한 Mandate 형식, 금융 단위, 원문 연결, 계정 범위, 정규화·정책/draft commitment |
| `src/economic_machine/capabilities.py` | network+contract+protocol/version+action 식별, token decimals, 단계별 UNKNOWN/UNSUPPORTED/SUPPORTED |
| `src/economic_machine/application.py` | 확인된 Mandate에서 기존 Need/InferenceScope/BasketPolicy/sandbox의 읽기용 투영 |
| `src/finance_service/context.py` | 인증 어댑터가 제공할 tenant/owner/wallet/network/session/trace 인터페이스 |
| `src/finance_service/repository.py` | 원자적 version 비교·rollback·범위 격리를 요구하는 repository port와 메모리 reference adapter |
| `src/finance_service/mandate_service.py` | 생성·확인·수정·취소·검토안 commitment·보수적 자금 hold |
| `cases/economic_mandate_demo.json` | 실제 사용자/지갑이 아닌 원문 근거 포함 합성 확인 폼 |
| `tests/test_economic_mandate.py` | 금융 단위·출처·hash·상품 capability·호환 경계 반례 |
| `tests/test_finance_mandate_service.py` | 교차 계정·동시 수정·잠금 충돌·만료·재생·저장소 변조 반례 |

서비스 패키지 `__init__.py` 외 기존 경제 코어와 과거 금고 계약은 변경하지 않았다. 데이터 수집·모델 학습·예약 서비스도 변경하지 않았다.

## 계약 의미

- 금액은 asset과 정확한 십진 문자열이며 float·boolean·쉼표·퍼센트 문자열을 받지 않는다. 비율은 `BPS` 정수, 기간은 초, 시점은 UTC다. 출금 조건은 해당 deadline까지 회수할 수 있어야 하는 **누적 최소액**이다.
- `capital`은 자산별 양수 금액을 보존한다. 여러 자산의 가치 평가가 필요한 경우 legacy projection/hold는 `valued multi-asset snapshot required`로 거절한다. 토큰 decimals에 따른 최소단위 변환은 PR 02/06의 검증된 상품 정보가 필요하다.
- 차입 `null`은 미확인 초안으로만 보관한다. 확인 단계에서는 거절한다. `false`/`null`을 부채 금액 또는 opcode를 보고 `true`로 바꾸지 않는다. 부채 한도와 차입 동작은 명시적 동의를 요구한다.
- 원문 메시지와 각 조건의 인용구를 묶는다. 실제 문구의 존재만 검사하며 의미 해석이 맞다는 증명은 아니다. 최종 확인은 원문까지 포함한 draft hash를 대상으로 한다. 기본값은 사용자가 보는 확인 폼에 드러내고 확인해야 하며 모델이 몰래 채우지 않는다.
- `policy_hash`는 정규화한 금융 조건과 tenant/owner/wallet/network에 묶인다. trace/revision/원문 표현만 달라지면 정책 hash는 유지될 수 있지만 draft hash는 바뀐다. 숫자 표현의 뒤쪽 0, JSON key 순서, 집합 순서, UTC `Z/+00:00` 차이는 정규화한다.
- 지갑/계약 주소의 내부 표현은 `41` 접두어를 포함한 TRON hex다. Base58 변환과 소유권 검증은 인증/RPC 어댑터 책임이다. 형식 검사가 지갑 소유 증명은 아니다.
- `ProductCapabilityV1`의 근거 hash는 검증 보고서 참조다. hash 문자열만 존재한다고 실지원이 입증되는 것은 아니다. read 성공을 simulate/execute/reconcile 지원으로 승격하지 않는다. ID는 상품의 버전/동작을 구별하고 전체 capability hash는 decimals와 지원 선언 변경도 감지한다.

## 상태와 동시성

`create → DRAFT → confirm → CONFIRMED → revise → SUPERSEDED + 새 DRAFT`를 지원한다. 초안 수정도 이전 초안을 `SUPERSEDED`로 보존한다. draft/confirmed 상태 모두 취소할 수 있으며 `REVOKED`를 재활성화하지 않는다. 같은 Mandate ID를 다시 생성할 수도 없다.

API body의 scope는 인증 컨텍스트와 정확히 일치해야 한다. repository key에도 tenant/owner/wallet/network/mandate ID가 들어간다. trace는 인증 어댑터가 제공하는 요청 trace와 일치해야 한다. 시간은 body에서 받지 않고 서버 clock port에서 읽는다.

모든 쓰기는 aggregate version의 compare-and-swap을 요구한다. 상태 확인·변경·unsigned 무효화·hold 생성은 한 트랜잭션이다. 충돌 시 한 작업만 commit되고 다른 작업은 `VersionConflict`를 받아 새 상태를 읽어야 한다. revision 번호와 aggregate version은 다르다. 확인·검토안 추가·hold만으로도 aggregate version은 증가한다.

`prepare_review`는 plan/state hash에 묶인 **검토안 placeholder**다. plan 재생·거래 그래프·지갑 서명은 아직 없어 세 blocker와 `execution_authority=NONE`을 반환한다. 새로운 세션은 이전 세션의 unsigned 검토안을 재사용할 수 없다. `hold_review`는 이후 지갑 연결 경계가 사용할 보수적 예약 hook으로, 외부 실행 권한을 주지 않고 가용 예산만 줄인다. 기존 경제 코어의 SQLite execution lock과 자동 연결되지는 않았다.

hold는 단건·누적 금액, 건별 비용, 즉시 현금과 함께 같은 base asset에서 계산한다. 모든 이전 revision의 미결 hold를 합산한다. 시간 만료가 체인 실패의 증거가 아니므로 hold에는 TTL 자동 해제가 없다. 원본 대조가 있는 release/정산은 PR 08에서 구현해야 한다. 현재 한 Mandate 안의 한도이며, 같은 지갑의 여러 Mandate 간 전체 잔액 예약·직렬화는 PR 08/09가 필요하다.

## 기존 계약 연결과 남은 부분

`compile_review_projections`는 확정된 record의 hash와 유효성을 다시 검사하고 기존 계약의 값을 같은 Mandate에서 만든다. 정책 전체와 `FULL_MANDATE_PLAN_REPLAY` 등 미검증 경계를 함께 반환한다. 기존 sandbox나 BasketPolicy에 표현할 수 없는 시간별 회수·노출·부채 조건을 적용했다고 주장하지 않는다. 1일 미만 기간은 legacy Need에 억지로 반올림하지 않는다.

legacy `/api/plan`은 여전히 기존 연구용 읽기 비교이고 `/api/approve`는 여전히 403이다. 공개 mandate 쓰기 endpoint를 이 PR에서 열지 않았다. PR 04에서 모든 조건을 재생한 계획과 실행 의도를 연결하고, PR 05에서 Qwen3 32B의 초안 입력을 연결하며, PR 09에서 실제 인증과 PostgreSQL transaction/outbox를 연결한다. 그 뒤 legacy 쓰기 경로를 단일 서비스로 전환한다.

`AuthenticatedContext`를 생성하는 것만으로 인증된 사용자가 되는 것은 아니다. 현재 구현은 **신뢰된 인증 어댑터가 제공할 인터페이스**와 그 경계를 검증하는 테스트다. 메모리 reference repository는 프로세스 종료 후 유지되지 않으며 여러 worker 간 조정도 제공하지 않는다. 실운영 연결에는 영구 저장소·소유권 검증·잔액 및 결과 대조가 필요하다.

## Cherry 검증 증거

검증 호스트/경로: `84.32.71.168:/srv/skew/gwdc-financial-agent-20260924`. 기존 `cache/contract-test-venv`와 기존 TRON/EVM 컴파일러를 사용했다. Mac에서 빌드·테스트·컴파일은 실행하지 않았다.

- 신규 단위/경계 테스트 **42개**, 기존 경제 코어 **142개**, 기존 서버 검사 **1개**: **185개 통과, 실패/오류/건너뜀 0개**.
- 최종 회귀 실행 시각: `2026-09-28T09:20:57.790148+00:00`.
- 원격 로그: `data/verification/pr01-20260928/core-regression-tests.log`.
- 원격 요약: `data/verification/pr01-20260928/core-regression-summary.json`.
- 변경 파일 hash와 코드/테스트 줄 수: 같은 디렉터리의 `source-manifest.json`.

저장소의 `artifacts/pr01/`에는 이 로그·요약·manifest의 작은 사본을 보관한다. manifest는 검증 당시 스냅샷이며 이후 GitHub 게시 문구를 바꾼 문서의 hash와는 다를 수 있다. 게시 과정에서 실행 소스와 테스트의 hash가 그대로인지 별도로 대조한다.

회귀 검사에는 기존 v1/v2/v3 프로그램, signed evidence, settlement lifecycle, TRON 읽기 검증, 기존 registry/vault 격리 검사가 포함된다. 이번 PR의 변경으로 새 실제 계약 배포·TVM 실거래·고객 서명 증거가 생긴 것은 아니다.

주요 실패 반례는 금액/비율 혼동, 미동의 차입, 원문 없는 조건, 다른 계정 본문, 확인 hash 교체, stale revision, 두 수정 요청 충돌, 수정과 hold의 경합, 재로그인 후 이전 카드 재사용, 만료 시 잠금 해제, 저장 후 amount 변조, 트랜잭션 도중 예외다.

## GitHub PR 설명

제목: `feat: unify confirmed mandates and service contracts`

사용자 조건이 여러 legacy 입력으로 흩어져 정책 수정 후 오래된 검토안이 재사용될 수 있는 경계를 정리한다. 원문에 연결한 typed Mandate, 정확한 확인 commitment, 계정 범위, version 비교와 unsigned 무효화를 추가하고, 미결 자금 hold는 수정·취소·만료에도 보존한다. 기존 코어 계약에는 읽기용 projection으로 연결하며 실행 권한을 부여하지 않는다.

Cherry에서 경제 코어·신규 서비스·기존 서버 185개 테스트가 모두 통과했다. PostgreSQL 영구 저장소, 실제 세션/지갑 소유 증명, 계획 전체 재생, 체인 전송과 정산은 후속 PR 범위다.
