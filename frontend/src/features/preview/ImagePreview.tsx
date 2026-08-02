import DOMPurify from "dompurify";
import type { components } from "../../api/generated/schema";
import { useState } from "react";

type Preview = components["schemas"]["ImagePreview"];

function safeSvgDataUrl(content: string): string {
  const sanitized = DOMPurify.sanitize(content, {
    FORBID_TAGS: ["script", "foreignObject"],
    USE_PROFILES: { svg: true, svgFilters: true },
  });
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(sanitized)}`;
}

export function ImagePreview({
  objectUrl,
  preview,
}: { readonly objectUrl?: string | undefined; readonly preview: Preview }) {
  const [zoom, setZoom] = useState(1);
  const [fit, setFit] = useState(true);
  const source = preview.mimeType === "image/svg+xml" && preview.content
    ? safeSvgDataUrl(preview.content)
    : objectUrl ?? preview.contentUrl ?? "";
  return (
    <section aria-label={`图片预览 ${preview.path}`}>
      <div className="preview-toolbar">
        <button onClick={() => { setFit(false); setZoom((value) => Math.max(0.25, value - 0.25)); }} type="button">缩小</button>
        <button onClick={() => { setFit(true); setZoom(1); }} type="button">适合窗口</button>
        <button onClick={() => { setFit(false); setZoom((value) => Math.min(4, value + 0.25)); }} type="button">放大</button>
      </div>
      <img alt={preview.path} src={source} style={{
        maxHeight: fit ? "62vh" : "none",
        maxWidth: fit ? "100%" : "none",
        transform: fit ? "none" : `scale(${zoom})`,
        transformOrigin: "top left",
      }} />
    </section>
  );
}
