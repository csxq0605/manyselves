import type { components } from "../../api/generated/schema";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type Preview = components["schemas"]["MarkdownPreview"];

export function MarkdownPreview({ preview }: { readonly preview: Preview }) {
  return (
    <section aria-label={`Markdown 预览 ${preview.path}`} className="markdown-preview">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{preview.content}</ReactMarkdown>
      {preview.truncated ? <p role="status">内容已截断</p> : null}
    </section>
  );
}
