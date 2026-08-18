import { useEffect } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { useConversationStore } from "../../store/conversation-store";

export function ProjectHomePage() {
  const navigate = useNavigate();
  const { projectId = "" } = useParams();
  const getActiveSession = useConversationStore((state) => state.getActiveSession);

  // Redirect to active conversation if one exists
  useEffect(() => {
    const activeSessionId = getActiveSession(projectId);
    if (activeSessionId && activeSessionId !== "new") {
      navigate(`/projects/${encodeURIComponent(projectId)}/conversations/${activeSessionId}`, { replace: true });
    }
  }, [projectId, getActiveSession, navigate]);

  return <section className="project-home"><p className="project-home__eyebrow">PROJECT / {projectId}</p><h1>最近对话</h1><p>项目对话会显示在这里，不会混入项目目录。</p><button onClick={() => navigate(`/projects/${encodeURIComponent(projectId)}/conversations/new`)} type="button">新对话</button></section>;
}
