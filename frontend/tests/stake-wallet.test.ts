import { expect,it } from "vitest";
import vectors from "./fixtures/native-stake-transactions.json";
import { assertWalletEncoding,assertStakeScope } from "../src/lib/wallet";
import type { Graph,Transaction } from "../src/api/contracts";
const toHex=(a:string)=>a;
const step=(name:keyof typeof vectors)=>({action:vectors[name].raw_data.contract[0].type,amount:{value:"600",symbol:"TRX",decimals:6},native_votes:[{vote_address:"41"+"12".repeat(20),vote_count:600}]} as Graph["steps"][number]);
it("uses TronWeb's real serializer for all five native actions",async()=>{
 for (const tx of Object.values(vectors)) await expect(assertWalletEncoding(tx as Transaction)).resolves.toBeUndefined();
});
it("binds native amount, Energy resource, representative and votes to the review",()=>{
 for (const [k,tx] of Object.entries(vectors)) expect(()=>assertStakeScope(tx as Transaction,step(k as keyof typeof vectors),toHex)).not.toThrow();
 const wrong=structuredClone(vectors.STAKE);wrong.raw_data.contract[0].parameter.value.frozen_balance+=1;
 expect(()=>assertStakeScope(wrong as Transaction,step("STAKE"),toHex)).toThrow("amount");
 const vote=structuredClone(vectors.VOTE);vote.raw_data.contract[0].parameter.value.votes[0].vote_count+=1;
 expect(()=>assertStakeScope(vote as Transaction,step("VOTE"),toHex)).toThrow("vote count");
 const unexpected=structuredClone(vectors.CLAIM) as Transaction;
 (unexpected.raw_data.contract as {parameter:{value:Record<string,unknown>}}[])[0].parameter.value.to_address="41"+"ff".repeat(20);
 expect(()=>assertStakeScope(unexpected,step("CLAIM"),toHex)).toThrow("Unexpected");
});
