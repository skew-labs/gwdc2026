# GWDC 금융 에이전트 — TRON B × Furiosa A

**2026-09-28 최신 제품 방향:** 고객 소유 노드/BYON/SSH·runner 가입을 제외하고 **Qwen3 32B + 일반 웹서비스 + Economic Machine + TronLink 승인**으로 통합한다. [통합 아키텍처·첨부 전체 대조·구현 격차](TRON_FURIOSA_UNIFIED_ARCHITECTURE_20260928.md)와 [의존관계·완료 기준을 정한 10개 PR 계획](TRON_FURIOSA_10PR_PLAN_20260928.md)이 현재 우선 명세다. 아래 과거 배포/연구 기록을 이 새 고객 흐름의 완료 증거로 해석하지 않는다.


데이터 입력을 제외한 별도 **Economic Machine 실행 코어**는 [구현·명령어·불변조건·검증표](MACHINE_ECONOMICS_CORE_20260925.md)에 있다. `src/economic_machine/`는 State/Opcode/Invariant/Transition/Receipt 및 Capital Sandbox를 구현한 독립 패키지이며, Cherry에서 가상 상태만으로 검증했다. 체인 거래 실행이나 실제 정산은 아직 연결되지 않았다.

기준 모델: **Qwen3 32B** — 2026-09-28 사용자 확정. 공개 Kiln 카탈로그에 `qwen3-32b`가 표시되며 계정별 실제 endpoint 호출·사용량·실행 환경 증거는 별도 확인 대상이다.

2026-09-25 정정된 핵심 목표: **변화만 계산하는 금융 에이전트 엔진**이다. [제품 런타임 아키텍처](FINANCIAL_AGENT_ENGINE_ARCHITECTURE_20260925.md)는 원천 변화와 사용자 조건의 의존성 그래프, 증분 판단, 정확한 오라클, 필요한 경우에만 호출하는 Qwen을 정의한다. 기존 자체 신경망은 독립 연구 경로이며 엔진의 필수 전제가 아니다.

과거 대회 적용안인 [FS1 BYON 설계](FS1_NODE_GWDC_ARCHITECTURE_20260925.md)와 [9/25 빌드·검증표](GWDC_BYON_CONTEST_BUILD_20260925.md)는 역사 기록이다. **BYON은 9/28 사용자 지시로 제품 범위에서 제외됐다.** 당시의 읽기 전용 runner와 변경 감지 코드는 서버 내부 worker로 재사용할 수 있으나, 가입/노드 네트워크를 만들지는 않는다. Cherry는 현재 승인된 개발·검증 호스트다. 과거 서비스 가동 기록은 오늘의 운영 상태를 보장하지 않는다.

**현재 구현:** Cherry에서 실원천 수집·사실 변화 채굴·제한된 형식 최적배분 계산이 주기 실행된다. 사용자별 영향 그래프, 지속형 판단 큐, 소비자용 배분 증명서, 실제 구매 경로는 아직 없다. 이전에 구현한 금액·단위·시점·출금 조건 입력과 공동 판단 연구는 [V3 구현 기록](research/FINANCIAL_CORE_V3.md)에 남긴다.

**TRON 실데이터 추가:** [실관측 데이터 릴리스와 학습 결과](research/REAL_TRON_DATA_20260924.md)에 USDD TRON 일별 기록 366개와 JustLend jUSDT·jUSDD 확정 이벤트 800개, 원문 대조, 시계열 인코더 학습 결과를 기록했다. 실제 배분 정답은 아직 없으며 첫 시계열 예측은 단순 지속값 기준선보다 오차가 컸다.

**소비자용 데이터 루프:** [데이터·지속 학습 계약](research/CONSUMER_GRADE_DATA_LEARNING_20260924.md)에 공식 원천 6개 수집, USDD TRON 담보·볼트 위험 지표, 매일 관측 릴리스·후보 학습·전진 평가, 모델 승격 금지 조건과 필요한 실제 정답을 정리했다. 주기 학습은 연구 후보만 만든다.

**배분 판단 오라클:** [오라클 구현·데이터 계약](research/ALLOCATION_ORACLE_20260924.md)은 제안된 배분을 원래 정답 없이 재검사한다. Cherry에서 합성 제약 판정 7,935건을 만들었고 실제 TRON 초안은 근거 부족으로 `NEEDS_EVIDENCE`였다. 두 결과 모두 사람 검토 정답이나 거래 허가가 아니다.

**자율 데이터 생산:** [원문 증명 사례 엔진](research/AUTONOMOUS_DATA_ENGINE_20260924.md)을 Cherry에 가동했다. 공식 원천의 새 스냅샷과 USDD·JustLend 이력 릴리스에서 첫 관측·값 변화·결측·원천 수정을 자동 채굴한다. 2026-09-24 검증 시 사실·변화 사례 2,056건을 만들었고 매시간 새 자료를 확인한다. 원천 권한은 아직 `unknown`이라 새 사례는 학습과 실제 배분 정답에서 제외된다.

