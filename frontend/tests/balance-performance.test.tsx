import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { BalancePerformance } from "../src/components/BalancePerformance";
import type { Workspace } from "../src/api/contracts";
const amount=(value:string)=>({value,symbol:"TRX",decimals:6});
const w={network:"nile",performance:{network:"nile",status:"RECONCILED",as_of:"2026-09-30T00:00:00Z",supplied_capital:amount("1"),net_return_pct:"-808.940000",expected_return_pct:"-911.340000",expected_to_date:amount("-9.1134"),net_income:amount("-8.0894"),accrued:amount("0"),realized:amount("0"),fees:amount("8.0894")}} as unknown as Workspace;
it("shows expected and actual net amounts and return without treating wallet cash as invested capital",()=>{
 const html=renderToStaticMarkup(<BalancePerformance workspace={w} symbol="TRX" />);
 expect(html).toContain("-808.94%");expect(html).toContain("-911.34%");
 expect(html).toContain("-8.0894 TRX");expect(html).toContain("Accrued income");expect(html).toContain("Realized income");
 expect(html).toContain("total supplied capital (1 TRX)");
});
it("does not turn another asset or incomplete accounting into a measured return",()=>{
 expect(renderToStaticMarkup(<BalancePerformance workspace={w} symbol="USDD" />)).not.toContain("-808.94%");
 const incomplete=structuredClone(w);incomplete.performance!.status="INCOMPLETE";
 const html=renderToStaticMarkup(<BalancePerformance workspace={incomplete} symbol="TRX" />);
 expect(html).not.toContain("-808.94%");expect(html).toContain("Returns are unavailable");
});
it("shows an explicit zero starting baseline for a new period without inventing a forecast",()=>{
 const fresh=structuredClone(w);
 Object.assign(fresh.performance!,{period_id:"period-one",period_empty:true,supplied_capital:amount("0"),net_return_pct:null,expected_return_pct:null,expected_to_date:null,net_income:amount("0"),fees:amount("0")});
 const html=renderToStaticMarkup(<BalancePerformance workspace={fresh} symbol="TRX" />);
 expect(html).toContain("Starting return");expect(html).toContain(">0%</strong>");
 expect(html).toContain("No investments yet");expect(html).toContain("Not measured");
 expect(html).toContain("Earlier transactions remain in your receipts");expect(html).not.toContain("-808.94%");
});
