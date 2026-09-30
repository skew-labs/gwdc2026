import { expect, it } from "vitest";
import { errorMessage, readableServiceError } from "../src/lib/errors";

it.each([
  `Unexpected token 'I', "Internal S"... is not valid JSON`,
  "Unexpected end of JSON input",
  "JSON.parse: unexpected character at line 1 column 1 of the JSON data",
])(
  "turns parser diagnostics into a status-first recovery message: %s",
  (raw) => {
    for (const value of [
      raw,
      new SyntaxError(raw),
      { error: { message: raw } },
    ]) {
      expect(errorMessage(value)).toContain("Refresh the current status");
      expect(errorMessage(value)).not.toContain(raw);
      expect(errorMessage(value)).not.toContain("No transaction");
    }
  },
);

it("preserves actionable wallet and policy errors", () => {
  expect(readableServiceError("Review your 2 TRX allocation limit.")).toBe(
    "Review your 2 TRX allocation limit.",
  );
  expect(errorMessage({ message: "Wallet locked" })).toBe("Wallet locked");
});