**자동 배분 최적해 정답:** [형식 최적해 엔진](research/FORMAL_OPTIMAL_LABELS_20260924.md)이 위의 매시간 작업에서 실제 JustLend 금리·현금을 원문 확인하고, 자동 생성한 예산·즉시 보유액·위험 조건 아래 정확한 수학적 최적 배분을 산출·재검증한다. 고유 시장 상태 14개에서 정답 1,512건과 한 조건 변경 쌍 3,528건을 만들었다. 기본 금리의 총수익률 대리값에 대한 최적성만 증명하며 실제 고객 최적성·순수익·거래 허가는 뜻하지 않는다. 원천 학습 권한이 미확인이라 연구용 정답도 모델 학습으로 자동 승격하지 않는다.

사용자 흐름: 대화 → 검색 → 상품·배분안 작업 공간 → 구체적인 승인 → 지갑 실행 → 결과와 보유 상태. UI 기준은 사용자가 지정한 Fomo token과 WHOLLET Workspace다.

장기 제품 경험은 [ALPHA·VAULT·WATCH 지속형 자산관리 팀 설계](PERSONAL_WEALTH_TEAM_20260924.md)에 정리했다. 현재 화면은 읽기용 대화·배분 비교까지만 구현되어 있으며, 직원 명단·개인 기억·루틴·거래는 설계 단계다.

| 파일 | 용도 |
|---|---|
| [금융 모델 아키텍처 명세](FINANCIAL_MODEL_ARCHITECTURE_20260923.md) | 최우선 연구 방향: 신경망 구조·학습 목적·데이터·비교 실험 |
| [금융 모델 소스](research/fdc/model.py) / [연구 상태](research/README.md) | 학습 가능한 금융 결합 블록, 입력 변환, 제약 보정, 학습·합성 평가 |
| [TRON 실관측 릴리스](research/REAL_TRON_DATA_20260924.md) | 실제 TRON API·계약 이벤트 1,166행, 원문 검증, 시계열 부분 학습과 한계 |
| [소비자용 데이터·지속 학습](research/CONSUMER_GRADE_DATA_LEARNING_20260924.md) | 주기 수집·변경 감지·전진 평가, 미확보 상품 약관·실제 정답, 승격 조건 |
| [배분 판단 오라클](research/ALLOCATION_ORACLE_20260924.md) | 수치·시점·공통 위험·근거 판정, 합성 반례 채굴, 실제 사례 유보 |
| [자율 데이터 생산 엔진](research/AUTONOMOUS_DATA_ENGINE_20260924.md) | 원문 대조·변화/수정 판정·중복 방지·매시간 생산, 학습 권한 게이트 |
| [형식 최적 배분 정답 엔진](research/FORMAL_OPTIMAL_LABELS_20260924.md) | 실제 원천 + 자동 사용자 조건의 정확한 목적함수 최적해·검증 증명·조건 변경 쌍 |
| [통합 구축안](MULTITRACK_FINANCIAL_AGENT_20260923.md) | 최신 트랙 구성, 전용 판단 구조, Qwen 운용, 평가·데모·일정 |
| [상세 데이터셋 계획 v1.4](DATASET_PLAN_20260921.md) | 미래에셋 구조 재사용, 금융 원천·정밀도·수익·노출·실행·UI |
| [데이터셋 카드](DATASET_CARD.md) | 라벨링, split, 권한, 품질 검사, 현재 상태 |
| [사례 스키마](contracts/decision_case.schema.json) | 네 가지 작은 금융 판단의 보관 계약 |
| [모델 연결 템플릿](contracts/model_binding.template.json) | Qwen 요건과 미확인 설정을 구분 |
| [합성 개발 사례 16건](cases/seed_decisions.jsonl) | 검토/구현 시작용 초안; 실제 금융 관측·골드 아님 |

## 구현 상태 · 2026-09-24

