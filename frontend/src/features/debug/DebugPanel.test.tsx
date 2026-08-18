import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { RuntimeDebugView, RuntimeUsageView } from "../agents/event-reducer";
import { DebugPanel } from "./DebugPanel";

describe("DebugPanel", () => {
  it("renders API failures and aggregate usage without exposing secrets", () => {
    const entries: RuntimeDebugView[] = [{
      agentId: "main",
      durationMs: 420,
      error: "Bearer [REDACTED]",
      id: "debug-1",
      model: "model-a",
      status: "error",
      timestamp: "2026-08-03T08:00:00Z",
      tokensIn: 12,
      tokensOut: 8,
    }];
    const usage: RuntimeUsageView = { durationMs: 420, tokensIn: 12, tokensOut: 8 };

    render(<DebugPanel entries={entries} usage={usage} />);

    expect(screen.getByRole("region", { name: "API 调试" })).toHaveTextContent("20 tokens · 420 ms");
    expect(screen.getByText("API 调用失败")).toBeVisible();
    expect(document.body).not.toHaveTextContent("provider-secret");
  });
});
