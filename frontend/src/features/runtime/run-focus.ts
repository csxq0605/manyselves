import type { WorkflowRunResponse } from "../runs/workflow-api";

export function isActiveRun(run: WorkflowRunResponse): boolean {
  return run.run.active || run.run.status === "running" || run.run.status === "waiting";
}

/** The Run the progress panel follows: the running one, else the newest. */
export function focusRun(runs: readonly WorkflowRunResponse[]): WorkflowRunResponse | undefined {
  return runs.find(isActiveRun) ?? runs[0];
}
