import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { MandateEditor } from "../src/components/MandateEditor";
import type { Machine } from "../src/api/useMachine";

function render(caps?: { prices: Record<string, number>; protocols: Record<string, number> }) {
  const mandate = caps ? {
    constraints: { capital: {symbol:"TRX",value:"300"}, horizon_days:365, min_cash_bps:2000, max_trx_exposure_bps:10000, max_fee:{value:"15"} },
    terms: { risk_profile:"growth", price_exposure_caps_bps:caps.prices, protocol_caps_bps:caps.protocols,
      borrowing:{consent:false,max_debt:{asset:"TRX",amount:"0"},min_collateral_ratio_bps:15000,liquidation_buffer_bps:2000},
      limits:{}, withdrawals:[] },
  } : null;
  const m = {workspace:{data:{mandate}},network:"nile",busy:null} as unknown as Machine;
  return renderToStaticMarkup(<MandateEditor m={m} onSaved={()=>{}} />);
}
function input(html: string, label: string) {
  return html.match(new RegExp(`<input[^>]*aria-label="${label}"[^>]*>`))?.[0];
}
it("reopens an existing sparse policy with omitted caps denied at zero",()=>{
  const html=render({prices:{TRX:10000},protocols:{justlend:8000}});
  expect(input(html,"Maximum USDD price exposure")).toContain('value="0"');
  expect(input(html,"Maximum USDD protocol allocation")).toContain('value="0"');
  expect(input(render({prices:{},protocols:{}}),"Maximum JustLend allocation")).toContain('value="0"');
  expect(html).not.toContain('NaN');
});
it("preserves explicitly reviewed nonzero caps",()=>{
  const html=render({prices:{USDD:1250},protocols:{justlend:7000,usdd:2500}});
  expect(input(html,"Maximum USDD price exposure")).toContain('value="12.5"');
  expect(input(html,"Maximum JustLend allocation")).toContain('value="70"');
  expect(input(html,"Maximum USDD protocol allocation")).toContain('value="25"');
});
it("keeps new unreviewed policy limits empty rather than silently choosing them",()=>{
  const html=render();
  for(const label of ["Maximum USDD price exposure","Maximum JustLend allocation","Maximum USDD protocol allocation"])
    expect(input(html,label)).toContain('value=""');
});
