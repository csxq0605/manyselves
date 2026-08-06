import { useMemo, useState } from "react";

import type { FileEntry } from "./file-api";
import { FileRowMenu } from "./FileRowMenu";
import type { SectionCapabilities } from "./project-sections";

export interface OutputTabsProps {
  readonly capabilities: SectionCapabilities;
  readonly entries: readonly FileEntry[];
  readonly onDelete: (entry: FileEntry) => void;
  readonly onDownload: (entry: FileEntry) => void;
  readonly onEdit: (entry: FileEntry) => void;
  readonly onPreview: (entry: FileEntry) => void;
}

const OUTPUT_TABS = [
  { label: "Modules", path: "Outputs/Modules" },
  { label: "Reports", path: "Outputs/Reports" },
  { label: "Reviews", path: "Outputs/Reviews" },
] as const;
const PAGE_SIZE = 20;

function entriesForTab(entries: readonly FileEntry[], tabPath: string): FileEntry[] {
  return entries
    .filter((entry) => entry.kind === "file" && entry.path.startsWith(`${tabPath}/`))
    .sort((left, right) => left.path.localeCompare(right.path));
}

export function OutputTabs({
  capabilities,
  entries,
  onDelete,
  onDownload,
  onEdit,
  onPreview,
}: OutputTabsProps) {
  const [activePath, setActivePath] = useState<(typeof OUTPUT_TABS)[number]["path"]>("Outputs/Modules");
  const [pages, setPages] = useState<Record<string, number>>({});
  const grouped = useMemo(() => Object.fromEntries(
    OUTPUT_TABS.map((tab) => [tab.path, entriesForTab(entries, tab.path)]),
  ) as Record<(typeof OUTPUT_TABS)[number]["path"], FileEntry[]>, [entries]);
  const activeTab = OUTPUT_TABS.find((tab) => tab.path === activePath) ?? OUTPUT_TABS[0];
  const activeEntries = grouped[activeTab.path] ?? [];
  const page = pages[activeTab.path] ?? 0;
  const totalPages = Math.max(1, Math.ceil(activeEntries.length / PAGE_SIZE));
  const boundedPage = Math.min(page, totalPages - 1);
  const visibleEntries = activeEntries.slice(
    boundedPage * PAGE_SIZE,
    boundedPage * PAGE_SIZE + PAGE_SIZE,
  );

  function setActivePage(nextPage: number) {
    setPages((current) => ({
      ...current,
      [activeTab.path]: Math.max(0, Math.min(totalPages - 1, nextPage)),
    }));
  }

  return (
    <section className="output-tabs">
      <div aria-label="Output categories" className="output-tabs__tablist" role="tablist">
        {OUTPUT_TABS.map((tab) => {
          const selected = tab.path === activeTab.path;
          const count = grouped[tab.path]?.length ?? 0;
          return (
            <button
              aria-controls={`output-panel-${tab.label}`}
              aria-selected={selected}
              className={selected ? "output-tabs__tab output-tabs__tab--active" : "output-tabs__tab"}
              id={`output-tab-${tab.label}`}
              key={tab.path}
              onClick={() => setActivePath(tab.path)}
              role="tab"
              type="button"
            >
              <span>{tab.label}</span>
              <strong>{count}</strong>
            </button>
          );
        })}
      </div>

      <div
        aria-labelledby={`output-tab-${activeTab.label}`}
        className="output-tabs__panel"
        id={`output-panel-${activeTab.label}`}
        role="tabpanel"
      >
        <div aria-label={`${activeTab.label} output scroll area`} className="output-tabs__viewport" role="region">
          {visibleEntries.length === 0 ? (
            <p className="project-directory__empty">这个分类还没有输出文件</p>
          ) : (
            <ul aria-label={`${activeTab.label} output files`} className="file-list output-tabs__list">
              {visibleEntries.map((entry) => (
                <li className="file-list__row" key={entry.path}>
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
              ))}
            </ul>
          )}
        </div>

        <div className="output-tabs__pagination">
          <span>Page {boundedPage + 1} / {totalPages}</span>
          <div>
            <button
              aria-label="Previous page"
              disabled={boundedPage === 0}
              onClick={() => setActivePage(boundedPage - 1)}
              type="button"
            >
              上一页
            </button>
            <button
              aria-label="Next page"
              disabled={boundedPage >= totalPages - 1}
              onClick={() => setActivePage(boundedPage + 1)}
              type="button"
            >
              下一页
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}
