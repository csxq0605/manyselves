import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { OperationApi } from "./operation-api";
import { PythonRunAction } from "./PythonRunAction";

describe("PythonRunAction", () => {
  it("stops polling after the server reports a timeout", async () => {
    const get = vi.fn(async () => ({ arguments: [], completedAt: new Date().toISOString(),
      operationId: "op-timeout", path: "a.py", returnCode: null,
      startedAt: new Date().toISOString(), status: "timed_out", stderr: "timeout",
      stderrTruncated: false, stdout: "", stdoutTruncated: false }));
    const api: OperationApi = {
      get,
      interrupt: vi.fn(),
      run: async () => ({ commandId: crypto.randomUUID(), operationId: "op-timeout", status: "accepted" }),
    };
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();

    render(<PythonRunAction api={api} path="a.py" pollIntervalMs={1} />);
    await user.click(screen.getByRole("button", { name: "在可信服务器运行" }));
    expect(await screen.findByText("状态：timed_out")).toBeVisible();
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(get).toHaveBeenCalledOnce();
  });

  it("requires trusted-server confirmation and can interrupt an accepted operation", async () => {
    const calls: string[] = [];
    const api: OperationApi = {
      get: async () => ({ arguments: [], completedAt: null, operationId: "op-1", path: "a.py",
        returnCode: null, startedAt: new Date().toISOString(), status: "running", stderr: "",
        stderrTruncated: false, stdout: "started", stdoutTruncated: false }),
      interrupt: async (id) => {
        calls.push(`interrupt:${id}`);
        return { arguments: [], completedAt: null, operationId: id, path: "a.py", returnCode: null,
          startedAt: new Date().toISOString(), status: "interrupted", stderr: "",
          stderrTruncated: false, stdout: "started", stdoutTruncated: false };
      },
      run: async (path) => {
        calls.push(`run:${path}`);
        return { commandId: crypto.randomUUID(), operationId: "op-1", status: "accepted" };
      },
    };
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();

    render(<PythonRunAction api={api} path="Scripts/a.py" pollIntervalMs={0} />);
    await user.click(screen.getByRole("button", { name: "在可信服务器运行" }));
    expect(await screen.findByText("started")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "中断运行" }));

    expect(calls).toEqual(["run:Scripts/a.py", "interrupt:op-1"]);
  });
});
