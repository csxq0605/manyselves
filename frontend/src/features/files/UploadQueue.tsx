export type UploadStatus = "queued" | "uploading" | "completed" | "failed" | "cancelled";

export interface UploadTask {
  readonly id: string;
  readonly name: string;
  readonly status: UploadStatus;
}

export interface UploadQueueProps {
  readonly onCancel: (id: string) => void;
  readonly onRetry: (id: string) => void;
  readonly tasks: readonly UploadTask[];
}

const statusLabels: Record<UploadStatus, string> = {
  cancelled: "已取消",
  completed: "已完成",
  failed: "失败",
  queued: "等待上传",
  uploading: "上传中",
};

export function UploadQueue({ onCancel, onRetry, tasks }: UploadQueueProps) {
  if (tasks.length === 0) return null;
  return (
    <ul aria-label="上传队列" className="upload-queue">
      {tasks.map((task) => (
        <li key={task.id}>
          <span>{task.name}</span>
          <span aria-live="polite">{statusLabels[task.status]}</span>
          {task.status === "uploading" ? (
            <progress aria-label={`正在上传 ${task.name}`} />
          ) : null}
          {task.status === "failed" ? (
            <button aria-label={`重试 ${task.name}`} onClick={() => onRetry(task.id)} type="button">重试</button>
          ) : null}
          {task.status === "queued" || task.status === "uploading" ? (
            <button aria-label={`取消 ${task.name}`} onClick={() => onCancel(task.id)} type="button">取消</button>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