최신 사용자 지시에 따라 AWS 대신 Cherry `charmed-weasel`의 격리 경로 `/srv/skew/gwdc-financial-agent-20260924`에서 구축·검증했다. 공유 서버의 다른 프로젝트 경로·서비스는 수정하지 않았다. 읽기 전용 수집기, 원문 해시가 연결된 SQLite 정본, 최신성 검사, 단일 자산 배분 비교기, Qwen 32B API 호환 해석 어댑터, 대화형 검토 UI를 작성했다. 공식 [JustLend](https://docs.justlend.org/developers/apis/)·[USDD](https://docs.usdd.io/developers/usdd-public-api) API 여섯 원천의 실제 응답을 수집·정규화했다. 전용 `gwdc-finance-collector.service`가 10분 간격으로 읽기 수집하며 하루 요청 시도 상한은 1,000회다. 9/24 자동 수집과 USDD TRON 담보·볼트 94개 지표의 원문 대조를 확인했다. 매일 `gwdc-finance-learning.timer`가 연구용 관측 릴리스와 새 성숙 일자에 대한 후보 학습을 진행한다.

JustLend 공급 연율, 별도 USDD 채굴 보상, USDD 프로젝트 APY를 구별한다. 공식 API 스냅샷으로 실제 응답 기반의 두 배분안과 에피소드 초안을 만들고 스키마 검증했다. 초안은 분할 `excluded`, 검토 `unreviewed`, 권한 `unknown`이라 학습에 넣지 않는다. 화면 서버의 `/api/plan`은 두 안을 반환했고 `/api/approve`는 403으로 차단됐다. 수수료·지갑 잔액·실제 예치 경로와 기간 수익 계산 방식이 확인되지 않았으므로 계획은 **연구 비교용**이며 실제 구매는 불가능하다. 화면의 시각적 브라우저 확인은 수행하지 못했다.

에피소드 V2 검사기는 예산 보존·근거 참조·가족별 분할·재사용 권한·사람 검토 상태를 확인한다. 합성 시나리오 가족 1,000개에서 에피소드 2,000건을 생성해 검증했고, 학습/개발/합성 홀드아웃은 1,598/180/222건이다. FDC 자체 가중치를 Cherry CPU에서 5 epoch 학습했다. 합성 홀드아웃 모드 분류는 제약 손실 전 94.1%, 제약 손실을 넣은 문자 해시 버전 95.5%, 고정 다국어 E5 인코더 버전 99.1%였다. 원시 배분 제약 위반률은 각각 22.2%, 4.1%, 3.1%이고, 배분 비중 평균 절대오차는 0.059, 0.091, 0.092였다. 단순 MLP 기준선은 모드 87.4%, 제약 위반 22.2%, 비중 오차 0.144였다. 이는 같은 생성 규칙으로 만든 **합성 자료의 연구 수치**이며 금융 실전 성능이나 JEV 대비 우위가 아니다. 보고서·체크포인트는 Cherry의 `data/runs/`에 있으며, 인코더는 [MIT 라이선스의 다국어 E5-small](https://huggingface.co/intfloat/multilingual-e5-small) 특정 리비전을 고정해 사용했다. 코어 가중치는 자체 학습했고 인코더 가중치는 고정했다.

Cherry에서 데이터·모델·기존 API 계약 테스트 29개가 통과했다. 실제 여섯 원천의 원문 해시·최신성·USDT/USDD 활성 시장·필수 지표 검사 결과는 `PASS_FOR_RESEARCH_READ`였으며 이는 사실성·권한·거래 가능성의 보증이 아니다. 시간순 관측 내보내기는 수집 완료 뒤 찍은 시각과 원문-정규화 값 대조를 요구한다. 초기 수집본은 요청 전 시각으로 기록되어 엄밀한 시점 평가에서 제외한다. 초기 API 내보내기는 USDT·USDD 2건/한 시점이었다. 이후 USDD의 연간 이력과 JustLend 이벤트를 별도 실관측 릴리스로 확보했지만, JustLend의 장기 금리·실제 출금 결과 이력은 부족하다. 실원천 릴리스는 검토 전 후보로만 기록한다. 독립 검토 골드셋, 충분한 JustLend 장기·출금 결과 이력, 확정 비용·출금 약관 oracle, Qwen 실호출, 확률 보정, NPU 이식, 지갑·테스트넷 실행은 아직 없다. FDC 약 961만 매개변수와 MLP 약 30만 매개변수의 이전 합성 실험에 더해 V3 입력·조건 변경 실험을 별도로 수행했다. 크기·지연·비용을 포함한 공정한 비교가 추가로 필요하다. 이 Mac에서는 빌드·테스트·수집·학습을 실행하지 않았다.

```sh
# Cherry의 /srv/skew/gwdc-financial-agent-20260924 안에서:
PYTHONPATH=src OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=src .venv/bin/python scripts/smoke_live_http.py data/live-read
PYTHONPATH=src .venv/bin/python -m finagent.cli --data-dir data/live-read export-history --output data/exports/market-observations.jsonl
systemctl status gwdc-finance-collector.service
.venv/bin/python -m research.fdc.validate data/synthetic-1000.jsonl --schema contracts/episode_v2.schema.json
```

Qwen 연동에는 대회 제공 API 문서, 정확한 Qwen 32B 모델 ID, 접속 정보가 필요하다. `GWDC_QWEN_BASE_URL`, `GWDC_QWEN_MODEL_ID`, `GWDC_QWEN_API_KEY`는 원격 비밀 환경에 설정하고 소스에 넣지 않는다. GPU/NPU 워크로드는 별도 사용 권한 확인 전 시작하지 않는다.

금융 데이터·사용자 조건·판단·계획·승인·결과를 연결하고, 금융 상태의 관계와 조건 변화에 맞게 판단하는 모델을 학습하는 것이 핵심이다. 기술적 신규성과 JEV 대비 경쟁력은 독립 평가로 입증할 연구 과제다.
