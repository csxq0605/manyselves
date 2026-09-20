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

interface OutputTab {
  readonly label: string;
  readonly path: string;
}

const OUTPUT_ROOT = "Outputs";
const PAGE_SIZE = 20;

function entriesForTab(entries: readonly FileEntry[], tabPath: string): FileEntry[] {
  return entries
    .filter((entry) => {
      if (entry.kind !== "file") return false;
      if (tabPath === OUTPUT_ROOT) {
        return entry.path.startsWith(`${OUTPUT_ROOT}/`)
          && !entry.path.slice(OUTPUT_ROOT.length + 1).includes("/");
      }
      return entry.path.startsWith(`${tabPath}/`);
    })
    .sort((left, right) => left.path.localeCompare(right.path));
}

function outputTabs(entries: readonly FileEntry[]): OutputTab[] {
  const paths = new Map<string, string>();
  for (const entry of entries) {
    if (!entry.path.startsWith(`${OUTPUT_ROOT}/`)) continue;
    const relative = entry.path.slice(OUTPUT_ROOT.length + 1);
    const [category, child] = relative.split("/", 2);
    if (!category) continue;
    if (entry.kind === "directory" && child === undefined) {
      paths.set(entry.path, entry.name);
    } else if (entry.kind === "file") {
      if (child === undefined) paths.set(OUTPUT_ROOT, "Files");
      else paths.set(`${OUTPUT_ROOT}/${category}`, category);
    }
  }
  return Array.from(paths, ([path, label]) => ({ label, path })).sort((left, right) => {
    if (left.path === OUTPUT_ROOT) return -1;
    if (right.path === OUTPUT_ROOT) return 1;
    return left.label.localeCompare(right.label);
  });
}

function tabDomId(tab: OutputTab): string {
  return encodeURIComponent(tab.path).replaceAll("%", "-");
}

export function OutputTabs({
  capabilities,
  entries,
  onDelete,
  onDownload,
  onEdit,
  onPreview,
}: OutputTabsProps) {
  const tabs = useMemo(() => outputTabs(entries), [entries]);
  const [activePath, setActivePath] = useState<string>("");
  const [pages, setPages] = useState<Record<string, number>>({});
  const grouped = useMemo(() => Object.fromEntries(
    tabs.map((tab) => [tab.path, entriesForTab(entries, tab.path)]),
  ) as Record<string, FileEntry[]>, [entries, tabs]);
  const activeTab = tabs.find((tab) => tab.path === activePath) ?? tabs[0];
  if (!activeTab) return null;
  const activeEntries = grouped[activeTab.path] ?? [];
  const page = pages[activeTab.path] ?? 0;
  const totalPages = Math.max(1, Math.ceil(activeEntries.length / PAGE_SIZE));
  const boundedPage = Math.min(page, totalPages - 1);
  const activeTabPath = activeTab.path;
  const visibleEntries = activeEntries.slice(
    boundedPage * PAGE_SIZE,
    boundedPage * PAGE_SIZE + PAGE_SIZE,
  );

  function setActivePage(nextPage: number) {
    setPages((current) => ({
      ...current,
      [activeTabPath]: Math.max(0, Math.min(totalPages - 1, nextPage)),
    }));
  }

  return (
    <section className="output-tabs">
      <div aria-label="Output categories" className="output-tabs__tablist" role="tablist">
        {tabs.map((tab) => {
          const selected = tab.path === activeTab.path;
          const count = grouped[tab.path]?.length ?? 0;
          const domId = tabDomId(tab);
          return (
            <button
              aria-controls={`output-panel-${domId}`}
              aria-selected={selected}
              className={selected ? "output-tabs__tab output-tabs__tab--active" : "output-tabs__tab"}
              id={`output-tab-${domId}`}
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
        aria-labelledby={`output-tab-${tabDomId(activeTab)}`}
        className="output-tabs__panel"
        id={`output-panel-${tabDomId(activeTab)}`}
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
