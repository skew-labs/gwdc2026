# PR 09 — 인증된 지속 서비스, 복구 가능한 작업, 직원 routine

2026-09-29. PR 08의 실행·포지션 대조 결과를 여러 사용자의 지속 서비스 기록과 복구 가능한 작업으로 연결한다. 이 PR은 hosted service control plane이며 서명기, broadcast client, 고객 노드 등록, SSH/API key 입력 경로가 아니다.

## 인증과 tenant 경계

`finance_service/auth.py`는 외부 wallet-proof adapter가 이미 검증한 `VerifiedWalletAssertionV1`만 입력으로 받는다. 서비스는 이를 짧은 수명의 HMAC session으로 교환하고, API body가 보낸 tenant/owner/wallet/network 대신 session scope만 사용한다. key ID를 포함해 키 교체가 가능하고 signature tamper, 만료, revoked session을 거부한다.

이 모듈 자체는 TronLink 서명을 검증하지 않는다. `verification_status=VERIFIED` assertion을 만드는 실제 wallet authentication adapter는 아직 외부 경계다. private key·mnemonic·seed·SSH/API key·access token 계열 필드는 표기 방식이 달라도 record/job/result 저장 전에 거부한다.

## 지속 record, outbox, lease

`OperationalRepository`는 Cherry에서 실행 검증할 수 있는 단일 호스트 SQLite reference다. 다음 의미를 구현한다.

- private record key에 tenant·owner·wallet·network scope hash를 포함하고 optimistic version compare로 갱신한다.
- job identity를 scope, kind, subject, dependency hash로 고정해 같은 사실은 한 번만 큐에 넣고 의존 사실이 바뀔 때만 새 job을 만든다.
- job insert와 outbox insert를 한 transaction에 넣는다.
- worker lease는 무작위 256-bit token, 짧은 만료, 최대 8회 시도, 지수 backoff를 사용한다.
- lease 만료 뒤 같은 job ID를 복구하고 최대 시도 뒤 `FAILED`로 닫는다. 재시도 outbox event도 다시 만든다.
- 같은 network+wallet의 leased job은 tenant가 달라도 직렬화한다. 다른 wallet의 작업은 병렬 claim이 가능하다.
- 모든 상태 변경은 hash-chain journal을 먼저 재검증한 뒤 기록한다. 변조가 있으면 이후 mutation과 backup을 거부한다.
- SQLite online backup 뒤 복원본 journal root를 원본과 대조한다.

SQLite는 검증 reference이며 최종 다중 인스턴스 저장소라는 뜻이 아니다. `db/migrations/009_finance_service.sql`은 PostgreSQL JSONB records/jobs/outbox/public observations/routines/journal, tenant RLS, account-key index, `FOR UPDATE SKIP LOCKED` claim 함수를 정의한다. 현재 Cherry에는 PostgreSQL server가 없어 migration은 parser/static 검증까지만 수행하며 실제 PostgreSQL 적용·장애복구 증거는 아직 없다.

## PR 08 연결

`ExecutionService`는 PR 08 hash-committed result를 같은 session scope에 저장하고 다음 안전한 읽기만 예약한다.

```text
SUBMISSION_UNKNOWN / SOLID_BODY_RECEIPT_PENDING
  -> WATCH: OBSERVE_TRON_TRANSACTION(original txid)

SOLID_EXECUTED_PENDING_POST_STATE
  -> VAULT: RECONCILE_POSITION

SOLID_EXECUTION_FAILED
  -> HELD_FAILURE, automatic replacement 없음

DISPUTED reconciliation
  -> ALPHA: INVESTIGATE_RECONCILIATION, capital lock 유지
```

worker와 API에는 sign/broadcast/deploy method가 없다. PR 08 record hash가 같으면 enqueue도 idempotent이고, unrelated record 갱신은 실행 job을 만들지 않는다.

## 감시와 직원 roster

`FinanceScheduler`는 값 변화와 시간 경과를 분리한다. public observation 값이 같으면 revision을 올리거나 dependent plan을 다시 계산하지 않지만, 새 `valid_until`은 journal에 남긴다. TTL이 지나면 WATCH refresh job을 한 개 만든다.

ALPHA/VAULT/WATCH routine은 책임, ACTIVE/PAUSED/HELD/FAILED, interval, next due, dependency hash를 보존한다. PAUSED routine은 실행되지 않는다. 재배분은 expected benefit이 estimated cost보다 커야 하고 cooldown이 끝나야 queue에 들어간다.

## API와 실행 형태

`create_app`은 FastAPI를 lazy import하고 다음 hosted 경계만 연다.

- health
- authenticated private record read/write
- 고객이 요청 가능한 monitor/reconcile/replan/performance job enqueue/read
- 직원 routine read/write

customer write는 `USER_NOTE`와 `CONVERSATION_DRAFT`만 허용하며 execution/approval/reconciliation 같은 시스템 record는 내부 서비스만 쓸 수 있다. worker lease token과 account serialization key는 API 응답에서 제거한다. customer job allowlist에는 execute/broadcast가 없고 scope당 active job 수도 제한한다. `entrypoint.py`와 `deploy/economic-service.service`는 Cherry 격리 검증용 SQLite service 정의이며 production PostgreSQL 배포 선언이 아니다.

## 검증 범위와 남은 경계

Cherry의 PR09 전용 가상환경에서 session 변조/만료/revoke, cross-tenant record, concurrent version conflict, dependency idempotence, secret rejection, same-wallet serialization, lease crash/restart/attempt cap, retry backoff, TTL refresh, routine pause, rebalance cost/cooldown, backup/restore/journal tamper, FastAPI scope isolation, PostgreSQL migration shape, PR08 execution/reconciliation 연결을 검사한 16개 테스트가 통과했다. 정적 검사, Python compile, PostgreSQL 문법 parse, 임시 Uvicorn health 결과와 소스 해시는 `artifacts/pr09/`에 고정했다.

아직 구현 또는 검증되지 않은 것은 실제 wallet assertion verifier, 실제 PostgreSQL adapter/apply/restore, provider별 observation handler, daemon supervisor 실행, 운영 reverse proxy/TLS, secret manager, production 배포, 실제 Qwen/시장 API 호출이다. 고객 서명 0건, broadcast 0건, 배포 0건, 자산 이동 0건이다.
