import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { SidePanel, panelTitles } from "../src/components/SidePanel";
import type { Machine } from "../src/api/useMachine";

function screen(approved = false, prepared = false) {
  const expires_at = new Date(Date.now() + 120000).toISOString();
  const graph = {id:"native-test",hash:"graph",review_kind:"ADJUSTMENT",mandate_hash:"active",account:"T-test-wallet",network:"nile",expires_at,
    expected_fee:{value:"7.25",symbol:"TRX",decimals:6},minimum_received:{value:"0.99",symbol:"TRX",decimals:6},
    steps:[{id:"step",action:"redeem(uint256)",status:"READY",amount:{value:"1",symbol:"TRX",decimals:6},
      fee_cap:{value:"15",symbol:"TRX",decimals:6},recipient:"market",input_amount:{value:"89.46435527",symbol:"jTRX",decimals:8},txid:null}]};
  const m={clock:Date.now(),walletValid:true,busy:null,pending:prepared?{txid:"tx",graph_id:"native-test",step_id:"step",phase:"PREPARED"}:null,error:null,session:{data:{authenticated:true}},
    workspace:{data:{graph,prepared_transactions:prepared?[{txid:"tx",graph_id:"native-test",expires_at,recover_after:expires_at}]:[],mandate:{status:"DRAFT",hash:"pending"},active_mandate:{status:"CONFIRMED",hash:"active"},
      approval:approved?{status:"APPROVED",graph_hash:"graph",expires_at}:null,execution:null}}} as unknown as Machine;
  return renderToStaticMarkup(<MemoryRouter><SidePanel panel="execution" m={m} onClose={()=>{}} theme="light" setTheme={()=>{}} /></MemoryRouter>);
}
it("execution panel actually renders a visible withdrawal approval despite pending draft edits",()=>{
  expect(panelTitles.execution).toBe("Review transaction");
  const html=screen();
  expect(html).toContain("Approve withdrawal");
  expect(html).not.toContain("This quote expired");
  expect(html).toContain("7.25 TRX");expect(html).toContain("15 TRX");
  expect(html).toContain("89.46435527");
});
it("approved withdrawal goes directly to wallet signing without a manual preflight button",()=>{
  const html=screen(true);
  expect(html).toContain("Sign in TronLink");
  expect(html).not.toContain("Run preflight");
});

it("a cancelled wallet prompt can resume the same pending transaction instead of creating another review",()=>{
 const html=screen(true,true);expect(html).toContain("Continue in TronLink");expect(html).not.toContain("Approve withdrawal");
});
