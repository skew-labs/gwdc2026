const byId = (id) => document.getElementById(id);
const turns = byId("turns");
const workspace = byId("workspace");
const workspaceBody = byId("workspace-body");
const state = {story: null, selectedPlan: null};

function el(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = String(text);
  return item;
}

function append(parent, ...children) {
  parent.append(...children.filter(Boolean));
  return parent;
}

function exactNumber(value) {
  if (value === null || value === undefined) return "미측정";
  const raw = String(value);
  const sign = raw.startsWith("-") ? "-" : "";
  const unsigned = sign ? raw.slice(1) : raw;
  if (!/^\d+(\.\d+)?$/.test(unsigned)) return raw;
  const [whole, fraction] = unsigned.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${sign}${grouped}${fraction ? `.${fraction}` : ""}`;
}

function shortHash(value) {
  return value ? `${value.slice(0, 8)}…${value.slice(-6)}` : "없음";
}

function timeLabel(value) {
  return value ? value.slice(0, 16).replace("T", " ") + " UTC" : "시점 미확인";
}

function toast(message) {
  const box = byId("toast");
  box.textContent = message;
  box.classList.add("show");
  window.setTimeout(() => box.classList.remove("show"), 2600);
}

function addTurn(kind, sender, message, action) {
  const article = el("article", `turn ${kind}`);
  if (kind !== "user") {
    const head = el("div", "sender");
    append(head, el("span", `agent-icon ${sender.toLowerCase()} mini`, sender[0]),
      el("strong", "", sender), el("time", "", "지금"));
    article.append(head);
  }
  article.append(el("p", "", message));
  if (action) {
    const button = el("button", "suggestion", action.label);
    button.type = "button";
    button.addEventListener("click", action.run);
    article.append(button);
  }
  turns.append(article);
  turns.scrollTop = turns.scrollHeight;
}

async function getJson(path) {
  const response = await fetch(path, {cache: "no-store", credentials: "same-origin"});
  let body;
  try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) throw new Error(body.detail || body.error || "서비스 응답을 확인할 수 없습니다.");
  return body;
}

function metric(label, value, tone = "") {
  const item = el("div", `metric ${tone}`);
  append(item, el("span", "", label), el("strong", "", value));
  return item;
}

function section(title, caption, id) {
  const block = el("section", "workspace-section");
  if (id) block.id = id;
  const head = el("div", "section-head");
  append(head, el("h3", "", title), caption ? el("p", "", caption) : null);
  block.append(head);
  return block;
}

function statusKorean(status) {
  return ({ACTIVE: "작동 중", PAUSED: "일시정지", HELD: "확인 필요", FAILED: "실패"})[status] || status;
}

function renderRoster(story) {
  for (const button of document.querySelectorAll("[data-role]")) {
    const employee = story.employees.find((item) => item.role === button.dataset.role);
    button.className = employee.status.toLowerCase();
    button.querySelector("i").textContent = statusKorean(employee.status);
    button.title = `${employee.responsibility} · ${employee.next_action}`;
  }
}

function renderMandate(story) {
  const block = section("확인된 조건", "모델 출력이 아니라 이 replay에 고정된 정책입니다.", "conditions");
  const grid = el("div", "condition-grid");
  append(grid,
    metric("운용 원금", `${exactNumber(story.mandate.amount)} ${story.mandate.asset}`),
    metric("즉시 보유", `${exactNumber(story.mandate.liquid_reserve)} ${story.mandate.asset}`),
    metric("운용 기간", `${story.mandate.horizon_days}일`),
    metric("차입", story.mandate.borrowing_consent ? "허용" : "허용 안 함", "safe"));
  block.append(grid, el("p", "policy-line", story.mandate.withdrawal_summary));
  return block;
}

function usageText(usage) {
  if (usage.status !== "ACTUAL") return "실제 호출 없음 · 토큰/지연/에너지 미측정";
  return `입력 ${exactNumber(usage.prompt_tokens)} · 출력 ${exactNumber(usage.completion_tokens)} · ${exactNumber(usage.latency_ms)} ms`;
}

function renderRuns(story) {
  const block = section("조건을 바꾼 두 번의 계산", "같은 snapshot에서 즉시 보유 조건만 바꿨습니다.", "run-comparison");
  const grid = el("div", "run-grid");
  for (const run of story.runs) {
    const card = el("article", "run-card");
    append(card, el("span", "run-id", `RUN ${run.run_id}`), el("strong", "", run.condition),
      el("p", "", usageText(run.model_usage)),
      el("code", "", `계획 ${shortHash(run.selected_plan_hash)}`));
    grid.append(card);
  }
  block.append(grid);
  return block;
}

function renderProducts(story) {
  const block = section("검색한 TRON 상품", "수익 원리와 부채 경로를 분리해 표시합니다.", "products");
  const list = el("div", "product-list");
  for (const product of story.products) {
    const card = el("article", `product ${product.decision.toLowerCase()}`);
    const top = el("div", "product-top");
    const label = el("div");
    append(label, el("strong", "", product.name), el("small", "", `${product.protocol} · ${product.kind}`));
    const decision = el("span", "decision", product.decision === "INCLUDED" ? "계획 포함" : "이번 계획 제외");
    append(top, label, decision);
    const rate = product.rate.value === null ? "수익률 미확인" : `${product.rate.value} ${product.rate.unit}`;
    append(card, top, el("p", "rationale", product.rationale), metric("관측 금리", rate),
      metric("출금", product.liquidity), metric("주요 위험", product.risk),
      el("code", "source-hash", `${product.source.source_id} · ${shortHash(product.source.capture_hash)}`));
    list.append(card);
  }
  block.append(list);
  return block;
}

function weightLabel(weight, story) {
  const product = story.products.find((item) => item.product_id === weight.product_id);
  return `${product ? product.name : weight.product_id} ${(weight.bps / 100).toFixed(0)}%`;
}

function renderPlans(story) {
  const block = section("배분안 2개", "순수익은 고정 가정의 replay 결과이며 실제 수익이 아닙니다.", "plans");
  const grid = el("div", "plans-grid");
  story.plans.forEach((plan, index) => {
    const card = el("article", `plan-card ${index ? "growth" : "conservative"}`);
    card.dataset.planHash = plan.plan_hash;
    const top = el("div", "plan-top");
    append(top, el("div", "plan-rank", `0${index + 1}`), el("h4", "", plan.name),
      el("span", "plan-hash", shortHash(plan.plan_hash)));
    const bar = el("div", "allocation-bar");
    let allocated = 0;
    for (const weight of plan.weights) {
      const segment = el("span", "");
      segment.style.width = `${weight.bps / 100}%`;
      segment.title = weightLabel(weight, story);
      bar.append(segment);
      allocated += weight.bps;
    }
    const cash = el("span", "cash-segment");
    cash.style.width = `${Math.max(0, 100 - allocated / 100)}%`;
    cash.title = "현금 보유";
    bar.append(cash);
    const labels = el("div", "weight-labels");
    plan.weights.forEach((weight) => labels.append(el("span", "", weightLabel(weight, story))));
    labels.append(el("span", "", `현금 ${exactNumber(plan.cash_amount)} ${story.mandate.asset}`));
    const metrics = el("div", "plan-metrics");
    append(metrics, metric("가정 순수익", `${exactNumber(plan.net_income_base)} ${story.mandate.asset}`),
      metric("비용", `${exactNumber(plan.total_cost_base)} ${story.mandate.asset}`),
      metric("최대 stress", `${exactNumber(plan.worst_stress_loss_base)} ${story.mandate.asset}`));
    const choose = el("button", "choose-plan", state.selectedPlan === plan.plan_hash ? "선택됨" : "이 안 검토");
    choose.type = "button";
    choose.addEventListener("click", () => {
      state.selectedPlan = plan.plan_hash;
      localStorage.setItem(`whollet:selected:${story.story_hash}`, plan.plan_hash);
      renderWorkspace(story);
      document.querySelector("#approval")?.scrollIntoView({behavior: "smooth", block: "start"});
    });
    append(card, top, bar, labels, metrics, el("p", "plan-note", plan.exit_summary),
      el("p", "plan-note", plan.vault_summary), choose);
    grid.append(card);
  });
  block.append(grid);
  return block;
}

function renderApproval(story) {
  const selected = story.plans.find((plan) => plan.plan_hash === state.selectedPlan);
  const block = section("승인 전 확인", "여기서 확인해도 지갑 서명이나 체인 전송은 일어나지 않습니다.", "approval");
  if (!selected) {
    block.append(el("div", "approval-empty", "위의 두 안 중 하나를 먼저 선택해 주세요."));
    return block;
  }
  const card = el("article", "approval-card");
  const header = el("div", "approval-title");
  append(header, el("div", "shield", "✓"), el("div", "", `${selected.name} 검토`),
    el("span", "blocked", story.approval.status === "READY" ? "지갑 검토 가능" : "실행 잠김"));
  const facts = el("div", "approval-facts");
  append(facts, metric("금액", `${exactNumber(story.approval.amount)} ${story.approval.asset}`),
    metric("수취인", shortHash(story.approval.recipient)), metric("네트워크", story.approval.network),
    metric("서명 / 체인", `${story.approval.signature_status} / ${story.approval.chain_status}`));
  card.append(header, facts);
  const reasons = el("ul", "blocker-list");
  story.approval.reason_codes.forEach((reason) => reasons.append(el("li", "", reason)));
  card.append(reasons);
  const reviewedKey = `whollet:reviewed:${story.story_hash}:${selected.plan_hash}`;
  const button = el("button", "review-button",
    localStorage.getItem(reviewedKey) ? "Replay 검토 완료" : "Replay 내용 확인");
  button.type = "button";
  button.addEventListener("click", () => {
    localStorage.setItem(reviewedKey, "1");
    button.textContent = "Replay 검토 완료";
    addTurn("assistant", "VAULT", "검토 기록만 저장했습니다. 지갑 요청, 서명, 전송은 만들지 않았습니다.");
    toast("검토 기록 저장 · 실행 효과 없음");
  });
  card.append(button);
  block.append(card);
  return block;
}

function renderEvidence(story) {
  const block = section("근거와 제출 상태", "완료되지 않은 요구는 그대로 미완으로 표시합니다.", "evidence");
  const status = el("div", "evidence-status");
  append(status, metric("TRON B", story.evidence.tron_b_status),
    metric("Furiosa A", story.evidence.furiosa_a_status),
    metric("실거래", story.evidence.live_execution_status));
  block.append(status);
  const details = el("details", "artifact-list");
  details.append(el("summary", "", `검증 artifact ${story.evidence.artifacts.length}개`));
  story.evidence.artifacts.forEach((artifact) => {
    const line = el("div", "artifact-row");
    append(line, el("strong", "", artifact.name), el("span", "", artifact.status),
      el("code", "", shortHash(artifact.sha256)));
    details.append(line);
  });
  block.append(details);
  const blockers = el("ul", "global-blockers");
  story.blockers.forEach((reason) => blockers.append(el("li", "", reason)));
  block.append(blockers);
  return block;
}

function renderWorkspace(story) {
  state.story = story;
  if (!story.plans.some((plan) => plan.plan_hash === state.selectedPlan)) {
    const savedPlan = localStorage.getItem(`whollet:selected:${story.story_hash}`);
    state.selectedPlan = story.plans.some((plan) => plan.plan_hash === savedPlan)
      ? savedPlan : null;
  }
  workspace.classList.add("open");
  byId("workspace-title").textContent = story.headline;
  byId("mode-badge").textContent = story.mode.replaceAll("_", " ");
  byId("mode-badge").className = `mode-badge ${story.mode.toLowerCase()}`;
  byId("story-time").textContent = timeLabel(story.evidence.snapshot_as_of);
  workspaceBody.replaceChildren(renderMandate(story), renderRuns(story), renderProducts(story),
    renderPlans(story), renderApproval(story), renderEvidence(story));
  renderRoster(story);
  localStorage.setItem("whollet:last-story", story.story_hash);
  byId("source-state").textContent = `snapshot ${shortHash(story.evidence.snapshot_hash)}`;
}

async function loadReplay({announce = true} = {}) {
  try {
    const story = await getJson("/api/demo/story");
    renderWorkspace(story);
    if (announce) addTurn("assistant", "ALPHA",
      "실제 모델 호출 대신 검증된 historical replay를 열었습니다. 조건 A/B의 계산 변화와 실행이 잠긴 이유를 함께 보여드립니다.",
      {label: "배분안 보기 →", run: () => workspace.classList.add("open")});
  } catch (error) {
    addTurn("assistant error", "WATCH", `기록을 열지 못했습니다. ${error.message}`);
  }
}

byId("open-replay").addEventListener("click", () => loadReplay());
byId("close-workspace").addEventListener("click", () => workspace.classList.remove("open"));
byId("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = byId("message");
  const message = input.value.trim();
  if (!message) return;
  addTurn("user", "", message);
  input.value = "";
  byId("send").disabled = true;
  await loadReplay();
  byId("send").disabled = false;
});

for (const button of document.querySelectorAll("[data-role]")) {
  button.addEventListener("click", () => {
    if (!state.story) return toast("먼저 대화에서 기록을 열어 주세요.");
    const employee = state.story.employees.find((item) => item.role === button.dataset.role);
    toast(`${employee.name} · ${statusKorean(employee.status)} · ${employee.next_action}`);
  });
}

getJson("/healthz").then((health) => {
  byId("service-state").textContent = health.status === "ok" ? "서비스 연결됨" : "서비스 확인 필요";
  byId("service-dot").classList.toggle("live", health.status === "ok");
}).catch(() => { byId("service-state").textContent = "서비스 연결 실패"; });

if (localStorage.getItem("whollet:last-story")) loadReplay({announce: false});
