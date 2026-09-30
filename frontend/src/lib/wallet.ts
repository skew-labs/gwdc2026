import {
  type Network,
  type Graph,
  type Prepared,
  type Transaction,
  Transaction as TransactionSchema,
} from "../api/contracts";
import { expired, toUnits } from "./format";
import { errorMessage } from "./errors";
export interface TronWebProvider {
  ready?: boolean;
  defaultAddress: { base58: string | false; hex?: string };
  fullNode?: { host: string };
  address: { toHex: (address: string) => string };
  trx: {
    sign: (tx: Transaction) => Promise<Transaction>;
    signMessageV2: (message: string) => Promise<string>;
  };
}
interface Provider {
  isTronLink?: boolean;
  tronWeb?: TronWebProvider;
  request: (args: { method: string; params?: unknown }) => Promise<unknown>;
  on?: (event: string, fn: (value: unknown) => void) => void;
  removeListener?: (event: string, fn: (value: unknown) => void) => void;
}
declare global {
  interface Window {
    tron?: Provider;
    tronLink?: Provider;
    tronWeb?: TronWebProvider;
  }
}
const chains: Record<string, Network> = {
  "0xcd8690dc": "nile",
  "0x2b6653dc": "mainnet",
};
let discovered: Provider | undefined;
if (typeof window !== "undefined") {
  window.addEventListener("TIP6963:announceProvider", (e) => {
    const d = (e as CustomEvent).detail;
    if (d?.info?.rdns === "org.tronlink.www" && d?.info?.name === "TronLink")
      discovered = d.provider;
  });
  window.dispatchEvent(new Event("TIP6963:requestProvider"));
}
function provider() {
  const p = discovered || window.tron || window.tronLink;
  if (!p) throw new Error("Install or unlock TronLink, then connect again.");
  return p;
}
function web() {
  const p = provider();
  const w = p.tronWeb || window.tronWeb;
  if (!w) throw new Error("TronLink is locked or unavailable.");
  return w;
}
export async function identity() {
  const p = provider();
  const w = web();
  const address = w.defaultAddress.base58;
  if (!address) throw new Error("Unlock TronLink and select an account.");
  let network: Network | undefined;
  try {
    const chain = await p.request({ method: "eth_chainId" });
    network = chains[String(chain)];
  } catch {
    /* Older TronLink exposes its node instead of eth_chainId. */
  }
  if (!network) {
    let host = "";
    try {
      host = new URL(w.fullNode?.host || "").hostname;
    } catch {
      /* Unknown custom nodes are rejected. */
    }
    network =
      host === "nile.trongrid.io"
        ? "nile"
        : host === "api.trongrid.io"
          ? "mainnet"
          : undefined;
  }
  if (!network)
    throw new Error(
      "This wallet network is not recognized. Select Nile or TRON Mainnet in TronLink.",
    );
  return { address, network };
}
export async function connectWallet() {
  const p = provider();
  if (p === window.tronLink && !window.tron && !discovered) {
    const r = (await p.request({ method: "tron_requestAccounts" })) as {
      code?: number;
    } | null;
    if (r?.code !== 200) throw new Error("Wallet connection was not approved.");
  } else await p.request({ method: "eth_requestAccounts" });
  return identity();
}
export async function signOwnership(message: string) {
  return web().trx.signMessageV2(message);
}
export function watchWallet(onChange: () => void) {
  let p: Provider;
  try {
    p = provider();
  } catch {
    return () => {};
  }
  const update = () => onChange();
  for (const e of ["accountsChanged", "chainChanged", "disconnect"])
    p.on?.(e, update);
  const legacy = (e: MessageEvent) => {
    if (e.source !== window || e.origin !== window.location.origin) return;
    if (
      ["setAccount", "setNode", "disconnectWeb"].includes(
        e.data?.message?.action,
      )
    )
      onChange();
  };
  window.addEventListener("message", legacy);
  return () => {
    for (const e of ["accountsChanged", "chainChanged", "disconnect"])
      p.removeListener?.(e, update);
    window.removeEventListener("message", legacy);
  };
}
function canonical(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value && typeof value === "object")
    return (
      "{" +
      Object.keys(value)
        .sort()
        .map(
          (k) =>
            JSON.stringify(k) +
            ":" +
            canonical((value as Record<string, unknown>)[k]),
        )
        .join(",") +
      "}"
    );
  return JSON.stringify(value);
}
export async function assertTransaction(
  tx: Transaction,
  account: string,
  toHex: (a: string) => string,
) {
  if (tx.signature?.length)
    throw new Error("The service must provide an unsigned transaction.");
  const bytes = new Uint8Array(
    tx.raw_data_hex.match(/.{1,2}/g)!.map((x) => parseInt(x, 16)),
  );
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const hash = Array.from(new Uint8Array(digest))
    .map((x) => x.toString(16).padStart(2, "0"))
    .join("");
  if (hash !== tx.txID.toLowerCase())
    throw new Error("Transaction hash does not match its payload.");
  await assertWalletEncoding(tx);
  const expiration = Number(tx.raw_data.expiration);
  if (!Number.isSafeInteger(expiration) || expiration <= Date.now() + 5000)
    throw new Error("Transaction expired. Run preflight again.");
  const contracts = tx.raw_data.contract as
    { parameter?: { value?: { owner_address?: string } } }[] | undefined;
  if (
    !contracts?.length ||
    contracts.some((c) => {
      const owner = c.parameter?.value?.owner_address;
      return (
        !owner || toHex(owner).toLowerCase() !== toHex(account).toLowerCase()
      );
    })
  )
    throw new Error("Transaction owner does not match the verified wallet.");
}
export async function assertWalletEncoding(tx: Transaction) {
  // Use the real wallet serializer, not only a hash of server-provided bytes.
  const { utils } = await import("tronweb");
  let compatible = false;
  try { compatible = utils.transaction.txCheck(tx as Parameters<typeof utils.transaction.txCheck>[0]); } catch { /* Invalid protobuf/JSON is rejected below. */ }
  if (!compatible)
    throw new Error("The service prepared a transaction that TronLink cannot verify. Nothing was sent. Refresh the withdrawal review to get a corrected transaction.");
}
export function assertSignedUnchanged(
  original: Transaction,
  signed: Transaction,
) {
  if (
    signed.txID !== original.txID ||
    signed.raw_data_hex !== original.raw_data_hex ||
    canonical(signed.raw_data) !== canonical(original.raw_data) ||
    !signed.signature?.length
  )
    throw new Error(
      "The wallet returned a different or unsigned transaction. Nothing was submitted.",
    );
}
export function normalizeWalletSignature(tx: Transaction): Transaction {
  const signatures = tx.signature;
  if (
    signatures?.length !== 1 ||
    !/^(?:0x)?[0-9a-f]{130}$/i.test(signatures[0])
  )
    throw new Error(
      "The wallet must return one 65-byte transaction signature. Nothing was submitted.",
    );
  // TronWeb emits 1B/1C for the recovery byte. Normalize notation, never bytes.
  return {
    ...tx,
    signature: [signatures[0].replace(/^0x/i, "").toLowerCase()],
  };
}
export function assertNativeScope(
  tx: Transaction,
  step: Graph["steps"][number],
  toHex: (address: string) => string,
) {
  if (!step.call_data && !["mint() · payable TRX", "mint()"].includes(step.action)) return;
  const contracts = tx.raw_data.contract as {
    type: string;
    Permission_id?: number;
    parameter: { type_url: string; value: Record<string, unknown> };
  }[];
  const c = contracts?.[0],
    v = c?.parameter?.value;
  if (
    contracts?.length !== 1 ||
    c.type !== "TriggerSmartContract" ||
    (c.Permission_id || 0) !== 0 ||
    c.parameter.type_url !==
      "type.googleapis.com/protocol.TriggerSmartContract" ||
    !v ||
    typeof v.contract_address !== "string" ||
    toHex(v.contract_address).toLowerCase() !==
      toHex(step.recipient).toLowerCase() ||
    (!step.call_data && step.amount.symbol !== "TRX") ||
    step.fee_cap.symbol !== "TRX" ||
    v.data !== (step.call_data || "1249c58b") ||
    BigInt(String(v.call_value || 0)) !== (step.call_data ? BigInt(step.call_value_sun ?? "-1") : toUnits(step.amount.value, 6)) ||
    BigInt(String(tx.raw_data.fee_limit)) > toUnits(step.fee_cap.value, 6) ||
    BigInt(String(v.call_token_value || 0)) !== 0n ||
    BigInt(String(v.token_id || 0)) !== 0n
  )
    throw new Error(
      "Native transaction differs from the reviewed amount, recipient, call or fee cap.",
    );
}
export function assertStakeScope(tx:Transaction,step:Graph["steps"][number],toHex:(address:string)=>string) {
  const kinds=["FreezeBalanceV2Contract","VoteWitnessContract","UnfreezeBalanceV2Contract","WithdrawExpireUnfreezeContract","WithdrawBalanceContract"];
  if (!kinds.includes(step.action)) return;
  const contracts=tx.raw_data.contract as {type:string;Permission_id?:number;parameter:{type_url:string;value:Record<string,unknown>}}[];
  const c=contracts?.[0],v=c?.parameter?.value;
  if (contracts?.length!==1 || c.type!==step.action || (c.Permission_id || 0)!==0 || c.parameter.type_url!==`type.googleapis.com/protocol.${step.action}` || !v || Number(tx.raw_data.fee_limit || 0)!==0) throw new Error("Native transaction type or permission differs from this review.");
  const allowed=step.action==="VoteWitnessContract" ? ["owner_address","votes"] : step.action==="FreezeBalanceV2Contract" ? ["owner_address","frozen_balance","resource"] : step.action==="UnfreezeBalanceV2Contract" ? ["owner_address","unfreeze_balance","resource"] : ["owner_address"];
  if (Object.keys(v).some(key=>!allowed.includes(key))) throw new Error("Unexpected native transaction field.");
  if (step.action==="FreezeBalanceV2Contract" || step.action==="UnfreezeBalanceV2Contract") {
    if (v.resource!=="ENERGY" || step.amount.symbol!=="TRX" || BigInt(String(v[step.action==="FreezeBalanceV2Contract" ? "frozen_balance" : "unfreeze_balance"]))!==toUnits(step.amount.value,6)) throw new Error("Staking amount or resource changed.");
  }
  if (step.action==="VoteWitnessContract") {
    const normalize=(votes:{vote_address:string;vote_count:number}[])=>votes.map(v=>({vote_address:toHex(v.vote_address).toLowerCase(),vote_count:v.vote_count})).sort((a,b)=>a.vote_address.localeCompare(b.vote_address));
    if (!Array.isArray(v.votes) || !step.native_votes?.length || canonical(normalize(v.votes as {vote_address:string;vote_count:number}[]))!==canonical(normalize(step.native_votes))) throw new Error("Representative or vote count differs from this review.");
  }
}
export async function signPrepared(p: Prepared, step: Graph["steps"][number]) {
  if (p.simulation || !p.transaction)
    throw new Error("A live, prepared transaction is required.");
  if (
    expired(p.expires_at) ||
    p.checks.length === 0 ||
    p.checks.some((c) => c.status !== "PASS")
  )
    throw new Error("Preflight must pass before signing.");
  const before = await identity();
  if (before.address !== p.account || before.network !== p.network)
    throw new Error("The wallet account or network changed. Verify it again.");
  await assertTransaction(p.transaction, p.account, web().address.toHex);
  assertNativeScope(p.transaction, step, web().address.toHex);
  assertStakeScope(p.transaction, step, web().address.toHex);
  const original = structuredClone(p.transaction);
  let walletResult: unknown;
  try { walletResult = await web().trx.sign(structuredClone(original)); }
  catch (error) { throw new Error(`TronLink: ${errorMessage(error)}`); }
  const signed = normalizeWalletSignature(TransactionSchema.parse(walletResult));
  assertSignedUnchanged(original, signed);
  const after = await identity();
  if (after.address !== before.address || after.network !== before.network)
    throw new Error("Wallet changed while signing. Nothing was submitted.");
  return signed;
}

