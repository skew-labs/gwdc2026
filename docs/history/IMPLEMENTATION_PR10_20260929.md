> Historical record. Current product, setup and verified scope: [faat README](../../README.md).

# PR 10 — 대화형 자산관리 workspace와 제출 증거

2026-09-29. PR01–09의 확인 조건, 결정론적 계획, 승인 경계, 실행 대조, 지속 서비스 상태를 ALPHA/VAULT/WATCH 대화형 화면에 투영한다. 공개 데모는 historical replay이며 실제 Qwen 호출, 지갑 서명, 체인 전송, 실현 성과가 아니다.

## 제품 흐름

`web/`은 대화를 왼쪽에 두고 상품·배분안·승인·근거를 오른쪽 작업 창에 연다. ALPHA는 조건과 두 계획, VAULT는 승인 가능 여부와 자본 상태, WATCH는 snapshot과 체인 상태를 맡는다. 직원 상태는 ACTIVE/PAUSED/HELD/FAILED를 그대로 표시하며 replay의 WATCH를 작동 중으로 꾸미지 않는다. 고객 노드·SSH·API key 등록 화면은 없다.

`ProductWorkspaceStoryV1`은 다음을 하나의 hash commitment로 고정한다.

- 금액, 단위, 기간, 즉시 보유액, 출금 설명, 명시적 차입 동의
- 같은 snapshot의 조건 run A/B와 각각의 policy/comparison/plan hash
- JustLend USDT, JustLend USDD, USDD Vault 담보·부채 경로
- 서로 다른 두 계획의 비중, 현금, 순수익 가정, 비용, stress, 출구
- 정확한 승인 상태, 수취인, 네트워크, 서명/체인/txid 상태
- 보유·예상/실제 성과·외부 입출금·측정 상태
- TRON B/Furiosa A artifact와 미달 blocker

replay/simulation story가 READY 승인, SIGNED, SUBMITTED/SOLID 또는 txid를 주장하면 거부한다. 실제 모델 호출이 없으면 token/latency 수치를 0으로 채울 수 없고 null로 보존한다. 두 계획이 없거나 USDD Vault 담보·부채 상품이 없으면 READY story가 되지 않는다.

## 서비스 연결

인증 경로는 PR09 session scope에서 최신 `PRODUCT_STORY`만 읽는다. revision이 달라진 이전 카드, 다른 tenant/wallet, 만료 story, 현재 계획에 없는 plan hash를 거부한다. replay에서는 `ACKNOWLEDGE_REPLAY`만 가능하고 `OPEN_WALLET_REVIEW`는 막는다. 이 결정도 `execution_authority=NONE`, `signature_status=NOT_REQUESTED`, `chain_status=NOT_SUBMITTED`로 기록된다.

공개 `/api/demo/story`는 합성 scope의 replay 전용이다. 새로고침 복원은 브라우저 localStorage에 story hash와 replay 확인 상태만 저장하며 금융 조건·개인 지갑 비밀값을 저장하지 않는다. 실제 고객 workspace는 Bearer session이 필요한 `/v1/workspaces/{story_id}`와 decision endpoint를 사용한다.

Cherry에서 새 프로세스를 필수 환경값과 함께 시작하고 SSH tunnel을 통해 실제 브라우저로 확인했다. 두 계획 중 선택한 안과 replay 검토 상태는 새로고침 뒤 유지됐다. 390×844 화면에서 작업창·닫기·계획·승인 영역이 보였고 가로 넘침은 0px였다. 브라우저 warning/error는 0건이었다. 검증 중 필수 기준 DB 경로가 빠진 첫 재시작은 실패했고, 이전 프로세스를 종료한 뒤 올바른 환경으로 새 프로세스를 시작해 동일 흐름을 다시 확인했다.

## 조건 A/B와 제출 상태

Cherry의 동일 historical public snapshot에서 Economic Machine을 두 번 실행한다.

- A: 즉시 현금 30%, 차입 미동의
- B: 즉시 현금 70%, 차입 미동의

두 comparison hash와 선택 plan hash가 달라져 조건 변경이 계획 전체에 반영됨을 검사한다. 이 두 실행은 Qwen 호출이 아니다. 실제 Kiln/Qwen3 32B 요청, prompt/completion token, latency, energy는 모두 미측정으로 남으며 Furiosa A 합격 증거가 아니다.

실제 Qwen/Kiln trace, 실제 wallet assertion adapter, PostgreSQL 운영 적용, TronLink 서명, Nile 거래, USDD Vault 실행 capability, live post-state와 realized PnL이 없으므로 두 트랙 전체 합격은 선언하지 않는다. 고객 서명 0건, broadcast 0건, 배포 0건, 자산 이동 0건이다.

## PR 10 검증

- 새 workspace 9개와 직접 연결된 PR09 서비스 16개, 합계 25개 테스트 통과
- 변경 Python 파일 Ruff, `py_compile`, `git diff --check` 통과
- checked-in story의 두 plan, USDD Vault 담보·부채 분류, 미측정 model usage null을 계약으로 재검사
- 새 Cherry Uvicorn의 `/healthz`와 `/api/demo/story` 200, `execution_authority=NONE`
- 데스크톱과 390×844 브라우저 검증, 새로고침 복원, 콘솔 warning/error 0건
- 원격 Playwright Chromium 설치는 CDN timeout으로 완료하지 못해 해당 증거로 세지 않았고, Codex in-app browser 검증을 별도 artifact에 기록
