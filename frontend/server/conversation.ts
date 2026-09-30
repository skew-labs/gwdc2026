import type { Workspace, MessageCard } from "../src/api/contracts";

type IntentResult = {
  intent?: string | null;
  status?: string;
  patch?: Record<string, unknown>;
  reason_codes?: string[];
  pending_proposal?: boolean;
};
export const portfolioRequest = (text: string) =>
  /portfolio|holdings|wallet balance|rebalance|performance|actual (profit|income|return)|how.*(doing|now)|current (status|state)|포트폴리오|잔액|보유\s*자산|리[밸벨]런|성과|실현\s*수익|수익\s*현황|얼마.*벌|현재.*(어때|어떻|상태)|지금.*(어때|어떻|상태)|조건.*(맞아|지키|벗어|위반)|투자.*(어때|상태)/i.test(
    text,
  );
export const portfolioOnlyRequest = (text: string) =>
  portfolioRequest(text) &&
  !/revise|change|invest|allocate|keep|retain|투자|바꿔|변경|수정|분산|\d+\s*(trx|usdt|달러|%)/i.test(
    text,
  );
export const leverageRequest = (text: string) =>
  /usdd|leverage|borrow.*(resupply|reinvest)|loop.*(yield|apy)|담보.*(대출|차입|발행)|반복.*(차입|예치)|재예치|레버리지/i.test(text);
export const comparisonRequest = (text: string) =>
  /compare|two plans|options|비교|선택지|두\s*(개|가지).*플랜/i.test(text);

