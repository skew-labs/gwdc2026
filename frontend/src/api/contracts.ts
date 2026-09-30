import { z } from "zod";
export const Network = z.enum(["nile", "mainnet"]);
export const Role = z.enum(["alpha", "vault", "watch"]);
export type Network = z.infer<typeof Network>;
export type Role = z.infer<typeof Role>;
export const AgentInput = z.object({
  name: z.string().trim().min(1).max(60),
  role: Role,
  instructions: z.string().max(4000),
});
export const Agent = z.object({
  id: z.string(),
  name: z.string(),
  role: Role,
  instructions: z.string(),
  created_at: z.string(),
  updated_at: z.string(),
  status: z.enum(["IDLE", "WORKING", "NEEDS_INPUT"]),
  last_message: z.string().nullable(),
});
export type Agent = z.infer<typeof Agent>;
export const Agents = z.array(Agent);
export const Decimal = z.string().regex(/^-?\d+(\.\d+)?$/);
export const Amount = z.object({
  value: Decimal,
  symbol: z.string(),
  decimals: z.number().int().min(0).max(30),
});
export type Amount = z.infer<typeof Amount>;
export const Session = z.object({
  authenticated: z.boolean(),
  user_name: z.string().nullable(),
  wallet_address: z.string().nullable(),
  csrf_token: z.string(),
});
export type Session = z.infer<typeof Session>;
export const Constraints = z.object({
  capital: Amount,
  horizon_days: z.number().int().positive().max(365),
  min_cash_bps: z.number().int().min(0).max(10000),
  max_trx_exposure_bps: z.number().int().min(0).max(10000),
  allow_debt: z.boolean(),
  allowed_protocols: z.array(z.string()).min(1),
  max_fee: Amount,
});
export const Mandate = z.object({
  id: z.string(),
  version: z.number().int(),
  hash: z.string(),
  network: Network,
  status: z.enum(["DRAFT", "CONFIRMED", "SUPERSEDED"]),
  source_text: z.string(),
  constraints: Constraints,
  missing_fields: z.array(z.string()),
  confirmed_at: z.string().nullable(),
  terms: z.record(z.string(), z.unknown()).optional(),
});
export type Mandate = z.infer<typeof Mandate>;
export const MessageCard = z.object({
  kind: z.enum([
    "conditions",
    "mandate",
    "plans",
    "execution",
    "portfolio",
    "review",
  ]),
  target_id: z.string(),
});
export type MessageCard = z.infer<typeof MessageCard>;
export const Message = z.object({
  id: z.string(),
  role: Role,
  author: z.enum(["user", "agent", "system"]),
  text: z.string(),
  created_at: z.string(),
  cards: z.array(MessageCard).optional(),
});
export const Snapshot = z.object({
  id: z.string(),
  network: Network,
  observed_at: z.string(),
  expires_at: z.string(),
  source: z.string(),
  source_url: z.string().nullable(),
  block: z.string().nullable(),
  root: z.string(),
  status: z.enum(["VALID", "STALE", "UNAVAILABLE"]),
});
export const Allocation = z.object({
  product: z.string(),
  protocol: z.string(),
  amount: Amount,
  share_bps: z.number().int().min(0).max(10000),
  kind: z.enum(["SUPPLY", "CASH", "STAKE", "VAULT", "SWAP"]),
  exit_description: z.string(),
  participation_terms: z.string(),
  base_yield: Amount.nullable(),
  incentive_rewards: Amount.nullable(),
  costs: z.array(z.object({ label: z.string(), amount: Amount.nullable() })),
});
export const Plan = z.object({
  id: z.string(),
  hash: z.string(),
  title: z.string(),
  summary: z.string(),
  allocations: z.array(Allocation),
  expected_net_return: Amount.nullable(),
  estimated_fees: Amount.nullable(),
  immediate_cash: Amount,
  recoverable_cash: z.array(
    z.object({ days: z.number(), amount: Amount, evidence: z.string() }),
  ),
  risks: z.array(z.string()),
  eligible: z.boolean(),
  violations: z.array(z.string()),
});
export type Plan = z.infer<typeof Plan>;
export const Comparison = z.object({
  candidate_count:z.number().optional(), eligible_candidates:z.number().optional(), exclusion_histogram:z.record(z.string(),z.number()).optional(),
  id: z.string(),
  network: Network,
  mandate_hash: z.string(),
  snapshot_root: z.string(),
  expires_at: z.string(),
  status: z.enum(["READY", "INFEASIBLE", "STALE"]),
  plans: z.array(Plan),
  reason: z.string().nullable(),
  usdd_vault: z.object({
    status: z.enum(["INCLUDED", "EXCLUDED", "UNAVAILABLE"]),
    reason: z.string(),
  }),
  math_version: z.string(),
  adapter_version: z.string(),
  search_scope: z.string(),
});
export const StepStatus = z.enum([
  "WAITING",
  "READY",
  "AWAITING_USER_SIGNATURE",
  "SUBMITTED",
  "SUBMISSION_UNKNOWN",
  "CONFIRMED",
  "POSITION_RECONCILED",
  "FAILED",
  "BLOCKED",
  "WAITING_EXIT",
]);
export const Step = z.object({
  native_votes:z.array(z.object({vote_address:z.string(),vote_count:z.number().int().positive()})).nullable().optional(),
  call_data: z.string().regex(/^[a-f0-9]+$/).optional(),
  call_value_sun: z.string().regex(/^\d+$/).optional(),
  input_amount: Amount.optional(),
  id: z.string(),
  title: z.string(),
  action: z.string(),
  amount: Amount,
  recipient: z.string(),
  depends_on: z.array(z.string()),
  status: StepStatus,
  txid: z.string().nullable(),
  fee_cap: Amount,
  allowance_remaining: Amount.nullable(),
  error: z.string().nullable(),
});
export const Graph = z.object({
  review_kind: z.enum(["ALLOCATION", "ADJUSTMENT", "USDD_WORKFLOW", "NATIVE_STAKE"]).optional(),
  review_id: z.string().optional(),
  snapshot_root: z.string().optional(),
  minimum_received: Amount.nullable().optional(),
  id: z.string(),
  hash: z.string(),
  plan_id: z.string(),
  plan_hash: z.string(),
  mandate_hash: z.string(),
  network: Network,
  account: z.string(),
  expires_at: z.string(),
  enforcement_scope: z.enum(["DIRECT_PROTOCOL", "EXECUTION_GUARD"]),
  disclosure: z.string().optional(),
  estimated_fee: Amount.optional(),
  expected_fee: Amount.optional(),
  minimum_shares: Amount.optional(),
  steps: z.array(Step).min(1),
});
export type Graph = z.infer<typeof Graph>;
export const Approval = z.object({
  id: z.string(),
  graph_hash: z.string(),
  plan_hash: z.string(),
  mandate_hash: z.string(),
  account: z.string(),
  network: Network,
  expires_at: z.string(),
  status: z.enum(["APPROVED", "EXPIRED", "REVOKED"]),
  consented_at: z.string(),
});
export type Approval = z.infer<typeof Approval>;
export const Check = z.object({
  name: z.string(),
  status: z.enum(["PASS", "FAIL", "UNKNOWN"]),
  detail: z.string(),
});
export const Transaction = z
  .object({
    txID: z.string().regex(/^[a-fA-F0-9]{64}$/),
    raw_data_hex: z.string().regex(/^[a-fA-F0-9]+$/),
    raw_data: z.record(z.string(), z.unknown()),
    signature: z.array(z.string()).optional(),
    visible: z.boolean().optional(),
  })
  .passthrough();
