export interface SaveConflictDialogProps {
  readonly onKeepDraft: () => void;
  readonly onOverwrite: () => void;
  readonly onReload: () => void;
  readonly path: string;
}

export function SaveConflictDialog({
  onKeepDraft,
  onOverwrite,
  onReload,
  path,
}: SaveConflictDialogProps) {
  return (
    <div aria-labelledby="save-conflict-title" aria-modal="true" role="dialog">
      <h3 id="save-conflict-title">服务器文件已更新</h3>
      <p>{path} 的服务器版本与当前草稿基线不一致。草稿尚未被覆盖。</p>
      <button onClick={onReload} type="button">重新加载服务器版本</button>
      <button onClick={onKeepDraft} type="button">保留草稿</button>
      <button onClick={onOverwrite} type="button">覆盖服务器版本</button>
    </div>
  );
}