/** Native Nile funding trade; the server independently decodes and verifies protobuf. */
export async function signFunding(
  tx: Transaction,
  account: string,
  expected: {
    exchange_id: number;
    amount_trn: string;
    minimum_trx: string;
  },
) {
  const before = await identity();
  if (before.network !== "nile" || before.address !== account)
    throw new Error("Select the verified wallet on Nile in TronLink.");
  await assertTransaction(tx, account, web().address.toHex);
  const contracts = tx.raw_data.contract as {
    type: string;
    parameter: { value: Record<string, unknown> };
  }[];
  const c = contracts[0];
  const units = (s: string) => {
    const [whole, fraction = ""] = s.split(".");
    return BigInt(whole) * 1000000n + BigInt(fraction.padEnd(6, "0"));
  };
  if (
    contracts.length !== 1 ||
    c.type !== "ExchangeTransactionContract" ||
    c.parameter.value.exchange_id !== expected.exchange_id ||
    c.parameter.value.token_id !== "31303035343136" ||
    BigInt(String(c.parameter.value.quant)) !== units(expected.amount_trn) ||
    BigInt(String(c.parameter.value.expected)) !== units(expected.minimum_trx)
  )
    throw new Error(
      "Prepared swap differs from the displayed exchange and amounts.",
    );
  const original = structuredClone(tx);
  const signed = TransactionSchema.parse(
    await web().trx.sign(structuredClone(original)),
  );
  assertSignedUnchanged(original, signed);
  const after = await identity();
  if (after.address !== account || after.network !== "nile")
    throw new Error("Wallet changed during signing. Nothing was submitted.");
  return signed;
}
