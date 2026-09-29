/** Wallet extensions may reject with a string or a JSON-RPC object. */
export function errorMessage(error: unknown): string {
  if (error && typeof error === "object" && "code" in error && error.code === 4001)
    return "You cancelled the wallet request. No transaction was sent by this request.";
  if (typeof error === "string" && error.trim()) return error.slice(0, 500);
  if (error && typeof error === "object") {
    for (const candidate of [error, "error" in error ? error.error : null, "data" in error ? error.data : null]) {
      if (candidate && typeof candidate === "object" && "message" in candidate && typeof candidate.message === "string" && candidate.message.trim())
        return candidate.message.slice(0, 500);
    }
  }
  return "This step could not be completed. Check the wallet connection and try again.";
}
