import { useState } from "react";
import type { MessageCard } from "../api/contracts";
import type { Machine } from "../api/useMachine";
import { Button, Row } from "./ui";
import { MandateEditor } from "./MandateEditor";
import { MandateCard, PlanCards } from "./PlanCards";
import { ExecutionCard } from "./ExecutionCard";
import { PortfolioReviewCard } from "./PortfolioReview";
import { PortfolioChart } from "./PortfolioChart";
import { money } from "../lib/format";

export function ConversationCard({
  card,
  m,
  openConditions,
  openPortfolio,
}: {
  card: MessageCard;
  m: Machine;
  openConditions: () => void;
  openPortfolio: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const w = m.workspace.data;
  if (!w) return null;
  const archived = (label: string) => (
    <div className="conversation-reference">
      <span>{label}</span>
      <Button secondary onClick={openConditions}>
        View current conditions
      </Button>
    </div>
  );
  if (card.kind === "review")
    return <PortfolioReviewCard m={m} targetId={card.target_id} />;
  if (card.kind === "portfolio")
    return (
      <div className="conversation-portfolio">
        <PortfolioChart workspace={w} />
        <Button secondary onClick={openPortfolio}>
          Open full portfolio
        </Button>
      </div>
    );
  if (card.kind === "plans")
    return w.comparison?.id === card.target_id ? (
      <PlanCards m={m} onReview={() => {}} onEdit={openConditions} />
    ) : (
      archived("This comparison has been replaced by a newer review.")
    );
  if (card.kind === "execution")
    return w.graph?.id === card.target_id ? (
      <ExecutionCard key={w.graph.hash} m={m} />
    ) : (
      archived("This execution review is retained in Run evidence.")
    );
  if (card.kind === "mandate")
    return w.mandate?.hash === card.target_id ? (
      editing ? (
        <section className="conversation-conditions artifact-body">
          <div className="editor-heading">
            <strong>Edit conditions</strong>
            <Button secondary onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
          <MandateEditor m={m} onSaved={() => setEditing(false)} />
        </section>
      ) : (
        <MandateCard m={m} inConversation edit={() => setEditing(true)} />
      )
    ) : (
      archived("These conditions have since been updated.")
    );
  const current =
    card.target_id === "new" ||
    card.target_id === (w.intent?.request_hash || w.intent?.created_at);
  if (!current)
    return archived("These draft edits have been reviewed or superseded.");
  const known = w.intent?.known || w.intent?.patch || {};
  const capital = known.capital as
    { asset: string; amount: string }[] | undefined;
  const reserve = known.immediate_cash as
    { kind: string; value?: number } | undefined;
  return (
    <section
      className="conversation-conditions"
      aria-label="Proposed conditions"
    >
      <div className="artifact-heading">
        <div>
          <strong>Draft conditions</strong>
          <small>Review before comparing</small>
        </div>
        <span className="badge neutral">Not confirmed</span>
      </div>
      <div className="artifact-body">
        <div className="summary-grid">
          {capital?.[0] && (
            <Row label="Capital">
              {money({
                value: capital[0].amount,
                symbol: capital[0].asset,
                decimals: 6,
              })}
            </Row>
          )}
          {typeof known.horizon_seconds === "number" && (
            <Row label="Horizon">{known.horizon_seconds / 86400} days</Row>
          )}
          {reserve?.kind === "BPS" && (
            <Row label="Available cash">{Number(reserve.value) / 100}%</Row>
          )}
          {typeof known.risk_profile === "string" && (
            <Row label="Risk preference">
              {known.risk_profile === "growth"
                ? "Aggressive"
                : known.risk_profile}
            </Row>
          )}
        </div>
        {!editing && (
          <div className="condition-actions">
            <Button onClick={() => setEditing(true)}>Review & compare</Button>
            <Button secondary onClick={openConditions}>
              Open condition editor
            </Button>
          </div>
        )}
        {editing && (
          <>
            <div className="editor-heading">
              <strong>Review all limits</strong>
              <Button secondary onClick={() => setEditing(false)}>
                Cancel
              </Button>
            </div>
            <MandateEditor m={m} onSaved={() => setEditing(false)} />
          </>
        )}
      </div>
    </section>
  );
}
