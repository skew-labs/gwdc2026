import { useState } from "react";
import type { Machine } from "../api/useMachine";
import { Constraints } from "../api/contracts";
import { Alert, Button } from "./ui";
import { toUnits } from "../lib/format";

type Money = { asset: string; amount: string };
type Terms = {
  allowed_actions?: string[];
  risk_profile: string;
  capital: Money[];
  horizon_seconds: number;
  immediate_cash: { kind: string; value?: number; amount?: string };
  price_exposure_caps_bps: Record<string, number>;
  protocol_caps_bps: Record<string, number>;
  borrowing: {
    consent: boolean;
    max_debt: Money;
    min_collateral_ratio_bps: number;
    liquidation_buffer_bps: number;
  };
  limits: Record<string, Money>;
  withdrawals: {
    after_seconds: number;
    minimum: { kind: string; value?: number; amount?: string };
  }[];
};
export function MandateEditor({
  m,
  onSaved,
}: {
  m: Machine;
  onSaved: () => void;
}) {
  const existing = m.workspace.data?.mandate;
  const t = existing?.terms as Terms | undefined;
  const c = existing?.constraints;
  const intent = m.workspace.data?.intent;
  const p = intent?.patch || {};
  const proposedCapital = Array.isArray(p.capital)
    ? (p.capital[0] as Money | undefined)
    : undefined;
  const cashPatch = p.immediate_cash as Terms["immediate_cash"] | undefined;
  const initial = (value: unknown, fallback = "") =>
    typeof value === "string" || typeof value === "number"
      ? String(value)
      : fallback;
  const [asset, setAsset] = useState(
    proposedCapital?.asset ||
      c?.capital.symbol ||
      (typeof p.base_asset === "string" ? p.base_asset : "USDT"),
  );
  const [capital, setCapital] = useState(
    proposedCapital?.amount || c?.capital.value || "",
  );
  const [days, setDays] = useState(
    typeof p.horizon_seconds === "number"
      ? String(p.horizon_seconds / 86400)
      : initial(c?.horizon_days),
  );
  const [cash, setCash] = useState(
    cashPatch?.kind === "BPS"
      ? String((cashPatch.value || 0) / 100)
      : initial(c ? c.min_cash_bps / 100 : undefined),
  );
  const [risk, setRisk] = useState(
    initial(p.risk_profile, t?.risk_profile || ""),
  );
  const [trx, setTrx] = useState(
    initial(c ? c.max_trx_exposure_bps / 100 : undefined),
  );
  const [usdd, setUsdd] = useState(
    initial(t ? (t.price_exposure_caps_bps.USDD ?? 0) / 100 : undefined),
  );
  const [justlend, setJustlend] = useState(
    initial(t ? (t.protocol_caps_bps.justlend ?? 0) / 100 : undefined),
  );
  const [nativeStake, setNativeStake] = useState(Boolean(t?.allowed_actions?.includes("STAKE") && t?.allowed_actions?.includes("VOTE")));
  const [nativeCap, setNativeCap] = useState(String((t?.protocol_caps_bps["tron-native"] || 10000)/100));
  const [vault, setVault] = useState(
    initial(t ? (t.protocol_caps_bps.usdd ?? 0) / 100 : undefined),
  );
  const [debt, setDebt] = useState(
    typeof p.borrowing_consent === "boolean"
      ? String(p.borrowing_consent)
      : t
        ? String(t.borrowing.consent)
        : "",
  );
  const [maxDebt, setMaxDebt] = useState(
    initial(
      (p.max_debt as Money | undefined)?.amount,
      t?.borrowing.max_debt.amount || "",
    ),
  );
  const [ratio, setRatio] = useState(
    initial(t ? t.borrowing.min_collateral_ratio_bps / 100 : undefined),
  );
  const [buffer, setBuffer] = useState(
    initial(t ? t.borrowing.liquidation_buffer_bps / 100 : undefined),
  );
  const [fee, setFee] = useState(c?.max_fee.value || "");
  const [limits, setLimits] = useState<Record<string, string>>(
    Object.fromEntries(
      [
        "single_amount",
        "cumulative_amount",
        "fee_amount",
        "daily_loss",
        "stress_loss",
      ].map((k) => [k, t?.limits[k]?.amount || ""]),
    ),
  );
  const [hours, setHours] = useState("24");
  const [withdrawals, setWithdrawals] = useState<
    Array<{ days: string; amount: string }>
  >(
    (Array.isArray(p.withdrawals)
      ? (p.withdrawals as Terms["withdrawals"])
      : t?.withdrawals || []
    ).map((w) => ({
      days: String(w.after_seconds / 86400),
      amount: w.minimum.kind === "AMOUNT" ? w.minimum.amount || "" : "",
    })),
  );
  const [planning, setPlanning] = useState<Record<string, string>>(
    Object.fromEntries(
      [
        "entry_cost",
        "exit_cost",
        "conversion_cost",
        "network_cost",
        "redemption_seconds",
        "daily_loss_bps",
        "stress_loss_bps",
      ].map((k) => [k, initial(m.workspace.data?.planning_assumptions?.[k])]),
    ),
  );
  const [error, setError] = useState("");
  const percent = (value: string) => {
    if (!/^\d+(\.\d{1,2})?$/.test(value))
      throw new Error("Enter percentages with up to two decimal places.");
    return Math.round(Number(value) * 100);
  };
  const numeric = (
    label: string,
    value: string,
    onChange: (v: string) => void,
    unit: string,
    max?: number,
  ) => (
    <label key={label}>
      {label}
      <span className="input-unit">
        <input
          aria-label={label}
          inputMode="decimal"
          type={max ? "number" : "text"}
          min="0"
          max={max}
          step="0.01"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          required
        />
        <span>{unit}</span>
      </span>
    </label>
  );
  return (
    <form
      className="mandate-form"
      onSubmit={async (e) => {
        e.preventDefault();
        setError("");
        try {
          if (toUnits(capital, 6) <= 0n)
            throw new Error("Capital must be greater than zero.");
          if (!risk || !debt)
            throw new Error("Choose a risk profile and borrowing permission.");
          if (nativeStake && (asset !== "TRX" || m.network !== "nile" || percent(nativeCap)<=0)) throw new Error("Native Stake requires Nile TRX and a positive allocation limit.");
          toUnits(fee, 6);
          Object.values(limits).forEach((v) => toUnits(v, 6));
          if (!["USDT", "USDD", "TRX"].includes(asset))
            throw new Error("Choose a supported capital asset.");
          const constraints = Constraints.parse({
            capital: { value: capital, symbol: asset, decimals: asset === "USDD" ? 18 : 6 },
            horizon_days: Number(days),
            min_cash_bps: percent(cash),
            max_trx_exposure_bps: percent(trx),
            allow_debt: debt === "true",
            allowed_protocols: [
              ...(Number(justlend) > 0 ? ["JustLend"] : []),
              ...(Number(vault) > 0 ? ["USDD"] : []),
              ...(nativeStake ? ["TRON Native"] : []),
            ],
            max_fee: { value: fee, symbol: "TRX", decimals: 6 },
          });
          const start = new Date();
          const validity = Number(hours);
          if (!Number.isInteger(validity) || validity < 1 || validity > 168)
            throw new Error("Policy validity must be 1–168 hours.");
          const terms = {
            capital: [{ asset, amount: capital }],
            base_asset: asset,
            risk_profile: risk,
            horizon_seconds: constraints.horizon_days * 86400,
            immediate_cash: { kind: "BPS", value: constraints.min_cash_bps },
            withdrawals: withdrawals.map((w) => {
              toUnits(w.amount, 6);
              return {
                after_seconds: Number(w.days) * 86400,
                minimum: { kind: "AMOUNT", asset, amount: w.amount },
              };
            }),
            price_exposure_caps_bps: {
              TRX: constraints.max_trx_exposure_bps,
              USDD: percent(usdd),
            },
            protocol_caps_bps: {
              justlend: percent(justlend),
              usdd: percent(vault),
              "tron-native": nativeStake ? percent(nativeCap) : 0,
            },
            borrowing: {
              consent: constraints.allow_debt,
              max_debt: {
                asset,
                amount: constraints.allow_debt ? maxDebt : "0",
              },
              min_collateral_ratio_bps: constraints.allow_debt
                ? percent(ratio)
                : 10000,
              liquidation_buffer_bps: constraints.allow_debt
                ? percent(buffer)
                : 0,
            },
            limits: Object.fromEntries(
              Object.entries(limits).map(([k, amount]) => [
                k,
                { asset, amount },
              ]),
            ),
            allowed_actions: [
              "HOLD",
              "SUPPLY",
              "REDEEM",
              "CLAIM",
              "REPAY",
              "WITHDRAW_COLLATERAL",
              ...(nativeStake ? ["STAKE", "UNSTAKE", "VOTE"] : []),
              ...(constraints.allow_debt
                ? ["OPEN_VAULT", "MINT_USDD", "BORROW"]
                : []),
            ],
            effective_at: start.toISOString(),
            expires_at: new Date(
              start.getTime() + validity * 3600000,
            ).toISOString(),
          };
          const assumptions = Object.fromEntries(
            Object.entries(planning).map(([key, value]) => {
              if (key.endsWith("_cost")) {
                toUnits(value, 6);
                return [key, value];
              }
              const n = Number(value);
              if (
                !value ||
                !Number.isInteger(n) ||
                n < 0 ||
                (key.endsWith("_bps") && n > 10000)
              )
                throw new Error(
                  "Complete the planning assumptions with whole numbers.",
                );
              return [key, n];
            }),
          );
          if (
            await m.saveMandate(
              constraints,
              intent?.source_text ||
                existing?.source_text ||
                "User-entered conditions",
              terms,
              assumptions,
            )
          )
            onSaved();
        } catch (err) {
          setError(
            err instanceof Error ? err.message : "Check your conditions.",
          );
        }
      }}
    >
      <p className="quiet-note">
        Save a draft, then review and confirm it before comparing plans.
      </p>
      {intent && (
        <Alert>
          Chat edits are prefilled. Existing allocation limits and cost
          assumptions are retained until you change them. An aggressive risk
          preference does not enable borrowing.
        </Alert>
      )}
      <label>
        Capital asset
        <select value={asset} onChange={(e) => setAsset(e.target.value)}>
          <option>USDT</option>
          <option>TRX</option>
          <option>USDD</option>
        </select>
      </label>
      <p className="caption">
        On Nile, TRX can use JustLend supply or Native Stake with voting. Native Stake requires the separate permission below; returns and costs stay in TRX.
      </p>
      {asset === "TRX" && m.network === "nile" && <fieldset>
        <legend>Native staking</legend>
        <label className="check-label"><input type="checkbox" checked={nativeStake} onChange={e=>setNativeStake(e.target.checked)} />Allow Stake 2.0 and representative voting</label>
        <p className="caption">Compare voting income after full entry and exit costs. TRX becomes locked until the chain unstaking delay ends. Stake and vote each require a wallet signature. Energy rental income is excluded.</p>
        {nativeStake && numeric("Maximum Native Stake allocation",nativeCap,setNativeCap,"%",100)}
      </fieldset>}
      {numeric("Starting capital", capital, setCapital, asset)}
      {numeric("Time horizon", days, setDays, "days", 365)}
      <label>
        Risk profile
        <select required value={risk} onChange={(e) => setRisk(e.target.value)}>
          <option value="">Choose a profile</option>
          <option value="cautious">Cautious</option>
          <option value="balanced">Balanced</option>
          <option value="growth">Growth</option>
        </select>
      </label>
      {cashPatch?.kind === "AMOUNT" && (
        <Alert>
          Your message requested {cashPatch.amount} in immediate cash. Enter the
          corresponding minimum percentage below.
        </Alert>
      )}
      {numeric("Minimum immediate wallet cash", cash, setCash, "%", 100)}
      <details
        className="companion-disclosure"
        open={![trx, usdd, justlend, vault].every(Boolean)}
      >
        <summary>
          Allocation limits<span>Assets & protocols</span>
        </summary>
        <fieldset className="conditions-limits">
          <legend>Allocation limits</legend>
          {numeric("Maximum TRX price exposure", trx, setTrx, "%", 100)}
          {numeric("Maximum USDD price exposure", usdd, setUsdd, "%", 100)}
          {numeric(
            "Maximum JustLend allocation",
            justlend,
            setJustlend,
            "%",
            100,
          )}
          {numeric(
            "Maximum USDD protocol allocation",
            vault,
            setVault,
            "%",
            100,
          )}
        </fieldset>
      </details>
      <details
        className="companion-disclosure"
        open={!Object.values(limits).every(Boolean) || !fee}
      >
        <summary>
          Spending & loss limits<span>Fees & exposure</span>
        </summary>
        <fieldset>
          <legend>Spending and loss limits</legend>
          {Object.entries({
            single_amount: "Maximum single allocation",
            cumulative_amount: "Maximum total invested",
            fee_amount: "Total cost budget",
            daily_loss: "Maximum daily loss",
            stress_loss: "Maximum stress loss",
          }).map(([key, label]) =>
            numeric(
              label,
              limits[key],
              (v) => setLimits({ ...limits, [key]: v }),
              asset,
            ),
          )}
          {numeric("Fee cap per transaction", fee, setFee, "TRX")}
        </fieldset>
      </details>
      <label>
        Collateralized borrowing
        <select required value={debt} onChange={(e) => setDebt(e.target.value)}>
          <option value="">Choose permission</option>
          <option value="false">Do not allow borrowing</option>
          <option value="true">Allow within explicit debt limits</option>
        </select>
      </label>
      {debt === "true" && (
        <fieldset>
          <legend>Debt protection</legend>
          {numeric("Maximum debt", maxDebt, setMaxDebt, asset)}
          {numeric("Minimum collateral ratio", ratio, setRatio, "%", 10000)}
          {numeric("Liquidation buffer", buffer, setBuffer, "%", 10000)}
        </fieldset>
      )}
      <details className="companion-disclosure" open={withdrawals.length > 0}>
        <summary>
          Withdrawal schedule
          <span>
            {withdrawals.length
              ? `${withdrawals.length} dates`
              : "No scheduled withdrawals"}
          </span>
        </summary>
        <fieldset>
          <legend>Required withdrawals</legend>
          <p className="muted">
            Amounts are cumulative. With no dates added, only the immediate cash
            requirement applies.
          </p>
          {withdrawals.map((w, i) => (
            <div key={i}>
              {numeric(
                "Withdrawal day " + (i + 1),
                w.days,
                (value) =>
                  setWithdrawals(
                    withdrawals.map((row, n) =>
                      n === i ? { ...row, days: value } : row,
                    ),
                  ),
                "days",
                365,
              )}
              {numeric(
                "Minimum recoverable " + (i + 1),
                w.amount,
                (value) =>
                  setWithdrawals(
                    withdrawals.map((row, n) =>
                      n === i ? { ...row, amount: value } : row,
                    ),
                  ),
                asset,
              )}
              <Button
                secondary
                type="button"
                onClick={() =>
                  setWithdrawals(withdrawals.filter((_, n) => n !== i))
                }
              >
                Remove date
              </Button>
            </div>
          ))}
          <Button
            secondary
            type="button"
            onClick={() =>
              setWithdrawals([...withdrawals, { days: "", amount: "" }])
            }
          >
            Add withdrawal date
          </Button>
        </fieldset>
      </details>
      <details
        className="planning-details companion-disclosure"
        open={Object.values(planning).some((v) => !v)}
      >
        <summary>Planning costs & risk assumptions</summary>
        <fieldset>
          <legend className="sr-only">Planning assumptions</legend>
          <p className="muted">
            These estimates are applied to each product in the comparison. They
            are not live execution quotes. Incentive proceeds are excluded. The
            transaction review will require fresh verified fees and funding
            routes.
          </p>
          {Object.entries({
            entry_cost: "Entry cost per product",
            exit_cost: "Exit cost per product",
            conversion_cost: "Conversion cost per product",
            network_cost: "Network cost per product",
            redemption_seconds: "Assumed redemption delay",
            daily_loss_bps: "Assumed daily loss rate",
            stress_loss_bps: "Assumed stress loss rate",
          }).map(([key, label]) =>
            numeric(
              label,
              planning[key],
              (value) => setPlanning({ ...planning, [key]: value }),
              key.endsWith("_cost")
                ? asset
                : key.endsWith("_bps")
                  ? "bps"
                  : "seconds",
            ),
          )}
        </fieldset>
      </details>
      {numeric("Policy validity", hours, setHours, "hours", 168)}
      <p className="muted">
        Permitted actions: hold, supply, redeem, claim rewards, repay and release repaid collateral
        {nativeStake ? ", stake, vote and request unstaking" : ""}
        {debt === "true"
          ? ", open a vault, mint USDD and borrow within the debt limit"
          : ""}
        .{" "}
        {debt === "true" &&
          "USDD strategies require a separate live route and cost review in Portfolio. "}
        A separate approval and wallet signature are required for each
        execution.
      </p>
      {error && <Alert tone="error">{error}</Alert>}
      <Button type="submit" disabled={!!m.busy || !!m.pending}>
        Save conditions for review
      </Button>
    </form>
  );
}
