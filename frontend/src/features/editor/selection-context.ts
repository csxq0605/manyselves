import type { components } from "../../api/generated/schema";

type FileContextRequest = components["schemas"]["FileContextRequest"];

export interface SelectionInput {
  readonly endLine: number;
  readonly path: string;
  readonly startLine: number;
}

export function buildSelectionContext(input: SelectionInput): FileContextRequest {
  return {
    endLine: input.endLine,
    file: input.path,
    startLine: input.startLine,
    type: "selection",
  };
}
