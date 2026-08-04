import { useEffect, useRef, useState } from "react";
import { useStore } from "zustand";

import { ApiError } from "../../api/gateway";
import { createUuid } from "../../app/uuid";
import type { ConversationApi } from "../conversations/conversation-api";
import type { FileApi } from "../files/file-api";
import { CommandPalette } from "./CommandPalette";
import { isSupportedCommand, supportedCommands, type SupportedCommand } from "./commands";
import type { ConversationMessageStore } from "./message-store";

interface UploadedAttachment {
  readonly name: string;
  readonly path: string;
  readonly size: number;
}

interface ContextAttempt {
  readonly attachment: UploadedAttachment;
  readonly idempotencyKey: string;
}

interface SendAttempt {
  readonly content: string;
  readonly contexts: readonly ContextAttempt[];
  readonly idempotencyKey: string;
  readonly messageId: string;
}

export interface MessageComposerProps {
  readonly agentId: string;
  readonly api: ConversationApi;
  readonly fileApi: FileApi;
  readonly onHistoryRequested?: (() => void) | undefined;
  readonly onSessionChanged?: ((sessionId: string) => void) | undefined;
  readonly projectId: string;
  readonly sessionId: string;
  readonly store: ConversationMessageStore;
}

function newId(): string {
  return createUuid();
}

function safeFileName(name: string): string {
  return name.split(/[\\/]/).at(-1) || "upload";
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  return `${(size / 1024).toFixed(1)} KB`;
}

function uploadErrorMessage(error: unknown): string {
  if (error instanceof ApiError && (error.code === "FILE_ALREADY_EXISTS" || error.status === 409)) {
    return "同名文件已存在，请在输入文件页处理后重试";
  }
  return "文件上传失败，请重试";
}

export function MessageComposer(props: MessageComposerProps) {
  return <ScopedMessageComposer key={`${props.projectId}:${props.sessionId}`} {...props} />;
}

