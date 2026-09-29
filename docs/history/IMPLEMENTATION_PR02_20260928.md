> Historical record. Current product, setup and verified scope: [faat README](../../README.md).

# PR 02 — TRON 관측과 경제 상태 연결

2026-09-28. **공개 RPC 연결 문제를 수정하고 총 251개 회귀 검사를 통과했다.** [RPC 원인·수정·실제 성공 증거](RPC_FIX_PR02_20260928.md)가 최신 상태다. PR 01 위에 쌓는 변경이며, 실제 공개 원천 조회와 개인 지갑 fixture를 구별한다. 실행·서명·배포를 활성화하지 않는다. 아래 원천 수치와 233개 검사는 최초 검토 초안의 기록이다.

## 구현 결과

`원문 응답 → 출처·조회 범위·hash → 공식 상품 식별 → 필드별 스냅샷 → 원문 재생 → StateDelta`를 연결했다. 원천별 숫자만 복사하지 않고, 공급/차입 금리의 필드 의미와 주소·자릿수·네트워크까지 대조한다. API가 원천 시각이나 블록을 주지 않으면 조회 완료 시각으로 대신 채우지 않는다.

| 파일 | 책임 |
| --- | --- |
| `tron_sources.py` | 고정된 공개/지갑 읽기 URL, 주소 체크섬, 원문 hash, JSON 중복 키·비정상 수치 거부, 기존 Store 정규화 재생 |
| `tron_products.py` | 공식 디렉터리와 명시적 설정에서 network+contract+protocol/version+action 식별, USDD/USDDOLD·지분/원자산 구별 |
| `tron_rpc_snapshot.py` | 서버 관리 TRON 읽기, 원문 요청/응답 재생, solid block 전후 대조, 지갑·자원·담보 부채·소유권 확인 |
| `snapshot_assembly.py` | 9개 상품의 필드 대응, missing/error/valid-zero, 시간·원천 불일치, 보수적 상태 투영 |
| `finance_service/snapshot_service.py` | 인증 컨텍스트의 tenant/owner/wallet/network로 조회하는 읽기 경계. 실제 인증·영구 저장소는 PR 09 |
| `config/tron_product_registry.json` | 검토할 상품·USDD JOIN/core 주소·최신성 제한. 실행 비활성 |
| `tests/test_economic_tron_sources.py` | 38개 테스트와 여러 입력 변형을 통한 주소·단위·재생·지갑·블록·실패 반례 |
| `scripts/verify_tron_snapshots.py` | Cherry에서만 실행하는 1회 공개 조회 및 저장 원문 재생. 기존 수집·학습 타이머를 시작하지 않음 |

소스는 `src/economic_machine/` 아래이며 서비스 파일은 위에 별도 표시했다. 기존 정규화/저장소와 TRON 읽기 검증 함수를 재사용했고, 기존 경제 계약과 거래 경로는 수정하지 않았다.

## 금융 의미와 실패 처리

- JustLend V1의 jUSDT·jUSDD·jUSDDOLD를 서로 다른 계약으로 보관한다. legacy는 신규 공급 후보로 표시하지 않는다. V2는 V1 parser로 대체하지 않고 거절한다. `new_supply_allowed`는 디렉터리의 시장 상태이며 실행 가능 선언이 아니다. 모든 상품의 execute 단계는 UNSUPPORTED다.
- sTRX의 연율은 스테이킹과 임대 수익의 집계라는 역할을 보존한다. 직접 Energy 임대 비용/잔액과 합산해서 수익을 두 번 세지 않는다. 사용자 출금 API의 claimable/unstaking 합계는 읽지만 각 요청의 해제 시각은 UNKNOWN으로 남긴다.
- USDD JOIN은 담보 유형의 진입 어댑터다. 공개 API의 전체 부채·발행량·담보가치는 개인 Vault 잔액이 아니다. TRX-A/B/C·USDT-A의 부채 상한, 최소 부채, 안정화 수수료, 청산 담보비율을 분리한다. USDD Earn 연율도 별도 읽기 항목이며 실행 경로는 unsupported다.
- 개인 Vault reader는 명시적으로 주어진 최대 8개 ID에 대해 manager→Vat binding, owner 또는 registry proxy→owner, urn·ilk를 대조한다. Vat의 `ink`, `art`, 저장된 `rate/spot/line/dust`를 읽는다. 부채는 `art × rate / 10^45`다. **전체 Vault 열거와 마지막 rate 갱신 이후 미반영 수수료는 아직 해결하지 않았으며**, 이를 current total debt라고 표시하지 않는다. 이 경로의 증거는 합성 fixture다.
- RPC reader에는 잔액·native stake·위임/수령 자원·native unstake 금액/해제 시각·Energy/Bandwidth·수수료 읽기가 있다. 임대 자원과 자기 소유 자원을 섞지 않는다. full-node resource/fee 응답에는 확정 블록이 없으므로 실행용 상태로 승격하지 않는다.
- 공개 API의 0은 VALID_ZERO, 누락은 MISSING, 통신 오류는 ERROR다. 체인 파라미터는 **키가 있는 protobuf scalar의 생략된 값**만 0으로 해석하고 그 변환을 표시한다. 키 자체가 없으면 MISSING이다. 관련 없는 signed governance parameter를 금액처럼 양수로 강제하지 않는다.
- API 출처 시각/블록 UNKNOWN, 미래/오래된 응답, registry 만료, source receipt skew, RPC 블록 변화, 원천 간 값 불일치, fixture는 의존 값의 상태 승격을 막는다. 같은 공급자의 여러 API를 독립 oracle 투표로 세지 않는다. hash는 원문 동일성이지 금융적 진실이나 provider 독립성의 증명이 아니다.
- StateDelta는 소유자·네트워크·이전 root·순서를 묶고, 개인 정보에는 tenant/wallet을 포함한 state scope도 요구한다. `tron.input.*`에서 사라진 값은 MISSING으로 무효화한다. 이미 무효화한 시점보다 오래된 블록으로 유효 값을 복구하지 않는다. 계좌 잔액·기존 자금 hold·거래 권한은 갱신하지 않는다.
- Source capture와 RPC transport는 신뢰된 서버 어댑터의 입력이다. 사용자 JSON의 scope나 `LIVE_READ` 문자열만으로 원천 인증이 되지 않는다. SnapshotService는 fixture를 실고객 응답으로 내보내지 않는다.

