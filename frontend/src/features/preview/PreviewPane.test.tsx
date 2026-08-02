import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { PreviewPane } from "./PreviewPane";
import { selectPreviewRenderer } from "./preview-registry";

describe("preview registry", () => {
  it.each([
    ["a.pdf", "pdf"], ["a.png", "image"], ["a.svg", "image"],
    ["a.xlsx", "spreadsheet"], ["a.csv", "spreadsheet"], ["a.docx", "docx"],
    ["a.md", "markdown"], ["a.py", "text"], ["a.bin", "unsupported"],
  ] as const)("selects %s renderer", (path, renderer) => {
    expect(selectPreviewRenderer(path)).toBe(renderer);
  });
});

describe("PreviewPane", () => {
  it("does not activate HTML embedded in markdown", () => {
    const { container } = render(
      <PreviewPane preview={{
        content: "# Safe\n<img src=x onerror=alert(1)><script>alert(1)</script>",
        kind: "markdown", path: "a.md", truncated: false,
      }} />,
    );

    expect(screen.getByRole("heading", { name: "Safe" })).toBeVisible();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
  });

  it("sanitizes SVG again before rendering it as an isolated image", () => {
    const { container } = render(
      <PreviewPane preview={{
        content: '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(1)</script><rect width="2" height="2"/></svg>',
        kind: "image", mimeType: "image/svg+xml", path: "a.svg",
      }} />,
    );

    const image = screen.getByRole("img", { name: "a.svg" });
    expect(image.getAttribute("src")).not.toContain("script");
    expect(image.getAttribute("src")).not.toContain("onload");
    expect(container.querySelector("script")).toBeNull();
  });

  it("switches bounded spreadsheet sheets and exposes truncation", async () => {
    render(<PreviewPane preview={{
      kind: "spreadsheet", path: "a.xlsx", sheets: [
        { columnCount: 1, name: "One", rowCount: 1, rows: [["A"]], truncated: false },
        { columnCount: 1, name: "Two", rowCount: 2, rows: [["B"]], truncated: true },
      ],
    }} />);

    screen.getByRole("tab", { name: "Two" }).click();
    expect(await screen.findByText("B")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("已截断");
  });

  it("renders bounded text and ordered DOCX blocks", () => {
    const { rerender } = render(<PreviewPane preview={{
      content: "partial", kind: "text", path: "a.txt", truncated: true,
    }} />);
    expect(screen.getByText("partial")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("已截断");

    rerender(<PreviewPane preview={{ blocks: [
      { kind: "paragraph", text: "First" },
      { kind: "table", rows: [["Cell"]] },
      { dataUrl: "data:image/png;base64,AA==", kind: "image", mimeType: "image/png" },
    ], kind: "docx", path: "a.docx", truncated: false }} />);
    expect(screen.getByText("First")).toBeVisible();
    expect(screen.getByText("Cell")).toBeVisible();
    expect(screen.getByRole("img", { name: "文档图片 3" })).toBeVisible();
  });

  it("shows metadata and downloads unsupported files without decoding them", async () => {
    const onDownload = vi.fn();
    const user = userEvent.setup();
    render(<PreviewPane onDownload={onDownload} preview={{
      downloadUrl: "/download", kind: "unsupported", mimeType: "application/octet-stream",
      path: "a.bin", size: 128,
    }} />);

    expect(screen.getByText("application/octet-stream")).toBeVisible();
    expect(screen.getByText("128 字节")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "下载文件" }));
    expect(onDownload).toHaveBeenCalledOnce();
  });

  it("dispatches PDF metadata to the range-capable renderer", () => {
    render(<PreviewPane preview={{
      kind: "pdf", mimeType: "application/pdf", path: "a.pdf", rangeUrl: "/range", size: 1024,
    }} />);
    expect(screen.getByRole("status")).toHaveTextContent("正在读取 PDF");
  });
});
