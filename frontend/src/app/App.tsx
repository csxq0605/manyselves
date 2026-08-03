import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { EventStream, type EventStreamOptions, type RuntimeEvent } from "../api/event-stream";
import { ApiError, type ApiGateway } from "../api/gateway";
import { createAgentStore } from "../features/agents/agent-store";
import { createReportingStore, type ReportingStore } from "../features/reporting/reporting-store";
import { AppShell } from "../features/shell/AppShell";
import type { PlatformBridge } from "../platform/types";
import { useConnectionStore } from "./store-context";

interface EventStreamController {
  start(): Promise<void>;
  stop(): void;
}

export interface AppProps {
  readonly createEventStream?: (options: EventStreamOptions) => EventStreamController;
  readonly eventSource?: Pick<EventStreamOptions, "baseUrl" | "fetch" | "getToken">;
  readonly gateway: ApiGateway;
  readonly platform?: PlatformBridge;
  readonly reportingStore?: ReportingStore;
}

const knownEventPrefixes = [
  "agent.",
  "checkpoint.",
  "conversation.",
  "debug.",
  "file.",
  "operation.",
  "project.",
  "queue.",
  "report.",
  "reporting.",
  "runtime.",
  "system.",
  "task.",
  "tool.",
  "user.",
] as const;

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
  if (prefix === "report.") {
    return ["reporting"];
  }
  if (["checkpoint.", "debug.", "queue.", "runtime.", "system.", "task.", "tool."].includes(prefix)) {
    return ["runtime"];
  }
  return [prefix.slice(0, -1)];
}

export function App({
  createEventStream = (options) => new EventStream(options),
  eventSource,
  gateway,
  platform,
  reportingStore,
}: AppProps) {
  const queryClient = useQueryClient();
  const setConnectionState = useConnectionStore((store) => store.setState);
  const streamId = useRef<string | null>(null);
  const unknownEventTypes = useRef(new Set<string>());
  const [agentStore] = useState(() => createAgentStore());
  const [fallbackReportingStore] = useState(() => createReportingStore());
  const reports = reportingStore ?? fallbackReportingStore;
  const resolvedEventSource = useMemo(
    () =>
      eventSource ?? {
        baseUrl: window.location.origin,
        fetch: window.fetch.bind(window),
        getToken: () => window.sessionStorage.getItem("manyselves.deploymentToken"),
      },
    [eventSource],
  );
  const bootstrap = useQuery({
    queryFn: async () => {
      const snapshot = await gateway.bootstrap();
      agentStore.getState().hydrate(snapshot.runtime, snapshot.streamId);
      reports.getState().resetStream(snapshot.streamId);
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
    setConnectionState(
      bootstrap.error instanceof ApiError && bootstrap.error.status === 401
        ? "unauthorized"
        : "offline",
    );
  }, [bootstrap.error, setConnectionState]);

  useEffect(() => {
    if (!bootstrapStreamId) {
      return;
    }
    streamId.current = bootstrapStreamId;
    const stream = createEventStream({
      baseUrl: resolvedEventSource.baseUrl,
      expectedStreamId: () => streamId.current,
      fetch: resolvedEventSource.fetch,
      getToken: resolvedEventSource.getToken,
      onEvent: (event) => {
        const wasRefreshRequested = agentStore.getState().refreshRequested;
        const wasReportingRefreshRequested = reports.getState().refreshRequested;
        agentStore.getState().applyEvent(event);
        reports.getState().applyEvent(event);
        if (
          (!wasRefreshRequested && agentStore.getState().refreshRequested)
          || (!wasReportingRefreshRequested && reports.getState().refreshRequested)
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
        await queryClient.invalidateQueries({
          queryKey: ["bootstrap"],
          refetchType: "active",
        });
      },
      onStateChange: setConnectionState,
    });
    void stream.start();
    return () => stream.stop();
  }, [
    agentStore,
    bootstrapStreamId,
    createEventStream,
    gateway,
    queryClient,
    reports,
    resolvedEventSource,
    setConnectionState,
  ]);

  return (
    <AppShell
      agentStore={agentStore}
      bootstrap={bootstrap.data}
      gateway={gateway}
      key={bootstrap.data?.project.id ?? "waiting-for-bootstrap"}
      {...(platform ? { platform } : {})}
      reportingStore={reports}
    />
  );
}