/** Financial statements come from typed service data; the model extracts intent. */
export function financialReply(
  w: Workspace,
  intent: IntentResult | undefined,
  text: string,
): { text: string; cards: MessageCard[] } | null {
  if (portfolioRequest(text) && !Object.keys(intent?.patch || {}).length) {
    const review = w.portfolio_review;
    const performance = w.performance;
    const fmt = (v: { value: string; symbol: string } | null | undefined) => v ? `${v.value} ${v.symbol}` : "unavailable";
    const actual = performance ? `\n\n**Performance as of ${performance.as_of}**\n- Original net forecast, full horizon: ${fmt(performance.expected_return)}\n- Expected net to date: ${fmt(performance.expected_to_date)}\n- Actual net to date: ${fmt(performance.net_income)}\n- Accrued / realized position income before fees: ${fmt(performance.accrued)} / ${fmt(performance.realized)}\n- Fees paid: ${fmt(performance.fees)}\n- Actual minus expected to date: ${fmt(performance.variance)}\n\nDeposits and wallet cash are not income. Unmeasured income remains unavailable.` : "";
    if (review)
      return {
        text: `**${review.status.replaceAll("_", " ")}** · ${review.reason}\n\nChecked ${review.observed_at}. ${review.policy_hash ? "This review uses your confirmed conditions; pending chat edits are excluded." : "Confirm your conditions to enable compliance checks."}${actual}\n\nNo transaction has been authorized.`,
        cards: [{ kind: "review", target_id: review.id }],
      };
    const cash =
      w.balances.map((a) => `${a.value} ${a.symbol}`).join(", ") ||
      "not observed yet";
    const fee = w.performance?.fees;
    return {
      text: `Your latest recorded ${w.network === "nile" ? "Nile testnet" : "TRON mainnet"} holdings are below. Wallet cash: **${cash}**.${fee ? ` Recorded execution fees: **${fee.value} ${fee.symbol}**.` : ""} Open the portfolio for the observation time and position details.`,
      cards: [
        { kind: "portfolio", target_id: w.performance?.as_of || "latest" },
      ],
    };
  }
  const pending = w.intent;
  if (
    pending &&
    Object.keys(pending.patch).length &&
    (Object.keys(intent?.patch || {}).length ||
      (intent?.pending_proposal &&
        /confirm|conditions|mandate|approve|조건|승인|확정|진행|괜찮|오케이/i.test(
          text,
        )) ||
      comparisonRequest(text))
  ) {
    const known = pending.known || pending.patch;
    const capital = known.capital as
      { amount: string; asset: string }[] | undefined;
    const cash = known.immediate_cash as
      | { kind: string; value?: number; amount?: string; asset?: string }
      | undefined;
    const lines = [
      capital?.length
        ? `**${capital.map((x) => `${x.amount} ${x.asset}`).join(" + ")}**`
        : null,
      typeof known.horizon_seconds === "number"
        ? `${known.horizon_seconds / 86400} days`
        : null,
      cash?.kind === "BPS"
        ? `${Number(cash.value) / 100}% available as cash`
        : null,
      known.risk_profile
        ? `${known.risk_profile === "growth" ? "Aggressive" : known.risk_profile} risk preference`
        : null,
    ].filter(Boolean);
    const borrowing =
      known.borrowing_consent === true
        ? "Borrowing still needs explicit debt limits and policy review."
        : "Borrowing permission has not been expanded.";
    const questions = pending.questions
      .slice(0, 2)
      .map((q) => q.question)
      .join(" ");
    return {
      text: `Draft conditions: ${lines.join(" · ") || "review the extracted fields below"}. ${borrowing}\n\n${questions || "Review the conditions and retained spending limits below. Confirm & compare will check current routes and show two eligible plans when available, or explain why keeping cash is preferable."}\n\nThese edits are not confirmed and no allocation has been executed.`,
      cards: [
        {
          kind: "conditions",
          target_id: pending.request_hash || pending.created_at,
        },
      ],
    };
  }
  if (leverageRequest(text) && /apy|rate|yield|compare|available|cost|fee|check|loop|leverage|금리|수익|수수료|반복|레버리지|가능|확인|비교/i.test(text)) {
    const review = w.usdd_review;
    if (!review || review.network !== w.network || !Number.isFinite(Date.parse(review.expires_at)) || Date.parse(review.expires_at) <= Date.now())
      return { text: "I could not obtain a current USDD route assessment. No loan, reinvestment or transaction has been approved.", cards: [] };
    const f = review.facts;
    const percent = (v: string) => `${(Number(v) * 100).toLocaleString("en-US", { maximumFractionDigits: 3 })}%`;
    const rows = [
      f.energy_sun !== null ? `Energy price: **${f.energy_sun} sun per unit**. Energy costs also exist on mainnet; available resources can reduce TRX burn.` : null,
      f.supply_apy !== null && f.borrow_apy !== null ? `JustLend USDD base rates: **${percent(f.supply_apy)} supply / ${percent(f.borrow_apy)} borrow**, annualized from current per-block rates. These variable forecasts exclude incentives.` : null,
      f.collaterals.length ? `Vault minimum debt: ${f.collaterals.map(c => `**${c.minimum_debt_usdd} USDD** (${c.ilk})`).join(", ")}.` : null,
      f.token_match === false ? `Vault USDD: \`${f.vault_token}\`. Destination token: \`${f.destination_token}\`. These are different contracts.` : null,
    ].filter(Boolean);
    return { text: `**USDD route assessment · ${w.network === "nile" ? "Nile testnet" : "TRON mainnet"}**\n\n${rows.map(r => `- ${r}`).join("\n")}\n\n${review.reason}\n\nChecked ${review.observed_at}. No transaction was sent.`, cards: [] };
  }
  if (comparisonRequest(text)) {
    if (w.mandate?.status === "DRAFT")
      return {
        text: "Review and confirm these conditions to compare eligible plans. If fewer than two routes satisfy your limits and costs, the comparison will explain the blockers.",
        cards: [{ kind: "mandate", target_id: w.mandate.hash }],
      };
    if (w.comparison)
      return {
        text:
          w.comparison.status === "READY"
            ? "Here are the two calculated plans. Compare their allocations and costs, then choose one to review."
            : w.comparison.reason ||
              "Two eligible plans are not currently available within your limits.",
        cards: [{ kind: "plans", target_id: w.comparison.id }],
      };
    return {
      text: "Set and review your allocation conditions first. Then I can check which plans are viable within your limits and costs.",
      cards: [{ kind: "conditions", target_id: "new" }],
    };
  }
  if (
    intent?.status === "MODEL_OUTPUT_REJECTED" ||
    intent?.status === "MODEL_UNAVAILABLE"
  )
    return {
      text: "I could not reliably extract those conditions. Your confirmed policy is unchanged. You can review the fields directly below.",
      cards: [
        { kind: "conditions", target_id: pending?.request_hash || "new" },
      ],
    };
  return null;
}
