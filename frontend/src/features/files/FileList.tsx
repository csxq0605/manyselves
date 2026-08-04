import { useState } from "react";

import type { FileEntry } from "./file-api";
import { FileRowMenu } from "./FileRowMenu";
import type { SectionCapabilities } from "./project-sections";

export interface FileListProps {
  readonly capabilities: SectionCapabilities;
  readonly entries: readonly FileEntry[];
  readonly onDelete: (entry: FileEntry) => void;
  readonly onDownload: (entry: FileEntry) => void;
  readonly onEdit: (entry: FileEntry) => void;
  readonly onPreview: (entry: FileEntry) => void;
}

function ancestorsBelowRoot(path: string, root: string): readonly string[] {
  const segments = path.split("/");
  const rootDepth = root ? root.split("/").length : 0;
  return segments
    .slice(rootDepth, -1)
    .map((_, index) => segments.slice(0, rootDepth + index + 1).join("/"));
}

export function FileList({ capabilities, entries, onDelete, onDownload, onEdit, onPreview }: FileListProps) {
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());
  const visibleEntries = entries.filter((entry) => (
    ancestorsBelowRoot(entry.path, capabilities.root).every((path) => expanded.has(path))
  ));

  return (
    <ul aria-label={`${capabilities.label}文件`} className="file-list">
      {visibleEntries.map((entry) => {
        const rootDepth = capabilities.root ? capabilities.root.split("/").length : 0;
        const depth = Math.max(0, entry.path.split("/").length - rootDepth - 1);
        if (entry.kind === "directory") {
          const isExpanded = expanded.has(entry.path);
          return (
            <li className="file-list__row file-list__row--directory" key={entry.path} style={{ paddingInlineStart: `${depth * 18}px` }}>
              <button
                aria-expanded={isExpanded}
                aria-label={`${isExpanded ? "收起" : "展开"} ${entry.name}`}
                className="file-list__directory"
                onClick={() => setExpanded((current) => {
                  const next = new Set(current);
                  if (isExpanded) next.delete(entry.path);
                  else next.add(entry.path);
                  return next;
                })}
                type="button"
              >
                <span aria-hidden="true">{isExpanded ? "▾" : "▸"}</span>
                <span>{entry.name}</span>
              </button>
            </li>
          );
        }
        return (
          <li className="file-list__row" key={entry.path} style={{ paddingInlineStart: `${depth * 18}px` }}>
            <span className="file-list__name">{entry.name}</span>
            <span className="file-list__size">{entry.size?.toLocaleString() ?? "—"} B</span>
            <FileRowMenu
              capabilities={capabilities}
              entry={entry}
              onDelete={onDelete}
              onDownload={onDownload}
              onEdit={onEdit}
              onPreview={onPreview}
            />
          </li>
        );
      })}
    </ul>
  );
}
