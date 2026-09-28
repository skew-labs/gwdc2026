# 사용자 제안 전체 독해 대응표 — FS1 BYON

2026-09-25. 이 표는 사용자가 보낸 도입부, 번호 1~16, 마지막 기술 스택/다음 단계까지 순서대로 대응한다. 이전 문서의 “우리가 자체 노드를 고객 대신 운영” 해석은 오독이므로 폐기한다. 새 제품 기준은 **Bring Your Own Node (BYON): 각 사용자가 자기 컴퓨팅 노드를 제공하고 그 위에 금융 에이전트 실행부가 설치되거나 API로 연결된다**이다. Cherry는 소유자가 제공한 대회 시험 노드이며 제품의 공용 실행 인프라가 아니다.

| 원문 위치 | 읽은 구체 내용 | 설계에 반영한 위치/판단 |
| --- | --- | --- |
| 도입부 | 블록체인 금융 에이전트의 컴퓨팅은 결국 노드에서 실행된다. 판단 오라클, 24/7 토큰비, 판단·실행 속도, 하드웨어 토폴로지를 함께 풀어야 한다. 대회 내부는 그 구조, 외부 UI는 Grok Bot형. 회사가 고객 컴퓨팅을 대지 않고 사용자가 SSH/API로 **자기 노드**를 연결한다. 기존 자체 운영 노드 가정을 바꾸라는 요청이다. | BYON 제품 경계, SSH/API 가입, ALPHA 직원형 UI, 사용자 노드의 데이터면과 가벼운 제어면. Cherry는 사용자 제공 시험 노드로만 설명. |
| 1 | `Market → API/RPC → agent loop → LLM → tool → trading API/chain`의 hot path 문제 7개: polling/inference, 토큰, 자동회귀 지연, 비결정성, 행동 오라클 부재, RPC 단계, 재현 불가. | 시장 감시와 거래 준비에서 LLM 제거, 변경 이벤트·결정적 검사·재현 가능한 영수증. 외부 API/체인 지연은 별도 측정. |
| 2 | 사용자의 자연어를 초기에 `Financial Policy IR`로 컴파일·해시/등록. Market/chain/oracle feed가 노드의 state/trigger/decision/risk/router/execute를 지나고 체인에 결정·실행 영수증을 남김. 예시 SOL 청산 버퍼·funding·호가 조건. | Policy IR의 버전·사용자 확인, 상시 노드 루프. SOL perp 예시는 TRON B의 USDT/USDD 배분·유동성·출금 트리거로 변환; 미지원 perp를 대회 기능으로 쓰지 않음. |
| 3 | 자연어 토큰 생성 대신 market/portfolio/liquidity/risk/user-policy/chain 상태 텐서에서 단일 pass로 HOLD/REDUCE/HEDGE/CANCEL/ESCALATE 확률, risk·confidence·TTL을 내는 금융 결정 모델. | 타입이 정해진 DecisionTile 인터페이스와 확률 보정 요구. 대회에서는 검증된 규칙·정확 계산을 먼저 사용; 실금융 라벨이 없는 모델의 확률·신뢰도·280ms를 성과로 주장하지 않음. |
| 4 | `state_root + policy_root + model_root`를 독립 노드에 제공, A/B/C 서명과 2/3 정족수로 DecisionReceipt/Attestation 생성. 동일 입력·모델·정책에서 결정적 출력. Pyth 라우터 패턴은 비유. | 영수증 스키마에 세 버전 루트와 서명. **한 호스트의 세 프로세스는 독립 정족수가 아님.** 모델 부동소수점 결과의 bit 일치는 가정하지 않고 정수화된 행동/제약을 비교. 장기 독립 운영자 풀로 확장. |
| 5 | 모든 거래에 정족수를 돌리지 않고 read-only/local, 소액/local+receipt, 고액/2-of-3, 출금·정책 변경/quorum+사용자 인증으로 위험별 분기. | 위험별 경로와 사람 승인. 원문의 $100/$50,000은 예시이며 실제 한도는 사용자 정책·상품·규칙별로 정의. 대회 실거래는 매회 사용자 서명. |
| 6 | numeric stream→CPU trigger; 대부분 rule/state machine, 일부 Financial Decision Model, 극소수 LLM. 99.x%는 벤치마크가 필요한 예상. | unchanged에서 모델/계획 재계산 0이라는 불변식과 흐름별 토큰 계측. 비율은 측정 전 미기재. |
| 7 | Firedancer식 NET/DECODE/ORACLE/STATE/FEATURE/TRIGGER/DECISION/RISK/ROUTER/SIGNER/EXECUTE 타일, 코어 pin, shared-memory queue, AF_XDP, huge pages, NUMA, IRQ. | 논리 타일과 큐/역압·복구 설계. SIGNER는 사용자 지갑 경계. 사용자가 가져온 전용 노드에서 capability 검사 후 최적화; 공유 Cherry의 NIC/IRQ를 변경하지 않음. |
| 8 | Node.js·HTTP·JSON·LLM·DB/heap을 hot path에서 제거하고 fixed structs, 사전 할당, shared-memory ring, SIMD, zero-copy, busy poll. | 빠른 경로의 바이너리/정수 메시지 계약. 외부 API와 영속화 경계에는 HTTP·JSON·DB가 여전히 필요. zero-copy/busy poll은 계측·전용 NIC·사용자 동의 전까지 미적용. |
| 9 | 작은 market/portfolio/policy encoder와 fusion, action/risk/confidence/TTL heads. 작은 추론은 CPU SIMD가 GPU의 PCIe·batching·scheduler보다 유리할 수 있음. | DecisionTile은 플러그형. 규칙·최적화·CPU 모델·NPU를 같은 품질/지연/전력 기준으로 비교하며 모델을 필수로 강제하지 않음. |
| 10 | 예시 FS1 하드웨어: 고클럭 단일 socket 24~32 physical cores, AVX2/AVX-512, 128GB ECC, RAM hot state, 25GbE+/RSS/AF_XDP/timestamp, NVMe 2개는 replay·audit용. | BYON 하드웨어 프로파일러가 실제 CPU/SMT/NUMA/RAM/NIC/disk/NPU를 읽고 light/standard/tiled 모드를 고른다. 원문 수치는 전용 노드 상위 목표이지 Cherry 또는 모든 사용자 노드의 최소 사양이 아니다. |
| 11 | FPGA는 처음부터 모델 추론보다 packet parse, feed normalization, 서명 사전 검사, timestamp, 위험 조건, trigger의 “sensor processor”로 사용. 뒤에 단순 head/route까지 확장 가능. | 미래 optional sensor offload. 대회 소유 장비·구현으로 표시하지 않음. |
| 12 | SmartNIC/DPU가 feed decode→verify→normalize→trigger를 담당하고 CPU가 decision→risk→router를 담당하는 금융 네트워크 appliance. | BYON 고성능 모드의 선택적 NIC 플러그. 표준 CPU 모드와 같은 결정 계약·재생 테스트 필요. |
| 13 | 온체인은 market tick 전체가 아니라 Policy Root, Model Root, State/Oracle Root, Decision Receipt, Execution Receipt 다섯 범주. 왜 포지션을 바꿨는지 재현. | 영수증 묶음의 Merkle root를 배치 커밋하고 원문·사용자 포트폴리오는 노드에 보관. 체인 커밋이 판단 정답이나 실제 체결을 대신하지 않음. |
| 14 | Data Oracle “무엇이 사실인가?”와 Decision Oracle “무엇을 해야 하나?” 분리. Pyth pull은 참고. Decision Attestation Network가 기술적으로 정확하고 “Proof of Decision”은 정답의 암호학적 증명이 아님. | 원천 검증·시각·단위·충돌, 별도 제약/목적함수 계산. 독립 노드 합의는 버전·행동 일치의 증명이지 금융 적합성 정답이 아님. |
| 15 | 마이크로/밀리초 감시, 밀리초 결정, 블록체인 실행·정산, 느린 LLM 전략·수정·조사라는 네 시간대 분리. | 각각 별도 큐/계측/실패 상태. 외부 10분 API 데이터와 체인 확정이 있는 전체 서비스를 마이크로초라고 주장하지 않음. |
| 16 | “A hardware-optimized decision network for autonomous finance”; 정형 결정·독립 증명·블록체인 실행을 가진 금융 System-1 runtime으로 새 카테고리를 정의. | 상품은 BYON 금융 에이전트 런타임 + Grok Bot형 제어면. 자율성은 PolicyIR 및 서명 권한 안에서만. |
| 마지막 스택 | Natural Language→Financial Policy Compiler→IR→Event-driven Runtime→Decision Model→Decision Attestation→Risk Kernel→Execution Kernel→Hardware-optimized Node. Firedancer처럼 하드웨어 토폴로지에 맞춰 재설계. | 제품 아키텍처의 수직 스택과 BYON 실행/제어 경계에 그대로 대응시킴. 모델/attestation/exec의 대회 구현 여부는 별도 표시. |
| 마지막 다음 단계 | 16~24코어 노드 v1에서 NET/ORACLE/STATE/FEATURE/TRIGGER/DECISION/RISK/ROUTE/EXECUTE/SIGN의 코어 배분과 shared-memory layout까지 내려가자는 제안. | 전용 사용자 노드의 측정 기반 코어 배분·ring 레이아웃 명세를 별도 수준으로 정의; 현재 공유 Cherry에 선점 적용하지 않음. |

이 표의 “반영”은 구현 완료 뜻이 아니다. 대회용 구현·측정·체인 영수증은 [BYON 대회 아키텍처](FS1_NODE_GWDC_ARCHITECTURE_20260925.md)에서 별도로 상태를 표시한다.
