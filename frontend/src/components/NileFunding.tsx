import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";
import { Funding, FundingState as State } from "../api/contracts";
import { request } from "../api/client";
import type { Machine } from "../api/useMachine";
import { signFunding } from "../lib/wallet";
import { date, expired, explorer, downloadJson } from "../lib/format";
import { Button, Row, Badge } from "./ui";

export function NileFunding({ m }: { m: Machine }) {
  const [amount, setAmount] = useState("");
  const qc = useQueryClient();
  const account = m.session.data?.wallet_address;
  const key = ["nile-funding", account];
  const query = useQuery({
    queryKey: key,
    queryFn: () => request("GET", "/v1/funding/state?network=nile", State),
    enabled: m.network === "nile" && !!m.session.data?.authenticated,
    retry: false,
    refetchInterval: 30000,
  });
  if (m.network !== "nile") return null;
  const row = query.data?.funding;
  const pending =
    row?.status === "SUBMITTED" || row?.status === "SUBMISSION_UNKNOWN";
  const blocked = !!m.busy || !m.walletValid;
  const save = (funding: z.infer<typeof Funding> | null) =>
    qc.setQueryData(key, { funding, support: query.data?.support });
  const reconcile = () =>
    m.run("Checking solidified transaction", async () => {
      const state = await request("POST", "/v1/funding/reconcile", State, {
        network: "nile",
      });
      save(state.funding);
      if (state.funding?.status === "CONFIRMED") {
        await m.mutate("/v1/observations/refresh", { network: "nile" });
      }
    });
  return (
    <section className="position">
      <h3>Nile transaction funding</h3>
      <p className="muted">
        Check whether Nile supports exchanging faucet TRN (TRC10 #1005416) for
        native test TRX. These test coins have no mainnet value.
      </p>
      {!account && (
        <Button onClick={m.connect} disabled={!!m.busy}>
          Connect TronLink to fund transactions
        </Button>
      )}
      {query.error && <p role="alert">{query.error.message}</p>}
      {query.data?.support?.available === false && (
        <div role="status">
          <p>{query.data.support.reason}</p>
          <p className="muted">
            For native TRX, the official TRON developer bot provides a separate
            testnet faucet. In the official developer group, send:
          </p>
          <code className="mono">!nile {account}</code>
          <p>
            <a
              className="text-link"
              href="https://developers.tron.network/docs/getting-testnet-tokens-on-tron"
              target="_blank"
              rel="noreferrer"
            >
              Official faucet and bot instructions
            </a>
          </p>
        </div>
      )}
      {account && query.isPending && (
        <p role="status">Checking live Nile exchange availability…</p>
      )}
      {!pending && query.data?.support?.available === true && (
        <form
          className="routine-form"
          onSubmit={(e) => {
            e.preventDefault();
            void m.run("Reading Nile exchange liquidity", async () => {
              save(
                await request("POST", "/v1/funding/quote", Funding, {
                  network: "nile",
                  amount,
                }),
              );
            });
          }}
        >
          <label>
            TRN to exchange
            <input
              inputMode="decimal"
              required
              value={amount}
              placeholder="Enter amount, up to 100 TRN"
              onChange={(e) => setAmount(e.target.value)}
            />
          </label>
          <Button type="submit" secondary disabled={blocked}>
            Get live exchange quote
          </Button>
        </form>
      )}
      {row && (
        <>
          <Badge>{row.status.replaceAll("_", " ")}</Badge>
          <Row label="Pay">{row.amount_trn} TRN</Row>
          <Row label="Estimated receipt">{row.estimated_trx} TRX</Row>
          <Row label="Minimum receipt">{row.minimum_trx} TRX</Row>
          <Row label="Native exchange">#{row.exchange_id}</Row>
          <Row label="Estimated network burn">{row.max_fee_trx} TRX</Row>
          <Row label="Free bandwidth / required bound">
            {row.free_bandwidth} / {row.bandwidth_bound}
          </Row>
          <p className="caption">
            1% slippage limit. The fee estimate assumes these wallet resources
            remain available until broadcast. Quote expires{" "}
            {date(row.expires_at)}.
          </p>
          <p role="status">{row.message}</p>
          {row.status === "PREPARED" && (
            <Button
              disabled={
                blocked ||
                query.data?.support?.available !== true ||
                expired(row.expires_at)
              }
              onClick={() => {
                void m.run(
                  "Review and sign the Nile exchange in TronLink",
                  async () => {
                    const signed = await signFunding(
                      row.transaction,
                      row.account,
                      row,
                    );
                    try {
                      save(
                        await request(
                          "POST",
                          "/v1/funding/submit",
                          Funding,
                          { network: "nile", signed_transaction: signed },
                          { key: `nile-funding-${signed.txID}` },
                        ),
                      );
                    } finally {
                      await qc.invalidateQueries({ queryKey: key });
                    }
                  },
                );
              }}
            >
              {expired(row.expires_at)
                ? "Quote expired — refresh above"
                : "Sign exchange in TronLink"}
            </Button>
          )}
          {pending && (
            <Button disabled={!!m.busy} onClick={reconcile}>
              Check confirmation
            </Button>
          )}
          {row.status !== "PREPARED" && (
            <a
              className="text-link"
              href={explorer("nile", row.txid)!}
              target="_blank"
              rel="noreferrer"
            >
              View transaction on Nile
            </a>
          )}
          <Button
            secondary
            onClick={() => downloadJson(row, `nile-funding-${row.txid}.json`)}
          >
            Export transaction evidence
          </Button>
        </>
      )}
    </section>
  );
}
