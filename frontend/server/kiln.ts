export type ChatMessage = {
  role: "system" | "user" | "assistant";
  content: string;
};
export async function* kilnStream(
  messages: ChatMessage[],
  signal: AbortSignal,
) {
  const key = process.env.KILN_API_KEY;
  if (!key) throw new Error("Kiln is not configured on the server.");
  const response = await fetch("https://api.bricksum.com/v1/chat/completions", {
    method: "POST",
    signal,
    headers: {
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: "qwen3-32b",
      messages,
      stream: true,
      stream_options: { include_usage: true },
      max_tokens: 2048,
      temperature: 0.4,
    }),
  });
  if (!response.ok)
    throw new Error(
      `Kiln request failed (HTTP ${response.status}). Check the server key, model availability or usage limit.`,
    );
  if (!response.body) throw new Error("Kiln returned no response stream.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      if (done && buffer) {
        lines.push(buffer);
        buffer = "";
      }
      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        const data = line.slice(5).trim();
        if (!data || data === "[DONE]") continue;
        const event = JSON.parse(data);
        if (event.error) throw new Error("Kiln reported a generation error.");
        const text = event.choices?.[0]?.delta?.content;
        if (typeof text === "string") yield { text };
        if (event.usage) yield { usage: event.usage, model: event.model };
      }
      if (done) break;
    }
  } finally {
    reader.releaseLock();
  }
}
