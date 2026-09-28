const turns = document.getElementById("turns");
const dialog = document.getElementById("needs-dialog");
const needsForm = document.getElementById("needs-form");
const workspace = document.getElementById("workspace");
const workspaceBody = document.getElementById("workspace-body");

function node(tag, className, content) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (content !== undefined) element.textContent = String(content);
  return element;
}

function turn(kind, message, buttonLabel, onClick) {
  const element = node("div", `turn ${kind}`);
  if (kind !== "user") element.append(node("div", "turn-sender", "Whollet"));
  element.append(node("div", "", message));
  if (buttonLabel) {
    const button = node("button", "inline-action", buttonLabel);
    button.type = "button";
    button.addEventListener("click", onClick);
    element.append(button);
  }
  turns.append(element);
  turns.scrollTop = turns.scrollHeight;
}

async function api(path, payload) {
  const response = await fetch(path, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload), cache: "no-store"
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || result.reason || "요청을 처리하지 못했습니다.");
  return result;
}

function fillDraft(draft = {}) {
  for (const name of ["asset", "amount", "liquid_reserve", "horizon_days", "risk"]) {
    needsForm.elements[name].value = draft[name] ?? "";
  }
}

function openDialog(draft = {}) {
  fillDraft(draft);
  dialog.showModal();
  needsForm.elements.asset.focus();
}

document.getElementById("cancel-needs").addEventListener("click", () => dialog.close());
document.getElementById("cancel-needs-bottom").addEventListener("click", () => dialog.close());
document.getElementById("close-workspace").addEventListener("click", () => workspace.classList.remove("open"));

document.getElementById("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.getElementById("message");
  const text = input.value.trim();
  if (!text) return;
  turn("user", text);
  input.value = "";
  document.getElementById("send").disabled = true;
  try {
    const result = await api("/api/interpret", {message: text});
    turn("assistant", "말씀하신 조건을 읽었습니다. 금액과 기간을 확인해 주시면 당시 자료로 비교하겠습니다.", "조건 확인", () => openDialog(result.draft));
    openDialog(result.draft);
  } catch (error) {
    turn("assistant error", "현재 AI 연결을 확인할 수 없습니다. 조건을 직접 입력하면 저장된 공식 자료를 조회해 비교할 수 있습니다.", "조건 직접 입력", () => openDialog());
    openDialog();
  } finally {
    document.getElementById("send").disabled = false;
  }
});

function row(label, value, highlight = false) {
  const line = node("div", `plan-row${highlight ? " warning" : ""}`);
  line.append(node("span", "", label), node("strong", "", value));
  return line;
}

function context(label, value) {
  const line = node("div", "context-row");
  line.append(node("span", "", label), node("span", "", value));
  return line;
}

