import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import type { ApiGateway } from "../../api/gateway";
import { createProjectApi } from "../projects/project-api";
import { createConversationApi } from "./conversation-api";
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
  const activation = useQuery({
    enabled: Boolean(projectId && projects.data && routeProject && !routeProject.active),
    queryFn: async () => {
      const activated = await projectApi.activate(projectId!);
      queryClient.setQueryData<Awaited<ReturnType<typeof projectApi.list>>>(["projects"], (current) => (
        current?.map((project) => ({ ...project, active: project.id === activated.id }))
      ));
      // 清空所有会话相关的缓存，避免项目切换后显示旧项目的会话
      queryClient.removeQueries({ queryKey: ["conversations"] });
      queryClient.removeQueries({ queryKey: ["conversation-messages"] });
      // 清空 localStorage 中保存的活跃会话 ID，避免尝试激活其他项目的会话
      const savedState = localStorage.getItem("manyselves-active-conversation");
      if (savedState) {
        try {
          const parsed = JSON.parse(savedState);
          if (parsed.state?.activeSessionIds) {
            // 只保留当前项目的活跃会话 ID
            const currentProjectSessionId = parsed.state.activeSessionIds[projectId!];
            parsed.state.activeSessionIds = currentProjectSessionId
              ? { [projectId!]: currentProjectSessionId }
              : {};
            localStorage.setItem("manyselves-active-conversation", JSON.stringify(parsed));
          }
        } catch {
          // 解析失败时，清空整个状态
          localStorage.removeItem("manyselves-active-conversation");
        }
      }
      return activated;
    },
    queryKey: ["project-activation", projectId, routeProject?.revision],
    retry: false,
  });
  const projectReady = Boolean(routeProject?.active || activation.isSuccess);

  useEffect(() => {
    if (!projectId || conversationId !== "new" || !projectReady) return;
    if (!createRef.current || createRef.current.projectId !== projectId) {
      createRef.current = { projectId, promise: api.create(projectId, "新会话", "main") };
    }
    let cancelled = false;
    void createRef.current.promise.then((created) => {
      if (!cancelled) {
        // 先刷新会话列表缓存，确保新会话可见
        queryClient.invalidateQueries({ queryKey: ["conversations", projectId, "main"] });
        navigate(`/projects/${encodeURIComponent(projectId)}/conversations/${encodeURIComponent(created.sessionId)}`, {
          replace: true,
        });
      }
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
      onSessionChanged={(sessionId) => navigate(
        `/projects/${encodeURIComponent(projectId)}/conversations/${encodeURIComponent(sessionId)}`,
      )}
      projectId={projectId}
      projectName={projects.data?.find((project) => project.id === projectId)?.displayName ?? projectId}
      requestedSessionId={conversationId}
    />
  );
}
