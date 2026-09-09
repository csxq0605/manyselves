import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/gateway";
import type { PlatformBridge } from "../../platform/types";
import { PreviewWorkspace } from "./PreviewWorkspace";

describe("PreviewWorkspace errors", () => {
  it.each([
    ["PREVIEW_TOO_LARGE", "文件超过安全预览限制，请关闭预览后下载查看。"],
    ["PREVIEW_INVALID_DOCUMENT", "文件预览加载失败"],
  ])("explains %s without changing preview limits", async (code, message) => {
    const onError = vi.fn();
    const api = {
      download: vi.fn(), downloadRange: vi.fn(),
      get: vi.fn().mockRejectedValue(new ApiError({
        code, details: {}, message: "server detail", requestId: "preview-test",
        retryable: false, status: 413,
      })),
    };
    render(<PreviewWorkspace api={api} onClose={vi.fn()} onError={onError}
      path="Outputs/Reports/report.docx" platform={{} as PlatformBridge} projectId="test" />);

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(api.get).toHaveBeenCalledOnce();
    expect(api.download).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledOnce();
  });
});
