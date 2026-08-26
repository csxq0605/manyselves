import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import type { ApiGateway } from "../../api/gateway";
import { createProjectApi } from "../projects/project-api";
import { useProjectActivation } from "../projects/use-project-activation";
import { createConversationApi } from "./conversation-api";
import { cacheCreatedConversation } from "./conversation-cache";
import "./conversation.css";
import { ConversationWorkspace } from "./ConversationWorkspace";

export function ConversationPage({ gateway }: { readonly gateway: ApiGateway }) {
  const { conversationId, projectId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const api = useMemo(() => createConversationApi(gateway), [gateway]);
  const projectApi = useMemo(() => createProjectApi(gateway), [gateway]);
  const projects = useQuery({ queryFn: () => projectApi.list(), queryKey: ["projects"] });
  const createRef = useRef<{ readonly projectId: string; readonly promise: ReturnType<typeof api.create> } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const routeProject = projects.data?.find((project) => project.id === projectId);
  const activation = useProjectActivation(projectId, routeProject, projectApi);
  const projectReady = activation.isReady;

  useEffect(() => {
    if (!projectId || conversationId !== "new" || !projectReady) return;
    if (!createRef.current || createRef.current.projectId !== projectId) {
      createRef.current = { projectId, promise: api.create(projectId, "新会话", "main") };
    }
    let cancelled = false;
    void createRef.current.promise.then(async (created) => {
      if (cancelled) return;
      await cacheCreatedConversation(queryClient, projectId, "main", created);
      if (!cancelled) navigate(`/projects/${encodeURIComponent(projectId)}/conversations/${encodeURIComponent(created.sessionId)}`, {
        replace: true,
      });
    }).catch(() => {
      if (!cancelled) setError("新建会话失败");
    });
    return () => { cancelled = true; };
  }, [api, conversationId, navigate, projectId, projectReady, queryClient]);

  if (!projectId || !conversationId) return <p role="alert">会话路由无效</p>;
  if (projects.isPending) return <p role="status">正在加载项目…</p>;
  if (projects.isError || !routeProject) return <p role="alert">项目不可用</p>;
  if (!projectReady) return activation.isError ? <p role="alert">项目切换失败</p> : <p role="status">正在切换项目…</p>;
  if (conversationId === "new") {
    return error ? <p role="alert">{error}</p> : <p role="status">正在新建会话…</p>;
  }
  return (
    <ConversationWorkspace
      agentId="main"
      gateway={gateway}
      onRequestedSessionMissing={() => navigate(`/projects/${encodeURIComponent(projectId)}`, { replace: true })}
      onSessionChanged={(sessionId) => navigate(
        `/projects/${encodeURIComponent(projectId)}/conversations/${encodeURIComponent(sessionId)}`,
      )}
      projectId={projectId}
      projectName={projects.data?.find((project) => project.id === projectId)?.displayName ?? projectId}
      requestedSessionId={conversationId}
    />
  );
}
