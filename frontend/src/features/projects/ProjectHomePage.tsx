import { useNavigate, useParams } from "react-router-dom";

export function ProjectHomePage() {
  const navigate = useNavigate();
  const { projectId = "" } = useParams();
  return <section className="project-home"><p className="project-home__eyebrow">PROJECT / {projectId}</p><h1>最近对话</h1><p>项目对话会显示在这里，不会混入项目目录。</p><button onClick={() => navigate(`/projects/${projectId}/conversations/new`)} type="button">新对话</button></section>;
}
