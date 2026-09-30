import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ConversationCard } from "../src/components/ConversationCard";
import type { Machine } from "../src/api/useMachine";
it("an old comparison links to the current result instead of forcing another condition edit", () => {
  const m = {
    workspace: { data: { comparison: { id: "new-result" } } },
  } as unknown as Machine;
  const html = renderToStaticMarkup(
    <ConversationCard
      card={{ kind: "plans", target_id: "old-result" }}
      m={m}
      openConditions={() => {}}
      openPortfolio={() => {}}
    />,
  );
  expect(html).toContain("View latest comparison");
  expect(html).not.toContain("View current conditions");
});
