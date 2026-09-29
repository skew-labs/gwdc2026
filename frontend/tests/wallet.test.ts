import { afterEach, expect, it, vi } from "vitest";
import { TronWeb, utils } from "tronweb";
import { assertWalletEncoding, signPrepared } from "../src/lib/wallet";
import { errorMessage } from "../src/lib/errors";
import type { Graph, Prepared, Transaction } from "../src/api/contracts";
import fixtures from "./fixtures/tronweb-trigger-transactions.json";

// A public, deterministic, unfunded test key. These tests never contact a node.
const TEST_KEY = "31".repeat(32);
const tron = new TronWeb({fullHost:"http://127.0.0.1:1"});
afterEach(()=>{vi.unstubAllGlobals();vi.restoreAllMocks();});
function setup(name:string, reject?:unknown) {
 const f=fixtures.find(f=>f.name===name)!;
 const transaction=structuredClone(f.transaction) as Transaction;
 const address=TronWeb.address.fromPrivateKey(TEST_KEY) as string;
 const sign=vi.fn(async(tx:Transaction)=>{
  if(reject!==undefined) throw reject;
  return tron.trx.sign(tx as unknown as Parameters<typeof tron.trx.sign>[0],TEST_KEY);
 });
 vi.stubGlobal("window",{tronLink:{request:async()=>"0xcd8690dc",tronWeb:{defaultAddress:{base58:address},address:TronWeb.address,trx:{sign}}}});
 vi.spyOn(Date,"now").mockReturnValue(f.request.timestamp_ms+1000);
 const step={id:"step",action:f.name==="redeem"?"redeem(uint256)":"protocol call",call_data:f.request.data_hex,call_value_sun:f.request.call_value_sun,
  amount:{value:"1",symbol:"TRX",decimals:6},fee_cap:{value:"15",symbol:"TRX",decimals:6},recipient:TronWeb.address.fromHex(f.request.contract_address)} as Graph["steps"][number];
 const p={transaction,account:address,network:"nile",simulation:false,expires_at:new Date(f.request.expiration_ms).toISOString(),checks:[{name:"test",status:"PASS",detail:"synthetic"}]} as Prepared;
 return {p,step,sign};
}
it.each(fixtures.map(f=>f.name))("server-generated %s passes real TronWeb validation and the full frontend signing path",async name=>{
 const {p,step,sign}=setup(name);
 expect(utils.transaction.txCheck(p.transaction as Parameters<typeof utils.transaction.txCheck>[0])).toBe(true);
 const signed=await signPrepared(p,step);
 expect(sign).toHaveBeenCalledTimes(1);
 expect(signed.txID).toBe(p.transaction!.txID);
 expect(signed.raw_data_hex).toBe(p.transaction!.raw_data_hex);
 expect(signed.signature?.[0]).toMatch(/^[0-9a-f]{130}$/);
 expect(utils.crypto.ecRecover(signed.txID,signed.signature![0]).toLowerCase()).toBe(TronWeb.address.toHex(p.account).toLowerCase());
});
it("rejects JSON/protobuf mismatch before asking the wallet to sign",async()=>{
 const {p,step,sign}=setup("redeem");
 const contracts=p.transaction!.raw_data.contract as {parameter:{value:Record<string,unknown>}}[];
 contracts[0].parameter.value.call_value=1;
 await expect(signPrepared(p,step)).rejects.toThrow("TronLink cannot verify");
 expect(sign).not.toHaveBeenCalled();
});
it("preserves string errors from the wallet instead of telling the user to refresh blindly",async()=>{
 const {p,step}=setup("redeem","Invalid transaction");
 await expect(signPrepared(p,step)).rejects.toThrow("TronLink: Invalid transaction");
});
it("describes wallet cancellation and plain RPC error objects",()=>{
 expect(errorMessage({code:4001,message:"rejected"})).toContain("cancelled");
 expect(errorMessage({message:"Wallet locked"})).toBe("Wallet locked");
 expect(errorMessage({error:{message:"Request already open"}})).toBe("Request already open");
 expect(errorMessage("Transaction expired")).toBe("Transaction expired");
});
it("self-check rejects malformed transaction data",async()=>{
 await expect(assertWalletEncoding({txID:"a".repeat(64),raw_data_hex:"aa",raw_data:{}})).rejects.toThrow("TronLink cannot verify");
});
