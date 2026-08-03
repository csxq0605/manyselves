import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { RuntimeToolView } from "../agents/event-reducer";
import { ToolCallGroup } from "./ToolCallGroup";

describe("ToolCallGroup", () => {
  it("groups tool calls by agent and keeps large payloads collapsed", () => {
    const tools: RuntimeToolView[] = [{
      agentId: "researcher",
      arguments: { authorization: "[REDACTED]", query: "market data" },
      error: "permission denied",
      id: "tool-1",
      name: "search",
      result: null,
      status: "failed",
    }];

    render(<ToolCallGroup tools={tools} />);

    expect(screen.getByText("researcher · search")).toBeVisible();
    expect(screen.getByText("失败")).toBeVisible();
    const disclosure = screen.getByText("参数、结果与错误").closest("details");
    expect(disclosure).not.toHaveAttribute("open");
    expect(document.body).not.toHaveTextContent("provider-secret");
  });
});
