# 자율 데이터 생산 엔진 — 원문 증명 사례의 지속 축적

2026-09-24 Cherry `charmed-weasel`의 `/srv/skew/gwdc-financial-agent-20260924`에 구축·가동했다. 사실·변화 구현은 [`auto_cases.py`](fdc/auto_cases.py), 배분 최적해 정답은 [별도 계약](FORMAL_OPTIMAL_LABELS_20260924.md)의 [`formal_optima.py`](fdc/formal_optima.py)다. 기존 10분 간격 공식 원천 수집과 매일 실관측 릴리스·연구용 학습에 더해, `gwdc-finance-auto-cases.timer`가 매시간 저장된 자료에서 새 사례와 명시적 목적함수의 배분 최적해를 생성한다. 사용자가 데이터를 건별로 판정하거나 봇이 자신의 답을 정답으로 채택하는 단계는 없다.

## 자동 생산 경로

1. JustLend·USDD 저장 스냅샷의 원문 SHA-256과 정규화 값의 JSON 위치를 다시 대조한다. 수집 **완료** 시각을 사용한 파서 버전만 처리한다. 이전 버전 75개 스냅샷은 시점이 부정확해 제외했다. 새로운 미지원 파서 버전은 조용히 건너뛰지 않고 실패한다.
2. 출처·대상·지표·단위를 키로 첫 관측값과 이후 **실제로 값이 바뀐 경우**만 사례화한다. 같은 값을 반복 수집해도 새 사례로 세지 않는다. 증가·감소는 Decimal로 계산한다. 수치의 결측은 0과 별도로 기록한다. 두 수집 시각 사이에 값이 달랐다는 의미일 뿐 시장의 정확한 변동 시각을 추정하지 않는다.
3. 기존 USDD 일별 자료와 확인된 JustLend `JTokenStatus` 이벤트 릴리스는 저장 원문부터 전체 재검증한 뒤, 동일한 경제적 값이 반복되면 중복 사례를 만들지 않는다. 같은 날짜·이벤트의 값이 나중에 수정되면 원래 사실을 덮지 않고 `SOURCE_REVISION` 사례로 남긴다.
4. 각 배치는 스냅샷 또는 릴리스 ID 이름의 변경 불가 JSONL로 보관하고 SHA-256을 색인에 기록한다. 재실행 때 배치가 없거나 해시가 바뀌면 중단한다. 새 자료가 없으면 새 사례는 0건이다.

산출물은 `data/auto-cases/batches/`, `data/auto-cases/history/`, `data/auto-cases/index.sqlite3`, `data/auto-cases/latest.json`에 있다. `latest_attempt.json`은 마지막 실행의 성공 또는 실패를 기록해 이전 성공 요약이 현재 정상 가동으로 오해되지 않게 한다. 개별 사례에는 원천 URL·해시·필드 위치·수집 가능 시각·단위가 들어 있다. 모든 사례의 `rights_status=unknown`, `training_scope=excluded_rights_unknown`, `decision_gold=false`, `product_actionable=false`다. 이는 실제 공급 금리·담보·시장 현금 등 **사실과 변화의 증거 자료**이며, 사용자에게 최선의 배분을 증명하지 않는다.

## Cherry 검증 상태

2026-09-24 가동 시점에 수집 완료 시각이 신뢰 가능한 스냅샷 47개에서 사례 869건, 이력 릴리스 5개에서 사례 1,187건을 만들었다. 이력 1,187건 중 원천 수정 4건을 별도 기록했다. 전체 2,056건을 실제 배분 정답 건수로 더하지 않는다. 같은 자료로 서비스를 재실행했을 때 새 사례 0건, `Result=success`, 타이머 `enabled/active`를 확인했다. 다음 예약 시각은 당시 기준 2026-09-24 07:17 UTC였다. Cherry 전체 테스트 35건이 통과했다.

현재의 매일 연구용 시계열 학습은 [소비자용 데이터·지속 학습 계약](CONSUMER_GRADE_DATA_LEARNING_20260924.md)에 따라 모델 후보만 만들고 제품 모델을 자동 승격하지 않는다. 이번 새 사례는 원천의 재사용·학습 권한이 `unknown`이므로 그 학습 입력에 자동 투입하지 않는다. 향후 권한이 확인되면 **원문 사실 추출**, **시점 간 변화 탐지**, **성숙한 미래 관측 예측**을 각각 분리해 학습·봉인 평가할 수 있다. 배분 코어 학습에는 이 사실 자료 외에 출금 약관·비용·사용자 조건·사후 체결/출금 결과가 더 필요하다. 증명 가능한 예산·단위·위험 제약은 별도의 [배분 오라클](ALLOCATION_ORACLE_20260924.md)이 라벨을 만들 수 있으나, 제약 통과를 최적 배분 정답으로 승격하지 않는다.

## 운영 확인

Cherry에서 `systemctl list-timers gwdc-finance-auto-cases.timer`, `systemctl show gwdc-finance-auto-cases.service -p Result -p ExecMainStatus`, `data/auto-cases/latest.json`을 확인한다. 서비스는 이 프로젝트의 데이터 경로만 읽고 `data/auto-cases`에만 사례를 쓴다. 원천 손상, 단위 변경, 미지원 파서, 이력 중복·수정은 자동 성공으로 포장하지 않는다. 근거 공백과 약관·권한 확인 요청은 집계 대기열로 남기며 소유자에게 개별 라벨 확인을 요구하지 않는다.
