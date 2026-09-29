# PR 12 — PostgreSQL 운영 복구와 부하 경로

2026-09-29. PR11에서 확인한 PostgreSQL 16 연결을 지속 운영 가능한 형태로 확장한다. 범위는 bounded connection pool, timeout, TLS, owner-only secret rotation, scope별 journal, 빠른 health, 전체 감사 timer, metrics/alerts, physical backup, PITR, standby promotion, schema 위험 gate다. 고객 서명·TRON 전송·계약 배포·자산 이동은 없다.

## 바뀐 실행 의미

API와 worker는 별도 pool과 별도 DB role을 사용한다. API entrypoint는 worker DSN을 거부하고 운영에서는 owner-only DSN 파일만 받는다. 운영 원격 연결은 `sslmode=verify-full`과 root CA가 없으면 시작하지 않는다. 파일 교체는 새 pool 연결과 role 확인이 끝난 뒤에만 활성화되며 기존 pool을 닫는다.

PR11의 전역 journal lock과 mutation 전 전체 chain scan을 scope별 stream/head로 바꿨다. 서로 다른 scope write가 독립적으로 진행된다. 일상 read와 health는 저장된 전체 감사 상태와 최신 stream/head를 확인한다. 전체 deterministic hash scan은 시작 시점과 5분 audit 작업에서 수행하고, 10분을 넘기면 health가 fail closed한다. journal UPDATE/DELETE/TRUNCATE 권한을 제거하고 rewrite trigger도 둔다.

## 실제 Cherry 증거

- 실제 PostgreSQL 16.15 pool 부하: 60초, worker 16개, scope 256개, 작업 37,345건, 오류 0건, 초당 622.214건, p50 24.111 ms, p95 40.251 ms, p99 50.877 ms, 최대 112.578 ms. 생성된 금융/체인 transaction은 0건이다.
- TLS clone: 평문 연결 거부, `verify-full` 성공, TLS 1.3, `TLS_AES_256_GCM_SHA384`, test CA의 SAN hostname 검증 통과.
- 복구 clone: `pg_verifybackup` 통과, restore point 이후 marker 제외, standby replay LSN 일치, primary fence 뒤 promotion, promoted write 성공.
- 실제 FastAPI 프로세스: PostgreSQL health와 supervisor check 통과. 서비스 재시작 없이 DSN 파일을 `pr11_api`에서 `pr12_api_rotated`로 바꿨고 generation 1→2 및 실제 role 변화를 확인했다.
- 전체 audit 작업: root와 head mismatch 0을 기록했다. systemd unit 문법 검사는 production 절대 경로를 검증 환경의 실행 경로로 치환한 사본에서 통과했다.

증거 원문은 `artifacts/pr12`에 있다. 테스트용 CA와 localhost 허용은 격리 TLS/서비스 시험에만 사용했다. 저장된 artifact에는 DSN, password, session secret, tenant, wallet이 없다.

## 운영 경계와 남은 외부 조건

verified physical backup 명령과 6시간 timer는 구현했지만 실제 off-host 암호화 목적지가 아직 설정되지 않아 production backup 성공을 주장하지 않는다. 복구 시간은 같은 Cherry의 작은 clone 결과라 production RTO가 아니다. 운영 배포 주소, 외부 TLS termination, 장기 보존과 alert 전달 채널도 배포 환경에서 정해야 한다.

이 PR은 Qwen/Kiln actual call, Furiosa 계측, TronLink 고객 서명, Nile/mainnet 상품 거래, USDD Vault 실제 capability, live post-state와 실현 수익을 만들지 않는다. 해당 항목은 현재도 제출 acceptance의 명시적 공백이다.
