> Historical record. Current product, setup and verified scope: [faat README](../../README.md).

# PR 11 — PostgreSQL 16 운영 상태와 서비스 재시작 복구

2026-09-29. PR09의 SQLite 기준 구현과 PR10의 고객 workspace를 실제 PostgreSQL 16 상태 저장 경로에 연결한다. 이 PR은 Cherry의 격리 PostgreSQL 인스턴스에서 API·worker 역할 분리, RLS, 원자적 journal, 작업 lease, 동시 claim, 서비스 재시작 복구를 검증한다. 운영 배포, 고객 서명, 체인 전송, 자산 이동은 수행하지 않는다.

## 런타임 경계

`PostgresOperationalRepository`는 API DSN과 선택적인 worker DSN을 분리한다. 웹 API entrypoint는 `FINANCE_SERVICE_POSTGRES_API_DSN`만 허용하고 worker DSN이 주입되면 시작을 거부한다. 따라서 고객 요청을 받는 프로세스는 전체 tenant를 순회하거나 작업 lease를 획득할 수 없다. worker의 cross-scope claim·routine scheduling·공용 관측 갱신은 별도 프로세스와 별도 DB 자격증명이 필요하다. 두 경로 모두 서명·broadcast 권한은 없다.

API transaction은 정규화한 tenant/owner/wallet/network scope hash를 transaction-local PostgreSQL 설정에 넣는다. `finance_service_records`, `finance_service_jobs`, `finance_service_routines`는 강제 RLS 정책을 사용하며, API 역할은 자기 scope만 읽고 쓸 수 있다. worker 그룹 역할만 명시적으로 cross-scope 접근할 수 있다. API 역할은 job/outbox를 생성하고 조회할 수 있지만 update·claim 권한은 없다.

## 지속 상태와 복구

레코드 변경, 작업 생성·lease·완료·재시도, 관측, routine 변경은 하나의 PostgreSQL transaction 안에서 상태와 hash-chain journal을 함께 기록한다. mutation 전에는 전역 advisory journal lock을 획득하고 전체 chain을 검증한다. journal이 변조되면 새 mutation과 `/healthz`가 실패한다. 레코드 갱신은 optimistic version을 검사하고 작업 claim은 `FOR UPDATE SKIP LOCKED`와 계정 단위 advisory lock으로 중복 lease와 같은 지갑의 병렬 작업을 막는다.

서비스 프로세스를 완전히 종료하고 같은 PostgreSQL에 다시 연결한 뒤 작성한 `USER_NOTE`와 `MONITOR_POLICY` 작업을 다시 조회했다. 동일 scope는 레코드와 작업을 읽었고 다른 scope의 레코드 조회는 409로 거부됐다. 재시작 전후 `/healthz`는 PostgreSQL 16, API scoped role, worker credential 미적재, journal 정상 상태를 보고했다.

`export_verified_snapshot`은 PostgreSQL transaction에서 서비스 테이블과 journal root를 canonical JSON으로 내보내고 snapshot hash를 다시 검증한다. 이것은 논리 증거 export이며 WAL archiving, point-in-time recovery, 암호화 백업, 복원 훈련을 대신하지 않는다.

## 마이그레이션과 실제 검증

`scripts/apply_postgres_migrations.py`는 migration 파일 SHA-256을 `finance_schema_migrations`에 기록하고 advisory transaction lock 아래 적용한다. 이미 적용된 version의 파일 hash가 바뀌면 즉시 거부한다. Cherry에서 Debian PostgreSQL 16.15 package를 시스템 설치 없이 격리 디렉터리에 풀어 실제 서버를 실행했으며, 비관리자 API/worker login 역할로 테스트했다. migration 재실행은 두 파일 모두 `ALREADY_APPLIED`로 끝났다.

검증 범위는 PR11 PostgreSQL 8개, 직접 연결된 PR10 workspace 9개, PR09 서비스 16개로 총 33개 테스트다. 실제 PostgreSQL transaction, API entrypoint의 worker 자격증명 거부, 4개 동시 worker의 서로 다른 job claim, lease 만료 복구, 재시작 지속성, RLS, migration hash drift, journal 변조 시 503을 포함한다. 변경 Python의 Ruff와 `py_compile`, SQL 적용, `git diff --check`를 별도로 실행한다.

## 남은 운영 공백

현재 전역 journal lock과 mutation 전 전체 chain scan은 correctness 우선 구현으로, 이벤트 수가 커지면 write throughput 병목이 된다. checkpointed journal segment 또는 Merkle accumulator, connection pool, statement timeout, metrics/alerts, TLS·password/secret rotation, managed backup/PITR, restore drill, schema rollback policy, load/soak/failover 검증이 아직 없다. 실제 Qwen trace, TronLink 서명, Nile/mainnet 상품 거래, USDD Vault 실제 capability, live post-state와 실현 성과도 이번 PR 범위 밖이며 두 트랙 전체 합격으로 표시하지 않는다.
