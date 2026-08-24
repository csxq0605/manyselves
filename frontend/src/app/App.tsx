import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { EventStream, type EventStreamOptions, type RuntimeEvent } from "../api/event-stream";
import { ApiError, type ApiGateway } from "../api/gateway";
import { createAgentStore } from "../features/agents/agent-store";
import { AppRoutes } from "./routes";
import type { PlatformBridge } from "../platform/types";
import type { SettingsStorage } from "../features/settings/settings-storage";
import { useConnectionStore } from "./store-context";

interface EventStreamController {
  start(): Promise<void>;
  stop(): void;
}

export interface AppProps {
  readonly accountUsername?: string;
  readonly createEventStream?: (options: EventStreamOptions) => EventStreamController;
  readonly eventSource?: Pick<EventStreamOptions, "baseUrl" | "fetch">;
  readonly gateway: ApiGateway;
  readonly onUnauthorized?: () => void;
  readonly onLogout?: () => void;
  readonly platform?: PlatformBridge;
  readonly settingsStorage?: SettingsStorage;
}

const knownEventPrefixes = [
  "action.",
  "agent.",
  "checkpoint.",
  "conversation.",
  "debug.",
  "file.",
  "operation.",
  "output.",
  "project.",
  "queue.",
  "report.",
  "reporting.",
  "runtime.",
  "system.",
  "task.",
  "tool.",
  "user.",
  "workflow.",
] as const;

function eventRunId(event: RuntimeEvent): string | null {
  const runId = event.payload.run_id;
  return typeof runId === "string" && runId ? runId : null;
}

function eventQueryKey(event: RuntimeEvent): readonly string[] | null {
  const prefix = knownEventPrefixes.find((candidate) => event.type.startsWith(candidate));
  if (!prefix) {
    return null;
  }
  if (prefix === "file.") {
    return ["files"];
  }
  if (prefix === "agent.") {
    return event.type.startsWith("agent.message.") ? ["conversation-messages"] : ["agents"];
  }
  if (prefix === "conversation." || prefix === "user.") {
    return ["conversation-messages"];
  }
  if (["action.", "output.", "report.", "reporting.", "workflow."].includes(prefix)) {
    const runId = eventRunId(event);
    return runId ? ["runs", runId] : ["runs"];
  }
  if (["checkpoint.", "debug.", "queue.", "runtime.", "system.", "task.", "tool."].includes(prefix)) {
    return ["runtime"];
  }
  return [prefix.slice(0, -1)];
}

export function App({
  accountUsername,
  createEventStream = (options) => new EventStream(options),
  eventSource,
  gateway,
  onUnauthorized,
  onLogout,
  platform,
  settingsStorage,
}: AppProps) {
  const queryClient = useQueryClient();
  const setConnectionState = useConnectionStore((store) => store.setState);
  const streamId = useRef<string | null>(null);
  const unknownEventTypes = useRef(new Set<string>());
  const [agentStore] = useState(() => createAgentStore());
  const [startedStreamId, setStartedStreamId] = useState<string | null>(null);
  const resolvedEventSource = useMemo(
    () =>
      eventSource ?? {
        baseUrl: window.location.origin,
        fetch: window.fetch.bind(window),
      },
    [eventSource],
  );
  const bootstrap = useQuery({
    queryFn: async () => {
      const snapshot = await gateway.bootstrap();
      agentStore.getState().hydrate(snapshot.runtime, snapshot.streamId);
      queryClient.setQueryData(["project"], snapshot.project);
      queryClient.setQueryData(["runtime"], snapshot.runtime);
      queryClient.setQueryData(["conversations"], snapshot.conversations);
      queryClient.setQueryData(["agents"], snapshot.agents);
      queryClient.setQueryData(["settings"], snapshot.settings);
      queryClient.setQueryData(["maintenance"], snapshot.maintenance);
      return snapshot;
    },
    queryKey: ["bootstrap"],
  });
  const bootstrapStreamId = bootstrap.data?.streamId;

  useEffect(() => {
    if (!bootstrap.error) {
      return;
    }
    if (bootstrap.error instanceof ApiError && bootstrap.error.status === 401) {
      onUnauthorized?.();
      return;
    }
    setConnectionState("offline");
  }, [bootstrap.error, onUnauthorized, setConnectionState]);

  useEffect(() => {
    if (!bootstrapStreamId) {
      return;
    }
    streamId.current = bootstrapStreamId;
    const stream = createEventStream({
      baseUrl: resolvedEventSource.baseUrl,
      expectedStreamId: () => streamId.current,
      fetch: resolvedEventSource.fetch,
      onEvent: (event) => {
        const wasRefreshRequested = agentStore.getState().refreshRequested;
        agentStore.getState().applyEvent(event);
        if (event.projectId) {
          void queryClient.invalidateQueries({ queryKey: ["event-logs", event.projectId] });
        }
        if (
          !wasRefreshRequested && agentStore.getState().refreshRequested
        ) {
          setConnectionState("resyncing");
          void queryClient.invalidateQueries({
            queryKey: ["bootstrap"],
            refetchType: "active",
          });
          return;
        }
        const queryKey = eventQueryKey(event);
        if (queryKey) {
          void queryClient.invalidateQueries({ queryKey });
          return;
        }
        if (!unknownEventTypes.current.has(event.type)) {
          unknownEventTypes.current.add(event.type);
          console.warn(`Ignoring unknown runtime event type: ${event.type}`);
        }
      },
      onResync: async () => {
        setConnectionState("resyncing");
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: ["bootstrap"], refetchType: "active" }),
          queryClient.invalidateQueries({ queryKey: ["event-logs"], refetchType: "active" }),
        ]);
      },
      onStateChange: (state) => {
        if (state === "unauthorized") {
          onUnauthorized?.();
          return;
        }
        setConnectionState(state);
      },
    });
    let stopped = false;
    void stream.start();
    queueMicrotask(() => {
      if (!stopped) setStartedStreamId(bootstrapStreamId);
    });
    return () => {
      stopped = true;
      stream.stop();
    };
  }, [
    agentStore,
    bootstrapStreamId,
    createEventStream,
    gateway,
    queryClient,
    resolvedEventSource,
    onUnauthorized,
    setConnectionState,
  ]);

  if (!bootstrap.data || startedStreamId !== bootstrapStreamId) {
    return (
      <main aria-busy={bootstrap.isPending} className="app-loading">
        {bootstrap.isError ? <p role="alert">Unable to load the application.</p> : null}
      </main>
    );
  }

  return <AppRoutes gateway={gateway} {...(accountUsername ? { accountUsername } : {})} {...(onLogout ? { onLogout } : {})} {...(platform ? { platform } : {})} {...(settingsStorage ? { settingsStorage } : {})} />;
}
