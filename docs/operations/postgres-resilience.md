# PostgreSQL 운영 복구 지침

이 서비스는 고객 노드나 고객 SSH를 받지 않는 hosted service다. API, worker, backup은 서로 다른 PostgreSQL 자격증명을 사용하며 어떤 프로세스도 지갑 서명·TRON 전송 권한을 갖지 않는다.

## 연결과 비밀 경계

- API는 `FINANCE_SERVICE_POSTGRES_API_DSN_FILE` 하나만 읽는다. worker DSN이 API 프로세스에 들어오면 시작을 거부한다.
- DSN 파일은 절대 경로, 실행 사용자 소유, mode `0600`, 8 KiB 이하의 일반 파일이어야 한다. 심볼릭 링크는 거부한다.
- 운영 DSN은 `sslmode=verify-full`과 절대 경로의 root CA를 포함해야 한다. 평문 localhost는 명시적인 격리 시험 flag에서만 허용한다.
- 파일을 같은 디렉터리의 새 `0600` 파일로 만든 뒤 원자적으로 교체하면 다음 checkout 전에 새 pool을 만들고 기존 pool을 닫는다. health의 `credential_generation`과 실제 DB role 변화로 교체를 확인한다.
- pool은 크기, 대기자 수, checkout timeout, connection lifetime, idle time을 제한한다. transaction마다 statement, lock, idle-in-transaction timeout을 건다.

## 저널과 상태 점검

저널 v2는 scope별 stream과 head를 사용한다. 서로 다른 고객 scope의 append는 하나의 전역 lock을 기다리지 않는다. 변경·조회 hot path는 audit quarantine과 해당 stream의 마지막 event/head를 확인한다. 전체 event hash chain scan은 `economic-service-journal-audit.timer`가 5분마다 수행한다.

전체 감사가 10분보다 오래되거나 head가 맞지 않거나 이전 감사가 실패하면 `/healthz`는 실패한다. 변조 감사가 실패한 뒤 새 쓰기는 거부된다. `/metrics`는 pool 대기·오류와 transaction 수·실패·시간만 노출하며 tenant, wallet, DSN을 label에 넣지 않는다. `economic-service-health.timer`는 1분마다 상태와 critical alert를 확인한다.

## 마이그레이션

`scripts/apply_postgres_migrations.py`는 advisory transaction lock 안에서 migration SHA-256과 위험 등급을 기록한다. 이미 적용한 version의 내용 변경은 거부한다. `DROP TABLE`, `DROP COLUMN`, 실제 `TRUNCATE`, column type 변경, `DELETE FROM`을 포함한 새 migration은 PITR 성공 증거 없이는 적용하지 않는다.

적용 전 순서:

1. 운영 backup 목적지 attestation과 최신 verified physical backup을 확인한다.
2. 같은 PostgreSQL major version의 격리 환경에서 복원과 목표 시점 복구를 확인한다.
3. migration 파일 hash와 위험 등급을 검토한다.
4. additive migration부터 적용하고 idempotent 재실행을 확인한다.
5. destructive migration은 별도 변경 창에서만 실행한다.

## 백업, PITR, failover

`scripts/postgres_physical_backup.py`는 owner-only DSN과 목적지 attestation을 요구한다. attestation은 `gwdc-backup-destination-1`, `off_host: true`, `encrypted_at_rest: true`, 비어 있지 않은 `destination_id`를 가져야 한다. `pg_basebackup`이 끝난 뒤 `pg_verifybackup`을 통과해야 partial 디렉터리를 최종 backup ID로 원자 이동하고 evidence를 쓴다. `economic-service-backup.timer`는 6시간 간격의 실행 예시다.

`scripts/postgres_resilience_drill.py`는 원본 cluster를 `pg_basebackup`으로만 읽고 격리 clone에서 다음을 검증한다.

- restore point 전 marker는 복구되고 이후 marker는 제외되는 PITR
- primary WAL을 standby가 replay한 뒤 primary를 fence하고 standby를 promote
- promote된 DB의 새 write와 기존 replicated marker

PR12 Cherry 시험은 위 세 항목과 backup manifest 검증을 통과했다. 시험 시작부터 완료까지 약 2.6초였지만 같은 서버의 작은 격리 cluster 결과다. 이 수치를 production RTO로 사용하지 않는다. 실제 off-host backup 목적지, 장기 보존, 운영 RPO/RTO, 외부 장애 도메인은 배포 환경에서 별도로 확정해야 한다.

## 장애 처리

- `JOURNAL_INTEGRITY_FAILED`: 서비스를 쓰기 금지 상태로 유지하고 마지막 정상 backup에서 별도 복원한 뒤 원본과 비교한다. audit row를 수동으로 true로 바꾸지 않는다.
- `JOURNAL_DEEP_AUDIT_STALE`: audit timer와 DB 연결을 복구한다. freshness가 돌아오기 전 정상으로 표시하지 않는다.
- `DATABASE_POOL_TIMEOUT_OBSERVED`: 요청 폭주, 느린 query, lock wait를 먼저 확인한다. pool 상한만 늘려 문제를 숨기지 않는다.
- DB 장애 전환: primary를 먼저 fence하고 standby replay LSN을 확인한 뒤 promote한다. 쓰기 endpoint를 둘에 동시에 열지 않는다.
- credential 노출: 새 역할 또는 secret으로 원자 교체하고 generation·role을 확인한 뒤 이전 자격증명을 폐기한다.

이 지침과 unit 파일은 운영 가능한 경계를 정의하지만 배포 완료 증거는 아니다. PR12에서는 Cherry 격리 PostgreSQL에서만 실행했으며 고객 서명, broadcast, 계약 배포, 자산 이동은 수행하지 않았다.
