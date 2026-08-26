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

const PAGE_SIZE = 20;

const fileNameCollator = new Intl.Collator("zh-CN", {
  numeric: true,
  sensitivity: "base",
});

function parentPath(path: string): string {
  return path.split("/").slice(0, -1).join("/");
}

function orderAsTree(entries: readonly FileEntry[], root: string): readonly FileEntry[] {
  const children = new Map<string, FileEntry[]>();
  for (const entry of entries) {
    const parent = parentPath(entry.path);
    children.set(parent, [...(children.get(parent) ?? []), entry]);
  }
  const ordered: FileEntry[] = [];
  const visited = new Set<string>();
  const appendChildren = (parent: string) => {
    const siblings = [...(children.get(parent) ?? [])].sort((left, right) => {
      if (left.kind !== right.kind) return left.kind === "directory" ? -1 : 1;
      return fileNameCollator.compare(left.name, right.name);
    });
    for (const entry of siblings) {
      if (visited.has(entry.path)) continue;
      visited.add(entry.path);
      ordered.push(entry);
      if (entry.kind === "directory") appendChildren(entry.path);
    }
  };
  appendChildren(root);
  for (const entry of entries) {
    if (!visited.has(entry.path)) ordered.push(entry);
  }
  return ordered;
}

function ancestorsBelowRoot(path: string, root: string): readonly string[] {
  const segments = path.split("/");
  const rootDepth = root ? root.split("/").length : 0;
  return segments
    .slice(rootDepth, -1)
    .map((_, index) => segments.slice(0, rootDepth + index + 1).join("/"));
}

export function FileList({
  capabilities,
  entries,
  onDelete,
  onDownload,
  onEdit,
  onPreview,
}: FileListProps) {
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());
  const [page, setPage] = useState(0);
  const orderedEntries = orderAsTree(entries, capabilities.root);
  const rootEntries = orderedEntries.filter((entry) => (
    parentPath(entry.path) === capabilities.root
  ));
  const totalPages = Math.max(1, Math.ceil(rootEntries.length / PAGE_SIZE));
  const boundedPage = Math.min(page, totalPages - 1);
  const pageRootPaths = new Set(rootEntries.slice(
    boundedPage * PAGE_SIZE,
    boundedPage * PAGE_SIZE + PAGE_SIZE,
  ).map((entry) => entry.path));
  const pageEntries = orderedEntries.filter((entry) => {
    const rootEntry = ancestorsBelowRoot(entry.path, capabilities.root)[0] ?? entry.path;
    return pageRootPaths.has(rootEntry) && ancestorsBelowRoot(
      entry.path,
      capabilities.root,
    ).every((path) => expanded.has(path));
  });

  return (
    <section className="file-list-panel">
      <div aria-label={`${capabilities.root} file scroll area`} className="file-list__viewport" role="region">
        <ul aria-label={`${capabilities.label}文件`} className="file-list">
          {pageEntries.map((entry) => {
            const rootDepth = capabilities.root ? capabilities.root.split("/").length : 0;
            const depth = Math.max(0, entry.path.split("/").length - rootDepth - 1);
            if (entry.kind === "directory") {
              const isExpanded = expanded.has(entry.path);
              return (
                <li
                  className={`file-list__row file-list__row--directory${depth === 0 ? " file-list__row--root-directory" : ""}`}
                  key={entry.path}
                  style={{ paddingInlineStart: `${18 + depth * 24}px` }}
                >
                  <button
                    aria-expanded={isExpanded}
                    aria-label={`${isExpanded ? "收起" : "展开"} ${entry.name}`}
                    className="file-list__directory"
                    onClick={() => {
                      setExpanded((current) => {
                        const next = new Set(current);
                        if (isExpanded) next.delete(entry.path);
                        else next.add(entry.path);
                        return next;
                      });
                      setPage(0);
                    }}
                    type="button"
                  >
                    <span aria-hidden="true" className="file-list__chevron">{isExpanded ? "▾" : "▸"}</span>
                    <span aria-hidden="true" className="file-list__folder-icon" />
                    <span className="file-list__directory-name">{entry.name}</span>
                  </button>
                  {capabilities.downloadDirectory ? (
                    <div aria-label={`${entry.name} 文件操作`} className="file-row-menu" role="group">
                      <button aria-label={`下载 ${entry.name}`} onClick={() => onDownload(entry)} type="button">
                        下载文件夹
                      </button>
                    </div>
                  ) : null}
                </li>
              );
            }
            return (
              <li className="file-list__row" key={entry.path} style={{ paddingInlineStart: `${18 + depth * 24}px` }}>
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
      </div>
      <div className="file-list__pagination">
        <span>Page {boundedPage + 1} / {totalPages}</span>
        <div>
          <button
            aria-label="Previous page"
            disabled={boundedPage === 0}
            onClick={() => setPage((current) => Math.max(0, current - 1))}
            type="button"
          >
            上一页
          </button>
          <button
            aria-label="Next page"
            disabled={boundedPage >= totalPages - 1}
            onClick={() => setPage((current) => Math.min(totalPages - 1, current + 1))}
            type="button"
          >
            下一页
          </button>
        </div>
      </div>
    </section>
  );
}
