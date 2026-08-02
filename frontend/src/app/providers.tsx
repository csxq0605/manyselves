import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type PropsWithChildren, useState } from "react";

import {
  createConnectionStore,
  type ConnectionState,
} from "../store/connection-store";
import {
  createWorkspaceStore,
} from "../store/workspace-store";
import { ConnectionStoreContext, WorkspaceStoreContext } from "./store-context";

export interface AppProvidersProps extends PropsWithChildren {
  readonly initialConnectionState?: ConnectionState;
  readonly initialDrafts?: Readonly<Record<string, string>>;
  readonly queryClient?: QueryClient;
}

export function AppProviders({
  children,
  initialConnectionState = "connecting",
  initialDrafts = {},
  queryClient: providedQueryClient,
}: AppProvidersProps) {
  const [connectionStore] = useState(() => createConnectionStore(initialConnectionState));
  const [workspaceStore] = useState(() => createWorkspaceStore(initialDrafts));
  const [queryClient] = useState(
    () =>
      providedQueryClient ??
      new QueryClient({
        defaultOptions: {
          queries: { retry: false, staleTime: 30_000 },
        },
      }),
  );

  return (
    <QueryClientProvider client={queryClient}>
      <ConnectionStoreContext value={connectionStore}>
        <WorkspaceStoreContext value={workspaceStore}>
          {children}
        </WorkspaceStoreContext>
      </ConnectionStoreContext>
    </QueryClientProvider>
  );
}
