import { useRef, useState } from "react";

import type { FileEntry } from "./file-api";

export interface FileTreeProps {
  readonly confirmDelete: (entry: FileEntry) => boolean;
  readonly entries: readonly FileEntry[];
  readonly initialExpanded?: readonly string[];
  readonly onDelete: (entry: FileEntry) => void;
  readonly onExpandedChange?: (paths: readonly string[]) => void;
  readonly onOpen: (entry: FileEntry) => void;
  readonly onRename: (entry: FileEntry) => void;
  readonly onSelect?: (entry: FileEntry) => void;
  readonly projectId: string;
}

function depth(path: string): number {
  return path.split("/").length;
}

export function FileTree({
  confirmDelete,
  entries,
  initialExpanded = [],
  onDelete,
  onExpandedChange,
  onOpen,
  onRename,
  onSelect,
}: FileTreeProps) {
  const [expanded, setExpanded] = useState(() => new Set(initialExpanded));
  const itemRefs = useRef(new Map<string, HTMLDivElement>());

  function isVisible(entry: FileEntry): boolean {
    const segments = entry.path.split("/");
    return segments.slice(0, -1).every((_, index) =>
      expanded.has(segments.slice(0, index + 1).join("/")),
    );
  }

  const visibleEntries = entries.filter(isVisible);

  function setDirectoryExpanded(path: string, nextExpanded: boolean) {
    setExpanded((current) => {
      const next = new Set(current);
      if (nextExpanded) next.add(path);
      else next.delete(path);
      onExpandedChange?.([...next]);
      return next;
    });
  }

  function focusPath(path: string | undefined) {
    if (path) itemRefs.current.get(path)?.focus();
  }

  return (
    <div aria-label="项目文件" role="tree">
      {visibleEntries.map((entry, index) => {
        const isDirectory = entry.kind === "directory";
        const isExpanded = expanded.has(entry.path);
        return (
          <div
            aria-expanded={isDirectory ? isExpanded : undefined}
            aria-level={depth(entry.path)}
            key={entry.path}
            onClick={() => {
              onSelect?.(entry);
              if (isDirectory) {
                setDirectoryExpanded(entry.path, !isExpanded);
              } else {
                onOpen(entry);
              }
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                if (isDirectory) setDirectoryExpanded(entry.path, !isExpanded);
                else onOpen(entry);
              } else if (event.key === "F2") {
                event.preventDefault();
                onRename(entry);
              } else if (event.key === "Delete" && confirmDelete(entry)) {
                event.preventDefault();
                onDelete(entry);
              } else if (event.key === "ArrowDown") {
                event.preventDefault();
                focusPath(visibleEntries[index + 1]?.path);
              } else if (event.key === "ArrowUp") {
                event.preventDefault();
                focusPath(visibleEntries[index - 1]?.path);
              } else if (event.key === "ArrowRight" && isDirectory) {
                event.preventDefault();
                if (!isExpanded) setDirectoryExpanded(entry.path, true);
                else focusPath(visibleEntries[index + 1]?.path);
              } else if (event.key === "ArrowLeft") {
                event.preventDefault();
                if (isDirectory && isExpanded) setDirectoryExpanded(entry.path, false);
                else focusPath(entry.path.split("/").slice(0, -1).join("/"));
              } else if (event.key === "Home") {
                event.preventDefault();
                focusPath(visibleEntries[0]?.path);
              } else if (event.key === "End") {
                event.preventDefault();
                focusPath(visibleEntries.at(-1)?.path);
              }
            }}
            ref={(node) => {
              if (node) itemRefs.current.set(entry.path, node);
              else itemRefs.current.delete(entry.path);
            }}
            role="treeitem"
            tabIndex={0}
          >
            <span aria-hidden="true">{isDirectory ? (isExpanded ? "▾" : "▸") : "·"}</span>{" "}
            {entry.name}
          </div>
        );
      })}
    </div>
  );
}
