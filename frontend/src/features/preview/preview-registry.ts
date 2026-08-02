export type PreviewRenderer =
  | "pdf" | "image" | "spreadsheet" | "docx" | "markdown" | "text" | "unsupported";

const rendererByExtension: Readonly<Record<string, PreviewRenderer>> = {
  bmp: "image", csv: "spreadsheet", docx: "docx", gif: "image", jpeg: "image",
  jpg: "image", json: "text", log: "text", md: "markdown", pdf: "pdf", png: "image",
  py: "text", svg: "image", txt: "text", webp: "image", xlsx: "spreadsheet",
  yaml: "text", yml: "text",
};

export function selectPreviewRenderer(path: string): PreviewRenderer {
  const extension = path.split(".").at(-1)?.toLowerCase() ?? "";
  return rendererByExtension[extension] ?? "unsupported";
}