## Cherry에서 확인한 초기 실제 원천 — RPC 수정 전 기록

원천 수집: **2026-09-28 10:18:16–10:18:22 UTC**. 최종 원문 재생: **10:20:11 UTC**. 공개 원천 9개가 AVAILABLE이며, 상품 9개·필드 76개에서 VALID 73개와 VALID_ZERO 3개를 얻었다. 실제 사용자 지갑은 입력받지 않았고 private API를 호출하지 않았다.

- 원격 최종 증거: `data/verification/pr02-20260928/run-03/`.
- `run-02/captures.json`에 보존한 실제 응답을 네트워크 없이 다시 검증했다. `run-03/live-manifest.json`에는 각 원문 hash, 필드/단위/JSON pointer, 조회/관측 시각, 실패와 유보 이유가 있다.
- Snapshot hash: `654e597a36cd7a98bd2fa6dcc897d6145bb4dd83f6e69b70f911432824c2c275`.
- **실행용으로 승격한 필드는 0개**다. 성공한 공개 API에도 원천 관측 시각과 확정 블록이 없기 때문이다.

실제 RPC는 첫 JustLend `getCash()`에서 중단됐다. API result는 true지만 `transaction.ret`이 `[{}]`이고, 예상 단일 ABI word 대신 3 words가 반환됐다. 이를 TVM 성공·정상 ABI 결과로 꾸미지 않았다. 기존 엄격한 검증기를 유지하고 `RPC_READ_INCOMPLETE`를 기록했다. 같은 batch의 미검증 잔액을 부분 성공으로 내보내지 않는다. 별도로 성공한 체인 파라미터 읽기는 유지된다.

이 초기 실패는 [후속 수정](RPC_FIX_PR02_20260928.md)에서 protobuf 기본값·구형 delegator의 반환 규약을 확인하고 공개 getter를 동일 호출로 묶어 해결했다. 수정 후 실제 관측은 87개 지표, 상태 투영 허용 3개이며 RPC 미완료 오류는 없다. 개인 지갑 소유권·전체 부채의 live 검증과 암호학적 상태 증명은 여전히 완료 범위가 아니다.

## 초기 테스트와 크기 — 최신 수치는 수정 보고서 참조

Cherry `/srv/skew/gwdc-financial-agent-20260924`에서 **총 233개 통과**: 기존 코어/서비스/서버 185개 + 신규 38개 = 223개, 기존 원천 pipeline 10개. 실패·오류·skip 0. 최종 종료 2026-09-28 10:20:57 UTC.

코어/계약 테스트는 기존 `cache/contract-test-venv`와 기존 solc들을 썼다. pipeline은 jsonschema가 설치된 기존 `.venv`에서 검증했다. 첫 합친 실행은 계약 환경에 jsonschema가 없어 pipeline 1개가 실패했으며, 초기 로그를 원격에 보존하고 원래 환경에서 다시 통과시켰다. 새 의존성을 설치하거나 Mac에서 테스트/빌드하지 않았다.

소스 **860줄**, 테스트 **411줄**, 검증 도구 **73줄**. 빈 줄·주석만 있는 줄·AST docstring을 제외한 물리적 줄 수이며 설정·문서는 합산하지 않았다. 생성 파일로 크기를 부풀리지 않았다. 파일별 SHA-256와 산정 방법은 `artifacts/pr02/source-manifest.json`에 있다.

`artifacts/pr02/`에는 source manifest, 최종 회귀 로그/요약, pipeline 로그, live manifest만 저장한다. 원문 전체와 SQLite는 Cherry의 검증 디렉터리에 보관한다. 이 PR에서 Qwen 호출, 데이터셋 학습, 공개 endpoint 배포, 사용자 서명, 자산 거래, 예약작업 재시작은 하지 않았다.

## 근거 문서

- [JustLend 공식 계약 디렉터리](https://docs.justlend.org/developers/contracts.json), [공식 API](https://docs.justlend.org/developers/apis/), [sTRX 인터페이스](https://docs.justlend.org/developers/staked_trx/).
- [USDD 공개 API](https://docs.usdd.io/developers/usdd-public-api), [배포 주소](https://docs.usdd.io/developers/deployment-addresses), [USDD V2 감사 보고서](https://usdd.io/USDD-V2-audit-report.pdf). Vault getter 구조는 이 보고서의 CDP Manager 설명과 [upstream CDP Manager](https://github.com/sky-ecosystem/dss-cdp-manager)에 근거하며, 배포 계약과의 live ABI 일치 확인은 남아 있다.
- [TRON constant call의 API/VM 결과 구별](https://developers.tron.network/reference/triggerconstantcontract). constant call 응답이나 읽기 성공은 실제 전송·정산 영수증이 아니다.
