import type { components } from "../../api/generated/schema";

type Preview = components["schemas"]["TextPreview"];

export function TextPreview({ preview }: { readonly preview: Preview }) {
  return (
    <section aria-label={`文本预览 ${preview.path}`}>
      <pre>{preview.content}</pre>
      {preview.truncated ? <p role="status">内容已截断</p> : null}
    </section>
  );
}
