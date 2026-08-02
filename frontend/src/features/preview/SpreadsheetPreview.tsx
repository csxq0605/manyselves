import type { components } from "../../api/generated/schema";
import { useState } from "react";

type Preview = components["schemas"]["SpreadsheetPreview"];

export function SpreadsheetPreview({ preview }: { readonly preview: Preview }) {
  const [sheetIndex, setSheetIndex] = useState(0);
  const sheet = preview.sheets[sheetIndex];
  if (!sheet) return <p>工作簿中没有可显示的工作表</p>;
  return (
    <section aria-label={`表格预览 ${preview.path}`}>
      <div aria-label="工作表" role="tablist">
        {preview.sheets.map((item, index) => (
          <button aria-selected={index === sheetIndex} key={item.name}
            onClick={() => setSheetIndex(index)} role="tab" type="button">{item.name}</button>
        ))}
      </div>
      <div className="spreadsheet-scroll">
        <table>
          <tbody>{sheet.rows.map((row, rowIndex) => (
            <tr key={rowIndex}>{row.map((cell, columnIndex) => (
              <td key={columnIndex}>{cell}</td>
            ))}</tr>
          ))}</tbody>
        </table>
      </div>
      {sheet.truncated || preview.sheetsTruncated ? <p role="status">预览已截断</p> : null}
      <p>{sheet.rowCount} 行 × {sheet.columnCount} 列</p>
    </section>
  );
}
