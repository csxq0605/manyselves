import { isEditableTextPath } from "../editor/editable-files";
import { selectPreviewRenderer } from "../preview/preview-registry";
import type { FileEntry } from "./file-api";
import type { SectionCapabilities } from "./project-sections";

export interface FileRowMenuProps {
  readonly capabilities: SectionCapabilities;
  readonly entry: FileEntry;
  readonly onDelete: (entry: FileEntry) => void;
  readonly onDownload: (entry: FileEntry) => void;
  readonly onEdit: (entry: FileEntry) => void;
  readonly onPreview: (entry: FileEntry) => void;
}

export function FileRowMenu({
  capabilities,
  entry,
  onDelete,
  onDownload,
  onEdit,
  onPreview,
}: FileRowMenuProps) {
  const canEdit = capabilities.edit && isEditableTextPath(entry.path);
  const canPreview = capabilities.preview && selectPreviewRenderer(entry.path) !== "unsupported";

  return (
    <div aria-label={`${entry.name} 文件操作`} className="file-row-menu" role="group">
      {canEdit ? <button aria-label={`编辑 ${entry.name}`} onClick={() => onEdit(entry)} type="button">编辑</button> : null}
      {canPreview ? <button aria-label={`预览 ${entry.name}`} onClick={() => onPreview(entry)} type="button">预览</button> : null}
      {capabilities.download ? <button aria-label={`下载 ${entry.name}`} onClick={() => onDownload(entry)} type="button">下载</button> : null}
      {capabilities.delete ? <button aria-label={`删除 ${entry.name}`} onClick={() => onDelete(entry)} type="button">删除</button> : null}
    </div>
  );
}
