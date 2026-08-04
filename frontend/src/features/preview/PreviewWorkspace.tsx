import { useEffect, useState } from "react";

import type { PlatformBridge } from "../../platform/types";
import type { PreviewApi, PreviewResponse } from "./preview-api";
import { PreviewPane } from "./PreviewPane";

export interface PreviewWorkspaceProps {
  readonly api: PreviewApi;
  readonly onClose: () => void;
  readonly onError?: (() => void) | undefined;
  readonly path: string;
  readonly platform: PlatformBridge;
  readonly projectId: string;
}

export function PreviewWorkspace({ api, onClose, onError, path, platform, projectId }: PreviewWorkspaceProps) {
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [objectUrl, setObjectUrl] = useState<string | undefined>();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let createdObjectUrl: string | undefined;
    void api.get(projectId, path).then(async (result) => {
      if (cancelled) return;
      setPreview(result);
      if (result.kind === "image" && result.mimeType !== "image/svg+xml" && result.contentUrl) {
        const blob = await api.download(result.contentUrl);
        if (!cancelled) {
          createdObjectUrl = URL.createObjectURL(blob);
          setObjectUrl(createdObjectUrl);
        }
      }
    }).catch(() => {
      if (!cancelled) {
        setError("文件预览加载失败");
        onError?.();
      }
    });
    return () => {
      cancelled = true;
      if (createdObjectUrl) URL.revokeObjectURL(createdObjectUrl);
    };
  }, [api, onError, path, projectId]);

  async function downloadUnsupported() {
    if (!preview || preview.kind !== "unsupported") return;
    try {
      const blob = await api.download(preview.downloadUrl);
      await platform.saveDownload({ blob, suggestedName: preview.path.split("/").at(-1) ?? "download" });
    } catch {
      setError("文件下载失败");
    }
  }

  return (
    <section aria-label={`文件预览 ${path}`} className="preview-workspace">
      <div className="workspace-heading">
        <div><p className="pane-label">安全预览</p><h2>{path}</h2></div>
        <button onClick={onClose} type="button">关闭预览</button>
      </div>
      {error ? <p role="alert">{error}</p> : null}
      {!preview && !error ? <p role="status">正在加载预览…</p> : null}
      {preview ? (
        <PreviewPane
          objectUrl={objectUrl}
          onDownload={preview.kind === "unsupported" ? () => void downloadUnsupported() : undefined}
          pdfLoadRange={preview.kind === "pdf"
            ? (begin, end) => api.downloadRange(preview.rangeUrl, begin, end)
            : undefined}
          preview={preview}
        />
      ) : null}
    </section>
  );
}
