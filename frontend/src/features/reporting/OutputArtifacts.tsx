import type { ReportingSnapshotView } from "./reporting-store";

export interface OutputArtifactsProps {
  readonly onDownload: (path: string) => Promise<unknown>;
  readonly outputsHref: string;
  readonly snapshot: ReportingSnapshotView;
}

function filename(path: string): string {
  return path.split("/").at(-1) || "report-output";
}

export function OutputArtifacts({ onDownload, outputsHref, snapshot }: OutputArtifactsProps) {
  const verified = snapshot.verification.status === "passed";
  return (
    <section className="reporting-card" aria-label="交付产物">
      <div className="reporting-section-heading">
        <p>DELIVERY OUTPUTS</p>
        <h3>校验与交付产物</h3>
      </div>
      <p className={`reporting-verification reporting-verification--${snapshot.verification.status}`}>
        {snapshot.verification.message}
      </p>
      <a className="reporting-output-link" href={outputsHref}>在项目“输出”中预览、下载或删除</a>
      {snapshot.outputs.length === 0 ? <p className="reporting-empty">尚无交付文件。</p> : null}
      <ul className="reporting-output-list">
        {snapshot.outputs.map((output) => (
          <li key={output.path}>
            <span><strong>{filename(output.path)}</strong><small>{output.path}</small></span>
            <span>{output.size.toLocaleString()} B</span>
            {verified && output.exists ? (
              <button onClick={() => void onDownload(output.path)} type="button">
                下载 {filename(output.path)}
              </button>
            ) : <span className="reporting-output-list__blocked">不可下载</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}
