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
const ROW_BASE_PADDING = 24;
const ROW_DEPTH_INDENT = 18;

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
  const paginationKey = `${capabilities.root}\u0000${entries.map((entry) => entry.path).join("\u0000")}`;
  const [pagination, setPagination] = useState({ key: paginationKey, page: 0 });
  const page = pagination.key === paginationKey ? pagination.page : 0;
  const setPage = (nextPage: number | ((currentPage: number) => number)) => {
    setPagination((current) => {
      const currentPage = current.key === paginationKey ? current.page : 0;
      return {
        key: paginationKey,
        page: typeof nextPage === "function" ? nextPage(currentPage) : nextPage,
      };
    });
  };
  const visibleEntries = entries.filter((entry) => (
    ancestorsBelowRoot(entry.path, capabilities.root).every((path) => expanded.has(path))
  ));
  const totalPages = Math.max(1, Math.ceil(visibleEntries.length / PAGE_SIZE));
  const boundedPage = Math.min(page, totalPages - 1);
  const pageEntries = visibleEntries.slice(
    boundedPage * PAGE_SIZE,
    boundedPage * PAGE_SIZE + PAGE_SIZE,
  );

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
                  className="file-list__row file-list__row--directory"
                  key={entry.path}
                  style={{ paddingInlineStart: `${ROW_BASE_PADDING + depth * ROW_DEPTH_INDENT}px` }}
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
                    <span aria-hidden="true">{isExpanded ? "▾" : "▸"}</span>
                    <span>{entry.name}</span>
                  </button>
                </li>
              );
            }
            return (
              <li
                className="file-list__row"
                key={entry.path}
                style={{ paddingInlineStart: `${ROW_BASE_PADDING + depth * ROW_DEPTH_INDENT}px` }}
              >
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
