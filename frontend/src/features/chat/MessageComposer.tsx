import { useCallback, useRef, useState } from "react";
import { useStore } from "zustand";

import { createUuid } from "../../app/uuid";
import type { SelectionInput } from "../editor/selection-context";
import type { ConversationApi } from "../conversations/conversation-api";
import { CommandPalette } from "./CommandPalette";
import { isSupportedCommand, supportedCommands, type SupportedCommand } from "./commands";
import { FileReferencePicker, type FileContext } from "./FileReferencePicker";
import type { ConversationMessageStore } from "./message-store";

interface ContextAttempt {
  readonly context: FileContext;
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
  readonly availableFiles?: readonly string[] | undefined;
  readonly currentEditorPath?: string | null | undefined;
  readonly currentSelection?: SelectionInput | null | undefined;
  readonly onHistoryRequested?: (() => void) | undefined;
  readonly onSessionChanged?: ((sessionId: string) => void) | undefined;
  readonly sessionId: string;
  readonly store: ConversationMessageStore;
}

function newId(): string {
  return createUuid();
}

export function MessageComposer({
  agentId,
  api,
  availableFiles,
  currentEditorPath,
  currentSelection,
  onHistoryRequested,
  onSessionChanged,
  sessionId,
  store,
}: MessageComposerProps) {
  const draft = useStore(store, (state) => state.drafts[sessionId] ?? "");
  const [contexts, setContexts] = useState<readonly FileContext[]>([]);
  const [failedAttempt, setFailedAttempt] = useState<SendAttempt | null>(null);
  const [referenceVersion, setReferenceVersion] = useState(0);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const pendingRef = useRef(false);
  const handleContexts = useCallback((next: readonly FileContext[]) => setContexts(next), []);

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

  async function runAttempt(attempt: SendAttempt) {
    if (!beginPending()) return;
    setError(null);
    setNotice(null);
    try {
      for (const item of attempt.contexts) {
        await api.sendFileContext(agentId, item.context, item.idempotencyKey);
      }
      await api.sendMessage(agentId, attempt.content, attempt.idempotencyKey, attempt.messageId);
      if (store.getState().drafts[sessionId]?.trim() === attempt.content) {
        store.getState().clearDraft(sessionId);
      }
      setFailedAttempt(null);
      setContexts([]);
      setReferenceVersion((version) => version + 1);
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
          ? (await api.create("新会话", agentId)).sessionId
          : (await api.clear(agentId)).activeSessionId;
        onSessionChanged?.(activeSessionId);
        if (command === "/clear") onHistoryRequested?.();
        store.getState().clearDraft(sessionId);
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
      if (command !== "/retry" || !failedAttempt) store.getState().clearDraft(sessionId);
    } catch {
      setError("命令执行失败");
    }
  }

  function submit() {
    const content = draft.trim();
    if (!content || pendingRef.current) return;
    if (isSupportedCommand(content)) {
      void executeCommand(content);
      return;
    }
    const attempt: SendAttempt = {
      content,
      contexts: contexts.map((context) => ({ context, idempotencyKey: newId() })),
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
      <FileReferencePicker
        availableFiles={availableFiles}
        currentEditorPath={currentEditorPath}
        currentSelection={currentSelection}
        key={`${sessionId}:${referenceVersion}`}
        onChange={handleContexts}
        sessionId={sessionId}
      />
      <label>
        <span>消息</span>
        <textarea
          aria-label="消息"
          disabled={pending}
          onChange={(event) => store.getState().setDraft(sessionId, event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
          value={draft}
        />
      </label>
      <CommandPalette
        onSelect={(command) => store.getState().setDraft(sessionId, command)}
        query={draft}
      />
      <div className="message-composer__actions">
        <button disabled={!draft.trim() || pending} onClick={submit} type="button">发送</button>
        <button disabled={pending} onClick={() => void stop()} type="button">停止生成</button>
        {failedAttempt ? (
          <button disabled={pending} onClick={() => void runAttempt(failedAttempt)} type="button">重试发送</button>
        ) : null}
      </div>
      {notice ? <p aria-live="polite" role="status">{notice}</p> : null}
      {error ? <p role="alert">{error}</p> : null}
    </section>
  );
}
