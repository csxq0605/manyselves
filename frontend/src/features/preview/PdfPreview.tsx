import { useEffect, useRef, useState } from "react";
import type { PDFDocumentLoadingTask, PDFDocumentProxy, PDFPageProxy, RenderTask } from "pdfjs-dist";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

export interface PdfPreviewProps {
  readonly data?: Uint8Array | undefined;
  readonly loadRange?: ((begin: number, end: number) => Promise<Uint8Array>) | undefined;
  readonly path: string;
  readonly size?: number | undefined;
}

export function PdfPreview({ data, loadRange, path, size }: PdfPreviewProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const renderTaskRef = useRef<RenderTask | null>(null);
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null);
  const [page, setPage] = useState(1);
  const [pageCount, setPageCount] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!data && (!loadRange || !size)) return;
    let cancelled = false;
    let loadingTask: PDFDocumentLoadingTask | null = null;
    void import("pdfjs-dist").then(async (pdfjs) => {
      pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
      if (data) {
        loadingTask = pdfjs.getDocument({ data: data.slice() });
      } else {
        class AuthenticatedRangeTransport extends pdfjs.PDFDataRangeTransport {
          private aborted = false;

          override abort() { this.aborted = true; }

          override requestDataRange(begin: number, end: number) {
            void loadRange?.(begin, end).then((bytes) => {
              if (!this.aborted) this.onDataRange(begin, bytes);
            }).catch(() => {
              if (!this.aborted) setError("PDF 分段读取失败");
            });
          }
        }
        loadingTask = pdfjs.getDocument({
          range: new AuthenticatedRangeTransport(size as number, null),
          rangeChunkSize: 64 * 1024,
        });
      }
      const loadedDocument = await loadingTask.promise;
      if (cancelled) {
        await loadingTask.destroy();
        return;
      }
      setDocument(loadedDocument);
      setPageCount(loadedDocument.numPages);
      setPage(1);
      setError(null);
    }).catch(() => {
      if (!cancelled) setError("PDF 加载失败");
    });
    return () => {
      cancelled = true;
      renderTaskRef.current?.cancel();
      if (loadingTask) void loadingTask.destroy();
    };
  }, [data, loadRange, size]);

  useEffect(() => {
    if (!document || !canvasRef.current) return;
    let cancelled = false;
    let loadedPage: PDFPageProxy | null = null;
    void document.getPage(page).then((pdfPage) => {
      if (cancelled || !canvasRef.current) return;
      loadedPage = pdfPage;
      const viewport = pdfPage.getViewport({ scale: zoom });
      const canvas = canvasRef.current;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("Canvas context unavailable");
      const pixelRatio = window.devicePixelRatio || 1;
      canvas.width = Math.floor(viewport.width * pixelRatio);
      canvas.height = Math.floor(viewport.height * pixelRatio);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      const renderTask = pdfPage.render({
        canvas,
        canvasContext: context,
        transform: pixelRatio === 1 ? undefined : [pixelRatio, 0, 0, pixelRatio, 0, 0],
        viewport,
      });
      renderTaskRef.current = renderTask;
      return renderTask.promise;
    }).catch((reason: unknown) => {
      if (!cancelled && !(reason instanceof Error && reason.name === "RenderingCancelledException")) {
        setError("PDF 页面渲染失败");
      }
    });
    return () => {
      cancelled = true;
      renderTaskRef.current?.cancel();
      loadedPage?.cleanup();
    };
  }, [document, page, zoom]);

  async function fitWidth() {
    if (!document || !viewportRef.current) return;
    const pdfPage = await document.getPage(page);
    const baseViewport = pdfPage.getViewport({ scale: 1 });
    const availableWidth = Math.max(1, viewportRef.current.clientWidth - 18);
    setZoom(Math.min(3, Math.max(0.5, availableWidth / baseViewport.width)));
  }

  if (!data && (!loadRange || !size)) return <p role="status">正在读取 PDF…</p>;
  return (
    <section aria-label={`PDF 预览 ${path}`} className="pdf-preview">
      <div className="preview-toolbar">
        <button disabled={page <= 1} onClick={() => setPage((value) => value - 1)} type="button">上一页</button>
        <span>{pageCount ? `${page} / ${pageCount}` : "正在载入…"}</span>
        <button disabled={!pageCount || page >= pageCount} onClick={() => setPage((value) => value + 1)} type="button">下一页</button>
        <button onClick={() => setZoom((value) => Math.max(0.5, value - 0.25))} type="button">缩小</button>
        <button onClick={() => void fitWidth()} type="button">适合宽度</button>
        <button onClick={() => setZoom((value) => Math.min(3, value + 0.25))} type="button">放大</button>
      </div>
      {error ? <p role="alert">{error}</p> : null}
      <div className="pdf-preview__viewport" ref={viewportRef}><canvas ref={canvasRef} /></div>
    </section>
  );
}