function formatAmount(value, asset) {
  const exact = String(value);
  if (!/^(0|[1-9]\d*)(\.\d+)?$/.test(exact)) return `${exact} ${asset}`;
  const [whole, fraction] = exact.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${grouped}${fraction ? `.${fraction}` : ""} ${asset}`;
}

function renderPlan(answer) {
  workspaceBody.replaceChildren();
  workspace.classList.add("open");
  document.getElementById("workspace-title").textContent = `${answer.needs.asset} 운용안 비교`;
  const market = answer.market;
  const status = node("div", "snapshot-strip");
  status.append(node("strong", "", "공식 API 수집 자료 · 검토 전"), node("span", "", ` · 비교 ${answer.as_of.slice(0, 16).replace("T", " ")} UTC · 실행 전 재조회 필요`));
  workspaceBody.append(status);

  const overview = node("div", "market-overview");
  const label = node("div");
  label.append(node("div", "market-name", `JustLend ${market.jtoken_symbol}`),
               node("div", "market-sub", `${market.underlying_symbol} 공급 시장 · ${market.status}`));
  const rate = node("div", "rate", `${(Number(answer.supply_apy) * 100).toFixed(2)}%`);
  rate.append(node("small", "", "최근 API 기본 공급 연율 · 반올림 표시"));
  overview.append(label, rate);
  workspaceBody.append(overview);
  workspaceBody.append(context("상품 주소", market.market_address));
  workspaceBody.append(context("설정한 위험 상한", `공급 배분 최대 ${(Number(answer.risk_ceiling_fraction) * 100).toFixed(0)}% · 임시 비중 정책`));
  workspaceBody.append(context("시장 가용 수량", formatAmount(answer.available_market_cash, answer.needs.asset)));
  workspaceBody.append(context("USDD 추가 보상", answer.usdd_mining_apy === null ? "미확인" : `${(Number(answer.usdd_mining_apy) * 100).toFixed(2)}% · 예상액 미포함`));
  workspaceBody.append(context("USDD 프로젝트 자료", answer.usdd_tron_earn_apy_context === null ? "미확인" : `${(Number(answer.usdd_tron_earn_apy_context) * 100).toFixed(2)}% · 직접 참여 경로 미검증`));
  const evidence = node("details", "evidence-details");
  evidence.append(node("summary", "", "조회 근거와 원문 해시"));
  evidence.append(context("공급 금리 위치", answer.supply_apy_evidence.json_path));
  evidence.append(context("시장 수량 위치", answer.available_cash_evidence.json_path));
  for (const [sourceId, ref] of Object.entries(answer.sources)) {
    evidence.append(context(sourceId, `${ref.fetched_at} · SHA-256 ${ref.sha256}`));
  }
  workspaceBody.append(evidence);
  workspaceBody.append(node("h2", "plans-title", "비교할 배분안"));

  if (answer.plans.length < 2) {
    workspaceBody.append(node("p", "workspace-empty", "현재 조건과 시장 자료로 서로 다른 두 계획을 만들 수 없습니다. 남겨 둘 금액이나 자료의 최신성을 확인해 주세요."));
  }
  for (const [index, plan] of answer.plans.entries()) {
    const card = node("article", `plan${index === 0 ? " primary-plan" : ""}`);
    const head = node("div", "plan-head");
    head.append(node("div", "plan-name", plan.name), node("div", "plan-numeral", `안 ${index + 1}`));
    card.append(head);
    const fraction = Math.max(0, Math.min(100, Number(plan.legs[1].amount) / Number(answer.needs.amount) * 100));
    const bar = node("div", "bar");
    const supply = node("div", "supply");
    supply.style.width = `${fraction}%`;
    bar.append(supply, node("div", "cash"));
    bar.lastChild.style.width = `${100 - fraction}%`;
    card.append(bar);
    const data = node("div", "plan-data");
    data.append(row("바로 보유", formatAmount(plan.legs[0].amount, answer.needs.asset)),
                row("공급 제안", formatAmount(plan.legs[1].amount, answer.needs.asset)),
                row("기간 중 수익", "연율 적용 방식 미확인", true),
                row("총 거래 비용·순수익", "견적 없음", true));
    data.append(node("p", "claim-foot", "표시된 공급 연율의 적용·복리 방식이 확인되지 않아 기간 수익을 계산하지 않았습니다. 금리 변동, 추가 보상, 거래 비용, 가격 변화도 반영 전입니다."));
    card.append(data);
    workspaceBody.append(card);
  }
  const footer = node("div", "workspace-footer");
  footer.append(node("div", "block", "구매·예치는 아직 사용할 수 없습니다. 현재 지갑 잔액, 정확한 수수료, 참여 경로와 승인 범위가 연결되면 실행 카드를 다시 확인해야 합니다."));
  const links = node("div", "source-links");
  const justlend = node("a", "", "JustLend 원천 문서");
  justlend.href = "https://docs.justlend.org/developers/apis/";
  justlend.target = "_blank";
  justlend.rel = "noopener noreferrer";
  const usdd = node("a", "", "USDD 원천 문서");
  usdd.href = "https://docs.usdd.io/developers/usdd-public-api";
  usdd.target = "_blank";
  usdd.rel = "noopener noreferrer";
  links.append(justlend, usdd);
  footer.append(links);
  workspaceBody.append(footer);
}

needsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const fields = new FormData(needsForm);
  const need = Object.fromEntries(["asset", "amount", "liquid_reserve", "horizon_days", "risk"].map(k => [k, fields.get(k)]));
  need.horizon_days = Number(need.horizon_days);
  dialog.close();
  turn("user", `확인한 조건: ${need.amount} ${need.asset}, 바로 보유 ${need.liquid_reserve}, ${need.horizon_days}일, ${needsForm.elements.risk.selectedOptions[0].textContent}`);
  try {
    const answer = await api("/api/plan", need);
    renderPlan(answer);
    turn("assistant", answer.plans.length >= 2 ?
      "같은 시점 자료로 유동성 우선안과 공급 비중 우선안을 계산했습니다. 오른쪽 창에서 배분과 근거를 비교해 주세요. 현재 비용 견적이 없어 구매는 열리지 않습니다." :
      "현재 조건에서는 실질적으로 다른 두 안을 계산하지 못했습니다. 오른쪽 창의 자료와 조건을 확인해 주세요.", "배분안 보기", () => workspace.classList.add("open"));
  } catch (error) {
    turn("assistant error", `비교안을 만들 수 없습니다: ${error.message} 공식 자료 수집과 유효 시점을 확인해 주세요.`);
  }
});

fetch("/api/status", {cache:"no-store"}).then(r => r.json()).then(status => {
  const count = Object.values(status.sources).filter(Boolean).length;
  const fresh = Object.values(status.sources).filter(source => source?.fresh).length;
  document.getElementById("source-state").textContent = `${fresh}/4개 필수 원천 최신 · ${count}/4개 수집`;
  document.getElementById("model-state").textContent = status.model_configured ? "Qwen 설정됨 · 호출 미확인" : "Qwen 연결 대기";
}).catch(() => {
  document.getElementById("source-state").textContent = "데이터 상태 조회 실패";
  document.getElementById("model-state").textContent = "연결 확인 실패";
});
