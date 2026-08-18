import { useEffect, useRef } from "react";

export type UploadConflictResolution = "replace" | "keep-both" | "cancel";

export interface UploadConflictDialogProps {
  readonly fileName: string;
  readonly onResolve: (resolution: UploadConflictResolution) => void;
}

export function UploadConflictDialog({ fileName, onResolve }: UploadConflictDialogProps) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const replaceRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    replaceRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onResolve("cancel");
        return;
      }
      if (event.key !== "Tab") return;
      const buttons = Array.from(
        dialogRef.current?.querySelectorAll<HTMLButtonElement>("button:not([disabled])") ?? [],
      );
      const first = buttons[0];
      const last = buttons.at(-1);
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    const keepFocusInside = (event: FocusEvent) => {
      const dialog = dialogRef.current;
      if (dialog && event.target instanceof Node && !dialog.contains(event.target)) {
        replaceRef.current?.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("focusin", keepFocusInside);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("focusin", keepFocusInside);
    };
  }, [onResolve]);

  return (
    <div aria-labelledby="upload-conflict-title" aria-modal="true" className="upload-conflict" ref={dialogRef} role="dialog">
      <div className="upload-conflict__card">
        <h2 id="upload-conflict-title">文件已存在</h2>
        <p>“{fileName}”已在此目录中。请选择如何继续。</p>
        <div className="upload-conflict__actions">
          <button onClick={() => onResolve("replace")} ref={replaceRef} type="button">替换</button>
          <button onClick={() => onResolve("keep-both")} type="button">保留两份</button>
          <button onClick={() => onResolve("cancel")} type="button">取消</button>
        </div>
      </div>
    </div>
  );
}
