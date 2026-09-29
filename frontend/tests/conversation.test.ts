import { expect, it } from "vitest";
import type { Workspace } from "../src/api/contracts";
import { financialReply, portfolioOnlyRequest } from "../server/conversation";
import { portfolioAllocation } from "../src/lib/portfolio";
function workspace(): Workspace {
  return {
    network: "nile",
    balances: [
      { symbol: "TRX", value: "990.9106", decimals: 6 },
      { symbol: "USDT", value: "10", decimals: 6 },
    ],
    positions: [
      {
        id: "position",
        network: "nile",
        product: "justlend.v1.jTRX",
        current_value: { symbol: "TRX", value: "0.999999", decimals: 6 },
        principal: { symbol: "TRX", value: "1", decimals: 6 },
      },
    ],
    performance: {
      fees: { symbol: "TRX", value: "8.0894", decimals: 6 },
      as_of: "2026-09-29T11:38:46Z",
    },
    intent: null,
    mandate: null,
    comparison: null,
  } as Workspace;
}
it("sums observed cash and valued positions exactly without mixing assets or substituting principal", () => {
  const w = workspace();
  const trx = portfolioAllocation(w, "TRX");
  expect(trx.total.value).toBe("991.910599");
  expect(trx.slices).toHaveLength(2);
  expect(portfolioAllocation(w, "USDT").total.value).toBe("10");
  w.positions[0].current_value = null;
  expect(portfolioAllocation(w, "TRX").total.value).toBe("990.9106");
  expect(portfolioAllocation(w, "TRX").unpriced).toBe(1);
  w.balances = [];
  expect(portfolioAllocation(w, "TRX").slices).toEqual([]);
});
it("uses bigint arithmetic for large balances and excludes other networks", () => {
  const w = workspace();
  w.balances = [
    { symbol: "TRX", value: "9007199254740993.000001", decimals: 6 },
  ];
  w.positions[0].network = "mainnet";
  const result = portfolioAllocation(w, "TRX");
  expect(result.total.value).toBe("9007199254740993.000001");
  expect(result.slices[0].percent).toBe(100);
});
it("portfolio answers use observed fees and never include unrelated conditions", () => {
  const r = financialReply(workspace(), undefined, "포트폴리오 보여줘")!;
  expect(r.text).toContain("8.0894 TRX");
  expect(r.text).not.toContain("30 TRX");
  expect(r.cards.map((c) => c.kind)).toEqual(["portfolio"]);
});
it("renders pending conditions without granting consent, inventing allocations or repeating known questions", () => {
  const w = workspace();
  w.intent = {
    request_hash: "draft",
    status: "DRAFT_READY",
    reason_codes: [],
    source_text: "test-only",
    created_at: "now",
    patch: { capital: [{ asset: "TRX", amount: "100" }] },
    known: {
      capital: [{ asset: "TRX", amount: "100" }],
      horizon_seconds: 604800,
      immediate_cash: { kind: "BPS", value: 2000 },
      risk_profile: "growth",
      borrowing_consent: false,
    },
    questions: [],
  } as Workspace["intent"];
  // A previous portfolio check must never swallow newly extracted conditions.
  w.portfolio_review = {
    id: "earlier-review",
    status: "HOLD",
    reason: "Earlier check",
  } as Workspace["portfolio_review"];
  const r = financialReply(
    w,
    { patch: { capital: [] } },
    "100trx 하이리스크 오케이",
  )!;
  expect(r.text).toContain("100 TRX");
  expect(r.text).toContain("7 days");
  expect(r.text).toContain("20%");
  expect(r.text).toContain("not confirmed");
  expect(r.text).toContain("not been expanded");
  expect(r.text).not.toContain("80 TRX");
  expect(r.cards).toEqual([{ kind: "conditions", target_id: "draft" }]);
  expect(
    financialReply(
      w,
      { patch: {}, pending_proposal: true },
      "What is JustLend?",
    ),
  ).toBeNull();
  expect(
    financialReply(
      w,
      { patch: {}, pending_proposal: true },
      "Confirm these conditions",
    )!.cards[0].kind,
  ).toBe("conditions");
});
it("requires review before comparison and points to real calculated IDs", () => {
  const w = workspace();
  w.mandate = { status: "DRAFT", hash: "draft-hash" } as Workspace["mandate"];
  expect(financialReply(w, undefined, "Compare two plans")!.cards).toEqual([
    { kind: "mandate", target_id: "draft-hash" },
  ]);
  w.mandate!.status = "CONFIRMED";
  w.comparison = {
    id: "real-comparison",
    status: "READY",
  } as Workspace["comparison"];
  expect(financialReply(w, undefined, "Compare two plans")!.cards).toEqual([
    { kind: "plans", target_id: "real-comparison" },
  ]);
});

