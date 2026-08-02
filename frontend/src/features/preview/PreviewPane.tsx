import type { PreviewResponse } from "./preview-api";
import { DocxPreview } from "./DocxPreview";
import { ImagePreview } from "./ImagePreview";
import { MarkdownPreview } from "./MarkdownPreview";
import { PdfPreview } from "./PdfPreview";
import { SpreadsheetPreview } from "./SpreadsheetPreview";
import { TextPreview } from "./TextPreview";

export interface PreviewPaneProps {
  readonly objectUrl?: string | undefined;
  readonly onDownload?: (() => void) | undefined;
  readonly pdfData?: Uint8Array | undefined;
  readonly pdfLoadRange?: ((begin: number, end: number) => Promise<Uint8Array>) | undefined;
  readonly preview: PreviewResponse;
}

export function PreviewPane({ objectUrl, onDownload, pdfData, pdfLoadRange, preview }: PreviewPaneProps) {
  switch (preview.kind) {
    case "pdf":
      return <PdfPreview data={pdfData} loadRange={pdfLoadRange} path={preview.path} size={preview.size} />;
    case "image":
      return <ImagePreview objectUrl={objectUrl} preview={preview} />;
    case "spreadsheet":
      return <SpreadsheetPreview preview={preview} />;
    case "docx":
      return <DocxPreview preview={preview} />;
    case "markdown":
      return <MarkdownPreview preview={preview} />;
    case "text":
      return <TextPreview preview={preview} />;
    case "unsupported":
      return (
        <section aria-label={`不支持的文件 ${preview.path}`}>
          <h3>此格式暂不支持在线预览</h3>
          <dl>
            <div><dt>类型</dt><dd>{preview.mimeType}</dd></div>
            <div><dt>大小</dt><dd>{preview.size.toLocaleString()} 字节</dd></div>
          </dl>
          {onDownload ? <button onClick={onDownload} type="button">下载文件</button> : null}
        </section>
      );
  }
}