export type Transaction = z.infer<typeof Transaction>;
export const Funding = z
  .object({
    network: z.literal("nile"),
    account: z.string(),
    txid: z.string(),
    status: z.enum([
      "PREPARED",
      "SUBMITTED",
      "SUBMISSION_UNKNOWN",
      "CONFIRMED",
      "FAILED",
    ]),
    transaction: Transaction,
    amount_trn: z.string(),
    estimated_trx: z.string(),
    minimum_trx: z.string(),
    exchange_id: z.number(),
    max_fee_trx: z.string(),
    free_bandwidth: z.number(),
    bandwidth_bound: z.number(),
    expires_at: z.string(),
    message: z.string(),
  })
  .passthrough();
export const FundingState = z.object({
  funding: Funding.nullable(),
  support: z
    .object({
      available: z.boolean(),
      reason: z.string().nullable(),
      checked_at: z.string(),
    })
    .optional(),
});
export const Prepared = z.object({
  graph_hash: z.string(),
  approval_id: z.string(),
  step_id: z.string(),
  network: Network,
  account: z.string(),
  expires_at: z.string(),
  checks: z.array(Check),
  transaction: Transaction.nullable(),
  simulation: z.boolean(),
});
export type Prepared = z.infer<typeof Prepared>;
export const Execution = z.object({
  id: z.string(),
  graph_id: z.string(),
  status: z.enum([
    "SIGNED",
    "SUBMITTED",
    "SUBMISSION_UNKNOWN",
    "CONFIRMED",
    "POSITION_RECONCILED",
    "PERFORMANCE_TRACKED",
    "FAILED",
    "PARTIALLY_COMPLETED",
    "DISPUTED",
    "WAITING_EXIT",
  ]),
  network: Network,
  started_at: z.string(),
  updated_at: z.string(),
  message: z.string(),
});
export const Position = z.object({
  id: z.string(),
  product: z.string(),
  protocol: z.string(),
  network: Network,
  provenance: z.enum(["LIVE", "SIMULATION", "REPLAY"]),
  principal: Amount.nullable(),
  observed_at: z.string().optional(),
  current_value: Amount.nullable(),
  debt: Amount,
  exit_status: z.string(),
  receipt_txid: z.string().nullable(),
});
export const Performance = z.object({
  supplied_capital: Amount.optional(),
  net_return_pct: Decimal.nullable().optional(),
  expected_return_pct: Decimal.nullable().optional(),
  status: z.enum(["RECONCILED", "INCOMPLETE"]).optional(),
  period_start: z.string().optional(),
  period_id: z.string().optional(),
  period_empty: z.boolean().optional(),
  expected_to_date: Amount.nullable().optional(),
  net_income: Amount.nullable().optional(),
  variance: Amount.nullable().optional(),
  open_cost_basis: Amount.nullable().optional(),
  withdrawn: Amount.optional(),
  txids: z.array(z.string()).optional(),
  basis: z.array(z.string()).optional(),
  ledger_hash: z.string().optional(),
  network: Network,
  provenance: z.enum(["LIVE", "SIMULATION", "REPLAY"]),
  as_of: z.string(),
  expected_return: Amount.nullable(),
  accrued: Amount.nullable(),
  realized: Amount.nullable(),
  rewards: Amount.nullable(),
  price_pnl: Amount.nullable(),
  debt_cost: Amount.nullable(),
  fees: Amount.nullable(),
  net_deposits: Amount.nullable(),
});
export const Routine = z.object({
  id: z.string(),
  name: z.string(),
  enabled: z.boolean(),
  time: z.string().regex(/^\d{2}:\d{2}$/),
  timezone: z.string(),
  notify_on: z.array(z.string()),
  last_success_at: z.string().nullable(),
  valid_until: z.string().nullable(),
  worker_status: z.enum(["CONNECTED", "OFFLINE", "STALE"]),
  next_due_at: z.string().optional(),
  last_checked_at: z.string().optional(),
  last_result: z.string().optional(),
  last_error: z.string().nullable().optional(),
});
export const EvidenceRun = z.object({
  calculation: z.record(z.string(), z.unknown()).optional(),
  inputs: z.object({
    mandate: Mandate,
    comparison: Comparison.nullable(),
    graph: Graph.nullable(),
    approval: Approval.nullable(),
    performance: Performance.nullable(),
  }),
  id: z.string(),
  network: Network,
  provenance: z.enum(["LIVE", "SIMULATION", "REPLAY"]),
  mandate_hash: z.string(),
  snapshot_root: z.string(),
  plan_hash: z.string().nullable(),
  graph_hash: z.string().nullable(),
  approval_id: z.string().nullable(),
  txids: z.array(z.string()),
  outcome: z.string(),
  model_id: z.string().nullable(),
  model_approval_evidence: z.string().nullable(),
  flows: z.array(
    z.object({
      name: z.string(),
      llm_calls: z.number().nullable(),
      input_tokens: z.number().nullable(),
      output_tokens: z.number().nullable(),
      latency_ms: z.number().nullable(),
    }),
  ),
  energy: z.object({
    status: z.enum(["MEASURED", "ESTIMATED", "UNAVAILABLE"]),
    wh: z.number().nullable(),
    basis: z.string().nullable(),
  }),
});
export const Intent = z.object({
  status: z.string(),
  patch: z.record(z.string(), z.unknown()),
  questions: z.array(z.object({ field: z.string(), question: z.string() })),
  reason_codes: z.array(z.string()),
  source_text: z.string(),
  created_at: z.string(),
  request_hash: z.string().nullable().optional(),
  known: z.record(z.string(), z.unknown()).optional(),
  agent_id: z.string().nullable().optional(),
  trigger_message_id: z.string().nullable().optional(),
});
export const ReviewOption = z.object({
  hash: z.string().optional(),
  action: z.enum(["HOLD", "SUPPLY", "REDEEM"]),
  position: Decimal,
  wallet_cash: Decimal,
  gross_income: Decimal,
  change_cost: Decimal.nullable(),
  exit_reserve: Decimal,
  future_exit_estimate: Decimal.optional(),
  exit_cost_saving: Decimal.optional(),
  net_improvement: Decimal.nullable().optional(),
  net_income: Decimal.nullable(),
  eligible: z.boolean(),
  reasons: z.array(z.string()),
  delta: Decimal.optional(),
  benefit_over_hold: Decimal.optional(),
  additional_cost: Decimal.nullable().optional(),
});
export const PortfolioReview = z.object({
  product_label:z.string().optional(),
  denomination: z.enum(["TRX", "USDD"]).optional(),
  id: z.string(),
  network: Network,
  policy_hash: z.string().nullable(),
  observed_at: z.string(),
  expires_at: z.string(),
  source: z.string(),
  status: z.enum([
    "HOLD",
    "ADJUST",
    "POLICY_BREACH",
    "POLICY_INACTIVE",
    "DATA_UNAVAILABLE",
    "PENDING_EXECUTION",
  ]),
  reason: z.string(),
  checks: z.array(z.string()),
  hold: ReviewOption.nullable(),
  alternatives: z.array(ReviewOption),
  suggested: ReviewOption.nullable(),
  execution_authority: z.literal("NONE"),
  limitations: z.array(z.string()),
  snapshot_hash: z.string().nullable(),
  block: z.string().nullable(),
  remaining_seconds: z.number().optional(),
  capital: Decimal.optional(),
  paid_fees: Decimal.optional(),
  remaining_budget: Decimal.optional(),
  cash_floor: Decimal.optional(),
  rate: z
    .object({
      annual_fraction: Decimal,
      convention: z.string(),
      day_count: z.string(),
    })
    .optional(),
});
export const AccountNotification = z.object({
  id: z.string(),
  kind: z.string(),
  title: z.string(),
  detail: z.string(),
  review_id: z.string(),
  created_at: z.string(),
  updated_at: z.string(),
  read_at: z.string().nullable(),
  resolved_at: z.string().nullable(),
});
export const UsddReview = z.object({
  id: z.string(), hash: z.string(), network: Network,
  observed_at: z.string(), expires_at: z.string(), status: z.literal("BLOCKED"),
  policy_hash: z.string().nullable(), blockers: z.array(z.string()), reason: z.string(),
  execution_authority: z.literal("NONE"), evidence_hash: z.string(), basis: z.string(),
  source_urls: z.array(z.string()),
  block_range: z.object({ from: z.number(), to: z.number() }).nullable(),
  facts: z.object({
    energy_sun: z.string().nullable(), bandwidth_sun: z.string().nullable(),
    vault_token: z.string().nullable(), destination_token: z.string().nullable(), token_match: z.boolean().nullable(),
    supply_apy: z.string().nullable(), borrow_apy: z.string().nullable(), loop_spread: z.string().nullable(),
    market_cash_usdd: z.string().nullable(), collateral_factor: z.string().nullable(),
    collaterals: z.array(z.object({
      ilk: z.string(), minimum_debt_usdd: z.string(), liquidation_ratio: z.string(),
      debt_capacity_per_trx: z.string(), minimum_trx_at_liquidation: z.string(),
      stability_apy: z.string(), debt_ceiling_remaining_usdd: z.string(),
      collateral_capacity_upper_bound_usdd: z.string().nullable(), policy_meets_minimum: z.boolean().nullable(),
    })),
  }),
});
export const UsddWorkflow = z.object({
  id: z.string(), hash: z.string(), account: z.string(), network: Network, mode: z.enum(["OWNED", "VAULT"]), phase: z.string().optional(),
  prepared: z.object({ txid: z.string(), graph_id: z.string(), step_id: z.string(), expires_at: z.string() }).nullable().optional(),
  rewards: z.array(z.object({ key: z.string(), round: z.string(), amount: z.string() })).optional(),
  status: z.string(), cursor: z.number().int(), confirmed_txids: z.array(z.string()),
  outcome: z.record(z.string(), z.string()), spent_fees: z.string(), total_fee_cap: z.string(),
  steps: z.array(z.object({ id: z.string(), operation: z.string(), amount: z.string(), status: z.string(), txid: z.string().nullable(), graph_id: z.string().nullable().optional() })),
});
export const StakeWorkflow = z.object({
  id: z.string(), status: z.string(), cursor: z.number(), amount: Amount,
  plan_hash: z.string().optional(), mandate_hash: z.string().optional(),
  account: z.string().nullable().optional(), network: Network.nullable().optional(),
  representative: z.string(), steps: z.array(z.object({
    action:z.string(),status:z.string(),
    graph_id:z.string().nullable().optional(),graph_hash:z.string().nullable().optional(),
    step_id:z.string().nullable().optional(),expires_at:z.string().nullable().optional(),
  })),
  forecast: z.object({gross:Decimal, net:Decimal, fees:Decimal, horizon_seconds:z.number()}),
});
export const StakePosition = z.object({
  status:z.string(), amount:Amount, representative:z.string(), as_of:z.string(), forecast_net:Amount, forecast_to_date:Amount.optional(), net_return_pct:Decimal.nullable().optional(),
  fees:Amount, realized:Amount.nullable(), accrued:Amount.nullable(), net_income:Amount.nullable(), basis:z.string(), unfreeze_at:z.number().nullable().optional(),
});
export const ProductCatalog = z.array(z.object({id:z.string(),name:z.string(),status:z.string(),reason:z.string()}));
export const Workspace = z.object({
  stake_transaction_receipts: z.array(z.object({txid:z.string(),graph_id:z.string(),step_id:z.string(),status:z.string()})).optional(),
  stake_workflow: StakeWorkflow.nullable().optional(),
  stake_position: StakePosition.nullable().optional(),
  product_catalog: ProductCatalog.optional(),
  prepared_transactions: z.array(z.object({
    txid: z.string(), graph_id: z.string(), step_id: z.string(), approval_id: z.string(),
    account: z.string(), network: Network, phase: z.literal("PREPARED"),
    expires_at: z.string(), recover_after: z.string(),
  })).optional(),
  transaction_resolutions: z.array(z.object({
    txid: z.string(), graph_id: z.string(), step_id: z.string(),
    status: z.literal("EXPIRED_NOT_OBSERVED"), message: z.string(),
  })).optional(),
  active_mandate: Mandate.nullable().optional(),
  withdrawal_policy: Mandate.nullable().optional(),
  usdd_workflow: UsddWorkflow.nullable().optional(),
  usdd_review: UsddReview.nullable().optional(),
  notifications: z.array(AccountNotification).optional(),
  portfolio_review: PortfolioReview.nullable().optional(),
  intent: Intent.nullable().optional(),
  planning_assumptions: z.record(z.string(), z.unknown()).nullable().optional(),
  revision: z.number().int(),
  network: Network,
  mode: z.enum(["LIVE", "SIMULATION"]),
  messages: z.array(Message),
  mandate: Mandate.nullable(),
  comparison: Comparison.nullable(),
  graph: Graph.nullable(),
  approval: Approval.nullable(),
  execution: Execution.nullable(),
  positions: z.array(Position),
  performance: Performance.nullable(),
  balances: z.array(Amount),
  snapshots: z.array(Snapshot),
  routines: z.array(Routine),
  evidence: z.array(EvidenceRun),
  activity: z.array(
    z.object({
      id: z.string(),
      role: Role,
      title: z.string(),
      detail: z.string(),
      at: z.string(),
      severity: z.enum(["INFO", "WARNING", "ERROR"]),
    }),
  ),
  jobs: z.array(
    z.object({
      id: z.string(),
      status: z.enum(["QUEUED", "RUNNING", "SUCCEEDED", "FAILED"]),
      label: z.string(),
      error: z.string().nullable(),
    }),
  ),
});
export type Workspace = z.infer<typeof Workspace>;
export const Ack = z.object({
  accepted: z.literal(true),
  job_id: z.string().nullable(),
});
export const Reconciliation = Ack.extend({
  resolution: z
    .object({
      txid: z.string().regex(/^[0-9a-f]{64}$/),
      graph_id: z.string(),
      step_id: z.string(),
      status: z.literal("EXPIRED_NOT_OBSERVED"),
      message: z.string(),
    })
    .optional(),
});
export const Challenge = z.object({
  id: z.string(),
  message: z.string(),
  domain: z.string(),
  address: z.string(),
  network: Network,
  expires_at: z.string(),
});
export const ErrorBody = z.object({
  error: z.object({
    code: z.string(),
    message: z.string(),
    request_id: z.string().optional(),
  }),
});
export const MessageInput = z.object({
  agent_id: z.string(),
  role: Role,
  text: z.string().trim().min(1).max(4000),
  network: Network,
});
export const MandateInput = z.object({
  agent_id: z.string().optional(),
  assumptions: z.record(z.string(), z.unknown()),
  terms: z.record(z.string(), z.unknown()),
  source_text: z.string(),
  network: Network,
  constraints: Constraints,
  previous_id: z.string().nullable(),
});
export const ConsentInput = z.object({
  graph_id: z.string(),
  graph_hash: z.string(),
  mandate_hash: z.string(),
  plan_hash: z.string(),
  account: z.string(),
  network: Network,
});
export const SubmitInput = z.object({
  approval_id: z.string(),
  graph_id: z.string(),
  step_id: z.string(),
  network: Network,
  signed_transaction: Transaction,
});
export const RoutineInput = z.object({
  enabled: z.boolean(),
  time: z.string().regex(/^([01]\d|2[0-3]):[0-5]\d$/),
  timezone: z.string().min(1),
  notify_on: z.array(z.string()),
});
// This manifest is the executable source of the generated OpenAPI document.
export const ServiceStatus = z.object({
  kiln_configured: z.boolean(),
  finance_connected: z.boolean(),
  model: z.string(),
});
export const Usage = z.array(
  z.object({
    id: z.string(),
    agent: z.string(),
    network: Network,
    status: z.string(),
    model: z.string().nullable(),
    input_tokens: z.number().nullable(),
    output_tokens: z.number().nullable(),
    latency_ms: z.number().nullable(),
    created: z.string(),
    error: z.string().nullable(),
  }),
);
export const endpoints = [
  {
    method: "post",
    path: "/v1/performance/periods",
    request: z.object({ network: Network, confirm_new_period: z.literal(true) }),
    response: z.object({ accepted: z.literal(true), job_id: z.null(), period_id: z.string(), started_at: z.string() }),
  },
  {
    method: "post",
    path: "/v1/usdd-workflows",
    request: z.object({ network: Network, mode: z.enum(["OWNED", "VAULT"]), amount_usdd: z.string(), collateral_trx: z.string().optional(), ilk: z.enum(["TRX-A", "TRX-B", "TRX-C"]).optional(), loops: z.number().int().min(0).max(4), borrow_bps: z.number().int().min(0).max(8500), per_step_fee_cap_trx: z.string(), total_fee_cap_trx: z.string() }),
    response: Ack,
  },
  { method: "post", path: "/v1/usdd-workflows/next", request: z.object({ network: Network }), response: Ack },
  { method: "post", path: "/v1/usdd-workflows/recover", request: z.object({ network: Network, claim_key: z.string().optional() }), response: Ack },
  { method: "post", path: "/v1/usdd-workflows/cancel", request: z.object({ network: Network }), response: Ack },
  { method: "post", path: "/v1/usdd-workflows/rewards", request: z.object({ network: Network }), response: Ack },
  {
    method: "post",
    path: "/v1/usdd-reviews",
    request: z.object({ network: Network }),
    response: Ack,
  },
  {
    method: "post",
    path: "/v1/portfolio-reviews",
    request: z.object({ network: Network, agent_id: z.string().optional() }),
    response: Ack,
  },
  {
    method: "post",
    path: "/v1/portfolio-adjustments",
    request: z.object({ network: Network, review_id: z.string(), option_hash: z.string().regex(/^[a-f0-9]{64}$/), agent_id: z.string().optional() }),
    response: Ack,
  },
  {
    method: "post",
    path: "/v1/notifications/{id}/read",
    request: z.object({ network: Network }),
    response: Ack,
  },
  {
    method: "post",
    path: "/v1/agents/{id}/restore",
    request: z.object({}),
    response: Ack,
  },
  { method: "get", path: "/v1/status", response: ServiceStatus },
  { method: "get", path: "/v1/usage", response: Usage },
  {
    method: "post",
    path: "/v1/jobs/{id}/cancel",
    request: z.object({}),
    response: Ack,
  },
  {
    method: "post",
    path: "/v1/routines",
    request: RoutineInput.extend({ name: z.string(), network: Network }),
    response: Ack,
  },
  { method: "get", path: "/v1/agents", response: Agents },
  { method: "post", path: "/v1/agents", request: AgentInput, response: Agent },
  {
    method: "patch",
    path: "/v1/agents/{id}",
    request: AgentInput,
    response: Agent,
  },
  {
    method: "delete",
    path: "/v1/agents/{id}",
    request: z.object({}),
    response: Ack,
  },

  { method: "get", path: "/v1/session", response: Session },
  {
    method: "post",
    path: "/v1/auth/challenge",
    request: z.object({
      address: z.string(),
      network: Network,
      domain: z.string(),
    }),
    response: Challenge,
  },
  {
    method: "post",
    path: "/v1/auth/verify",
    request: z.object({ challenge_id: z.string(), signature: z.string() }),
    response: Session,
  },
  {
    method: "post",
    path: "/v1/auth/logout",
    request: z.object({}),
    response: Ack,
  },
  { method: "get", path: "/v1/workspace", response: Workspace },
  {
    method: "post",
    path: "/v1/messages",
    request: MessageInput,
    response: Ack,
  },
  {
    method: "post",
    path: "/v1/mandates",
    request: MandateInput,
    response: Ack,
  },
  {
    method: "post",
    path: "/v1/mandates/{id}/confirm",
    request: z.object({
      hash: z.string(),
      version: z.number(),
      network: Network,
      agent_id: z.string().optional(),
    }),
    response: Ack,
  },
  {
    method: "post",
    path: "/v1/plan-comparisons",
    request: z.object({
      mandate_id: z.string(),
      mandate_hash: z.string(),
      network: Network,
      snapshot_root: z.string().optional(),
      agent_id: z.string().optional(),
    }),
    response: Ack,
  },
  {
    method: "post",
    path: "/v1/execution-graphs",
    request: z.object({
      plan_id: z.string(),
      plan_hash: z.string(),
      mandate_hash: z.string(),
      network: Network,
      agent_id: z.string().optional(),
    }),
    response: Ack,
  },
  {
    method: "post",
    path: "/v1/approvals",
    request: ConsentInput,
    response: Ack,
  },
  {
    method: "post",
    path: "/v1/execution-graphs/{id}/preflight",
    request: z.object({
      approval_id: z.string(),
      step_id: z.string(),
      network: Network,
      account: z.string(),
    }),
    response: Prepared,
  },
  {
    method: "post",
    path: "/v1/executions",
    request: SubmitInput,
    response: Ack,
  },
  {
    method: "post",
    path: "/v1/executions/reconcile",
    request: z.object({
      graph_id: z.string(),
      step_id: z.string(),
      txid: z.string(),
      network: Network,
    }),
    response: Reconciliation,
  },
  {
    method: "patch",
    path: "/v1/routines/{id}",
    request: RoutineInput.extend({ network: Network }),
    response: Ack,
  },
  { method: "get", path: "/v1/evidence", response: z.array(EvidenceRun) },
  ...(["next", "cancel", "refresh"] as const).map((action) => ({
    method: "post" as const,
    path: `/v1/stake-workflows/${action}`,
    request: z.object({ network: z.literal("nile"), agent_id: z.string().optional() }),
    response: Ack,
  })),
  {
    method: "post",
    path: "/v1/stake-workflows/lifecycle",
    request: z.object({ network: z.literal("nile"), action: z.enum(["UNSTAKE", "WITHDRAW", "CLAIM"]), agent_id: z.string().optional() }),
    response: Ack,
  },
  { method: "get", path: "/v1/funding/state", response: FundingState },
  {
    method: "post",
    path: "/v1/funding/quote",
    request: z.object({ network: z.literal("nile"), amount: z.string() }),
    response: Funding,
  },
  {
    method: "post",
    path: "/v1/funding/submit",
    request: z.object({
      network: z.literal("nile"),
      signed_transaction: Transaction,
    }),
    response: Funding,
  },
  {
    method: "post",
    path: "/v1/funding/reconcile",
    request: z.object({ network: z.literal("nile") }),
    response: FundingState,
  },
] as const;