it("routes current-state and rebalancing questions to fresh read-only reviews", () => {
  for (const text of [
    "현재 상태 어때?",
    "현재 조건에서 리밸런싱 필요해?",
    "지금 어떻냐",
    "포트폴리오 리밸런싱 검토해줘",
    "How is my portfolio doing?",
    "Rebalance my portfolio",
    "현재 성과 보여줘",
    "실현 수익 얼마야?",
    "Show actual income and performance",
  ])
    expect(portfolioOnlyRequest(text)).toBe(true);
  expect(portfolioOnlyRequest("포트폴리오 조건 바꿔 100 TRX 투자")).toBe(false);
});
it("failed review cannot become a reassuring hold recommendation", () => {
  const w = workspace();
  w.portfolio_review = {
    id: "failed-read",
    network: "nile",
    policy_hash: "policy",
    status: "DATA_UNAVAILABLE",
    reason: "Current redemption cost unavailable",
    observed_at: "2026-09-29T13:00:00Z",
    expires_at: "2026-09-29T13:05:00Z",
    source: "ON_DEMAND",
    checks: [],
    hold: null,
    alternatives: [],
    suggested: null,
    execution_authority: "NONE",
    limitations: [],
    snapshot_hash: null,
    block: null,
  };
  const r = financialReply(w, undefined, "현재 상태 어때?")!;
  expect(r.text).toContain("DATA UNAVAILABLE");
  expect(r.text).not.toContain("Keep your current allocation");
  expect(r.cards).toEqual([{ kind: "review", target_id: "failed-read" }]);
});

it("USDD replies use fresh network-bound service rates and preserve absence of trade authority", () => {
  const w = workspace();
  w.usdd_review = {
    network: "nile", observed_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 60000).toISOString(),
    reason: "Different token contracts. Borrowing has not been permitted. No signed execution adapter is connected.",
    facts: { energy_sun: "100", supply_apy: "0.529264", borrow_apy: "0.541723", token_match: false,
      vault_token: "vault-token", destination_token: "destination-token", collaterals: [{ ilk: "TRX-C", minimum_debt_usdd: "600" }] },
  } as Workspace["usdd_review"];
  const r = financialReply(w, undefined, "USDD 반복 차입하면 APY 높아?")!;
  expect(r.text).toContain("52.926% supply / 54.172% borrow");
  expect(r.text).toContain("600 USDD");
  expect(r.text).toContain("different contracts");
  expect(r.text).toContain("No transaction was sent");
  expect(r.cards).toEqual([]);
  expect(financialReply(w, undefined, "What is USDD?")).toBeNull();
  w.usdd_review!.network = "mainnet";
  expect(financialReply(w, undefined, "USDD APY")!.text).toContain("could not obtain");
  w.usdd_review!.network = "nile";
  w.usdd_review!.expires_at = "invalid";
  expect(financialReply(w, undefined, "USDD APY")!.text).toContain("could not obtain");
  w.usdd_review!.expires_at = new Date(Date.now() - 1).toISOString();
  expect(financialReply(w, undefined, "USDD APY")!.text).not.toContain("52.926%");
});
