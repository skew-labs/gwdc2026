# 배분 판단 오라클 v1 — 제약 판정과 데이터 채굴

2026-09-24. 대상은 모델이 제안한 배분안이다. 오라클은 에피소드의 `target` 정답을 읽지 않고 입력 조건·후보·근거와 새 제안을 대조한다. 자기 자신이 낸 답을 다시 정답으로 채택하지 않는다. 구현은 [`allocation_oracle.py`](fdc/allocation_oracle.py), 제안 형식은 [`allocation_proposal_v1.schema.json`](../contracts/allocation_proposal_v1.schema.json), 결과 형식은 [`allocation_oracle_v1.schema.json`](../contracts/allocation_oracle_v1.schema.json)이다.

## 판정 계약

한 제안의 각 계획에 대해 Decimal 문자열로 예산 보존, 즉시 보유액, 총 위험 한도, 공통 위험 그룹 합계, 후보별 시장 현금, 토큰 단위, 활성 시장, 출금 일정, 관측·유효 시각, 사용한 후보의 근거 ID와 의존성 ID를 검사한다. 이유 코드는 계획·상품 또는 위험 그룹·관측값·한도와 함께 기록한다. 결과와 입력은 각각 SHA-256으로 식별한다. 실제 원천 대조가 성공하면 스냅샷 ID·원문 SHA-256·수집 시각·필드 위치를 `source_witnesses`에 남긴다.

- `REJECTED`: 증명 가능한 위반 또는 손상된 원문 근거가 있다. 여러 위반을 동시에 기록한다.
- `NEEDS_EVIDENCE`: 확인한 제약 위반은 없지만 제품 판단에 필요한 근거가 없다.
- `CONSTRAINTS_PASS`: 제공된 **합성** 계약의 결정론적 제약을 통과했다. 최적 배분, 실수익, 소비자 적합성, 구매 허가는 뜻하지 않는다.

`judge_counterfactual_pair`는 사용자 조건 한 필드만 바뀐 두 입력을 검증하고 두 제안을 독립적으로 판정한다. 바뀐 위험 한도 아래 옛 배분을 그대로 내서 제약을 어겼다면 `CHANGED_PROPOSAL_REJECTED`를 반환한다. 두 안이 모두 통과해도 변경이 최적이었다는 뜻은 아니다. 결과 형식은 [`allocation_pair_verdict_v1.schema.json`](../contracts/allocation_pair_verdict_v1.schema.json)이다.

실제 API 사례에는 저장된 JustLend 시장 스냅샷의 해시·수집 시각·주소·상품 정보·공급 금리·현금과 각 JSON 위치를 다시 대조한다. 이 API의 `supplyRate`와 `cash`는 별도 의미를 갖고, 현금은 미래 출금 가능액의 증거가 아니다. [JustLend 공식 API](https://docs.justlend.org/developers/apis/)가 필드와 단위를 명시한다. 실제 사례는 사용 권한, 출금 약관, 비용·실행 경로, 공유 위험 그래프, 독립 검토 정답이 아직 부족해 오라클의 `training_scope`를 `excluded_unreviewed`로 고정한다. 오라클이 시장 상태를 읽었다는 사실만으로 구매 가능 결론을 내지 않는다.

## 생성·검증한 데이터

[`mine_oracle_cases.py`](fdc/mine_oracle_cases.py)는 검증된 합성 V3 에피소드 2,000건에서 **7,935개 제약 판정 사례**를 생성했다. 그중 원래 제안 1,689개는 `CONSTRAINTS_PASS`, 예산 1 증가·사용 근거 제거·시장 만료·공통 위험 초과로 만든 6,246개는 `REJECTED`다. 1,000개 원천 가족 중 배분 제안이 있는 909개 가족에서 사례가 나왔다. 가족 단위 분할은 학습 6,333 / 개발 792 / 합성 홀드아웃 810건이다. 각 사례에는 입력(정답 `target` 제외), 제안, 위반 이유, 합성 변형 유형, 원래 가족·분할을 넣었다.

Cherry 산출물은 `/srv/skew/gwdc-financial-agent-20260924/data/oracle/synthetic-constraint-v1-3-20260924.jsonl`과 같은 이름의 `.manifest.json`이다. JSONL SHA-256은 `345fbb3ef5976694d66eb49b3c379e75ba202f94594a89166fc4cc6187fe54c4`. 모든 판정의 출력 계약을 검증했고, 사례 ID 중복·가족 분할·정답 필드 유입이 없음을 별도로 확인했다. 이 수치는 정해진 변형에서의 **합성 제약 판정** 결과이지 실제 금융 판단 정확도나 타 시스템보다 나은 성능을 뜻하지 않는다.

실제 TRON API 사례에 대한 시험 제안은 `/srv/skew/gwdc-financial-agent-20260924/data/oracle/real-api-draft-100-usdt-audit-v2.json`에 남겼다. 이 100 USDT는 테스트 입력이며 추천 금액이 아니다. 원문 금리·현금 대조는 통과했지만 사용자의 실제 의사와 출금 일정·비용·권한이 없어 `NEEDS_EVIDENCE`, `product_actionable=false`, `human_gold=false`였다.

## 다음 단계의 데이터 품질 문턱

1. 출금·잠금·수수료·가격·한도·계약 버전의 공식 근거를 시점과 함께 수집한다. 값이 없는 경우 오라클의 `unknown`으로 보존한다.
2. 실제 사용자 조건은 동의·정정·철회와 함께 별도 보관한다. 모델이 만든 제안, 오라클 판정, 사용자 승인, 실제 결과를 각각 독립 객체로 연결한다.
3. 실제 자료에서도 예산·단위·시점·제약 위반처럼 원문과 규칙으로 증명 가능한 판정은 자동 처리한다. 자료 누락은 수집 또는 사용자 조건 질문으로, 원문 충돌·규칙 미정의·배분안의 주관적 우열은 선택적 독립 검토로 보낸다. 사용자가 모든 라벨을 검사하지 않는다. 다만 현재 구현의 실제 사례 `training_scope`는 여전히 `excluded_unreviewed`이며, 자동 검증한 제약 결과를 실제 배분의 최적성 정답이나 학습 허가로 승격하는 기능은 아직 없다. 운영 분기는 [검토 부담을 줄이는 운영 계약](REVIEW_ROUTING_20260924.md)에 정의한다.
4. 학습은 `synthetic_constraint_only`를 제약 판별 보조 과제에만 사용한다. 가족별 봉인 자료와 이후에 들어온 실제 사례로 위반 탐지·근거 충실도·유보·조건 변경 성능을 별도로 평가한다. JEV 등과의 우열은 같은 데이터·권한·비용·지연 조건의 독립 비교가 있기 전에는 주장하지 않는다.

Cherry에서 한 안을 재검사하는 명령:

```sh
PYTHONPATH=src:. .venv/bin/python -m research.fdc.oracle_cli \
  --episode data/drafts/typed-tron-api-example-20260924.jsonl \
  --proposal data/oracle/real-api-draft-100-usdt-probe.json \
  --data-dir data/live-read \
  --output data/oracle/새-판정.json
```

기존 출력 파일은 덮어쓰지 않는다. 현재 오라클은 제안 **검사기**이며 투자 효용 함수나 승인 엔진이 아니다.
