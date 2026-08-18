import type { components } from "../../api/generated/schema";

type Preview = components["schemas"]["DocxPreview"];

export function DocxPreview({ preview }: { readonly preview: Preview }) {
  return (
    <article aria-label={`Word 预览 ${preview.path}`}>
      {preview.blocks.map((block, index) => {
        if (block.kind === "paragraph") return <p key={index}>{block.text}</p>;
        if (block.kind === "image") return <img alt={`文档图片 ${index + 1}`} key={index} src={block.dataUrl} />;
        return <table key={index}><tbody>{block.rows.map((row, rowIndex) => (
          <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>
        ))}</tbody></table>;
      })}
      {preview.truncated ? <p role="status">文档预览已截断</p> : null}
    </article>
  );
}