function ScopedMessageComposer({
  agentId,
  api,
  fileApi,
  onHistoryRequested,
  onSessionChanged,
  projectId,
  sessionId,
  store,
}: MessageComposerProps) {
  const scope = `${projectId}:${sessionId}`;
  const draft = useStore(store, (state) => state.drafts[scope] ?? "");
  const [attachments, setAttachments] = useState<readonly UploadedAttachment[]>([]);
  const [failedAttempt, setFailedAttempt] = useState<SendAttempt | null>(null);
  const [pending, setPending] = useState(false);
  const [uploadingCount, setUploadingCount] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pendingRef = useRef(false);
  const uploadControllersRef = useRef(new Set<AbortController>());
  const mountedRef = useRef(true);
  const scopeRef = useRef(scope);

  useEffect(() => {
    mountedRef.current = true;
    const controllers = uploadControllersRef.current;
    return () => {
      mountedRef.current = false;
      controllers.forEach((controller) => controller.abort());
      controllers.clear();
    };
  }, []);

  function beginPending(): boolean {
    if (pendingRef.current) return false;
    pendingRef.current = true;
    setPending(true);
    return true;
  }

  function endPending() {
    pendingRef.current = false;
    setPending(false);
  }

  async function uploadFile(file: File, uploadScope: string) {
    const controller = new AbortController();
    uploadControllersRef.current.add(controller);
    const name = safeFileName(file.name);
    setFailedAttempt(null);
    setUploadingCount((current) => current + 1);
    try {
      const uploaded = await fileApi.upload(
        projectId,
        `Inputs/${name}`,
        file,
        "reject",
        undefined,
        controller.signal,
      );
      if (scopeRef.current !== uploadScope || controller.signal.aborted) return;
      setAttachments((current) => current.some((item) => item.path === uploaded.path)
        ? current
        : [...current, { name: uploaded.name, path: uploaded.path, size: uploaded.size ?? file.size }]);
    } catch (uploadError) {
      if (controller.signal.aborted || scopeRef.current !== uploadScope) return;
      setError(uploadErrorMessage(uploadError));
    } finally {
      uploadControllersRef.current.delete(controller);
      if (mountedRef.current) setUploadingCount((current) => Math.max(0, current - 1));
    }
  }

  async function runAttempt(attempt: SendAttempt) {
    if (!beginPending()) return;
    setError(null);
    setNotice(null);
    try {
      for (const item of attempt.contexts) {
        await api.sendFileContext(
          projectId,
          agentId,
          { file: item.attachment.path, type: "file" },
          item.idempotencyKey,
        );
      }
      await api.sendMessage(projectId, agentId, attempt.content, attempt.idempotencyKey, attempt.messageId);
      if (store.getState().drafts[scope]?.trim() === attempt.content) {
        store.getState().clearDraft(scope);
      }
      setFailedAttempt(null);
      setAttachments([]);
      setNotice("消息已提交");
      onHistoryRequested?.();
    } catch {
      setFailedAttempt(attempt);
      setError("发送失败，草稿仍保留在本地");
    } finally {
      endPending();
    }
  }

  async function executeCommand(command: SupportedCommand) {
    setError(null);
    setNotice(null);
    if (command === "/new" || command === "/clear") {
      if (command === "/clear" && !window.confirm("确定清空当前会话？")) return;
      if (!beginPending()) return;
      try {
        const activeSessionId = command === "/new"
          ? (await api.create(projectId, "新会话", agentId)).sessionId
          : (await api.clear(projectId, agentId)).activeSessionId;
        onSessionChanged?.(activeSessionId);
        if (command === "/clear") onHistoryRequested?.();
        store.getState().clearDraft(scope);
      } catch {
        setError("命令执行失败");
      } finally {
        endPending();
      }
      return;
    }
    try {
      if (command === "/help") {
        setNotice(supportedCommands.map((item) => item.command).join("  "));
      } else if (command === "/history") {
        onHistoryRequested?.();
      } else if (command === "/retry") {
        if (failedAttempt) await runAttempt(failedAttempt);
        else setNotice("没有可重试的发送");
      } else if (command === "/stop") {
        await stop();
      }
      if (command !== "/retry" || !failedAttempt) store.getState().clearDraft(scope);
    } catch {
      setError("命令执行失败");
    }
  }

  function submit() {
    const content = draft.trim();
    if (!content || pendingRef.current || uploadingCount > 0) return;
    if (isSupportedCommand(content)) {
      void executeCommand(content);
      return;
    }
    const attempt: SendAttempt = {
      content,
      contexts: attachments.map((attachment) => ({ attachment, idempotencyKey: newId() })),
      idempotencyKey: newId(),
      messageId: newId(),
    };
    void runAttempt(attempt);
  }

  async function stop() {
    if (!beginPending()) return;
    setError(null);
    try {
      await api.interrupt(agentId, newId());
      setNotice("已请求停止生成");
    } catch {
      setError("停止请求失败");
    } finally {
      endPending();
    }
  }

  return (
    <section aria-label="消息编辑器" className="message-composer">
      <input
        aria-label="选择本地文件"
        hidden
        multiple
        onChange={(event) => {
          setError(null);
          const uploadScope = scopeRef.current;
          for (const file of Array.from(event.target.files ?? [])) void uploadFile(file, uploadScope);
          event.target.value = "";
        }}
        ref={fileInputRef}
        type="file"
      />
      {attachments.length > 0 ? (
        <ul aria-label="待发送附件">
          {attachments.map((attachment) => (
            <li key={attachment.path}>
              <span>{attachment.name}</span> <small>{formatBytes(attachment.size)}</small>
              <button
                aria-label={`移除 ${attachment.name}`}
                disabled={pending}
                onClick={() => {
                  setFailedAttempt(null);
                  setAttachments((current) => current.filter((item) => item.path !== attachment.path));
                }}
                type="button"
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      <label>
        <span>消息</span>
        <textarea
          aria-label="消息"
          disabled={pending}
          onChange={(event) => {
            setFailedAttempt(null);
            store.getState().setDraft(scope, event.target.value);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
          placeholder="输入消息，Enter 发送，Shift+Enter 换行"
          value={draft}
        />
      </label>
      <CommandPalette
        onSelect={(command) => store.getState().setDraft(scope, command)}
        query={draft}
      />
      <div className="message-composer__actions">
        <button aria-label="上传本地文件" disabled={pending} onClick={() => fileInputRef.current?.click()} title="上传本地文件" type="button">+</button>
        <span>文件将保存到项目“输入”</span>
        <button aria-label="发送" disabled={!draft.trim() || pending || uploadingCount > 0} onClick={submit} type="button">↑</button>
        {failedAttempt ? (
          <button disabled={pending} onClick={() => void runAttempt(failedAttempt)} type="button">重试发送</button>
        ) : null}
      </div>
      {uploadingCount > 0 ? <p aria-live="polite" role="status">正在上传 {uploadingCount} 个文件…</p> : null}
      {notice ? <p aria-live="polite" role="status">{notice}</p> : null}
      {error ? <p role="alert">{error}</p> : null}
    </section>
  );
}
