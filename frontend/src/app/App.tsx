import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef } from "react";

import { EventStream, type EventStreamOptions, type RuntimeEvent } from "../api/event-stream";
import { ApiError, type ApiGateway } from "../api/gateway";
import { AppShell } from "../features/shell/AppShell";
import { useConnectionStore } from "./store-context";

interface EventStreamController {
  start(): Promise<void>;
  stop(): void;
}

export interface AppProps {
  readonly createEventStream?: (options: EventStreamOptions) => EventStreamController;
  readonly eventSource?: Pick<EventStreamOptions, "baseUrl" | "fetch" | "getToken">;
  readonly gateway: ApiGateway;
}

const knownEventPrefixes = [
  "agent.",
  "conversation.",
  "file.",
  "operation.",
  "project.",
  "reporting.",
  "runtime.",
  "system.",
] as const;

function eventQueryKey(event: RuntimeEvent): readonly string[] | null {
  const prefix = knownEventPrefixes.find((candidate) => event.type.startsWith(candidate));
  if (!prefix) {
    return null;
  }
  return [prefix.slice(0, -1)];
}

export function App({
  createEventStream = (options) => new EventStream(options),
  eventSource,
  gateway,
}: AppProps) {
  const queryClient = useQueryClient();
  const setConnectionState = useConnectionStore((store) => store.setState);
  const streamId = useRef<string | null>(null);
  const unknownEventTypes = useRef(new Set<string>());
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
        const queryKey = eventQueryKey(event);
        if (queryKey) {
          void queryClient.invalidateQueries({ queryKey });
          void queryClient.invalidateQueries({ queryKey: ["bootstrap"] });
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
    bootstrapStreamId,
    createEventStream,
    gateway,
    queryClient,
    resolvedEventSource,
    setConnectionState,
  ]);

  return <AppShell bootstrap={bootstrap.data} />;
}
