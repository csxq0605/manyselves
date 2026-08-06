export type ProjectFileSection = "inputs" | "knowledge" | "templates" | "outputs";

export interface SectionCapabilities {
  readonly delete: boolean;
  readonly download: boolean;
  readonly edit: boolean;
  readonly label: string;
  readonly preview: boolean;
  readonly root: string;
  readonly upload: boolean;
}

export const SECTION_CAPABILITIES = {
  inputs: {
    delete: true, download: true, edit: true, label: "输入", preview: true, root: "Inputs", upload: true,
  },
  knowledge: {
    delete: true, download: true, edit: true, label: "知识库", preview: true, root: "Knowledge", upload: true,
  },
  outputs: {
    delete: true, download: true, edit: false, label: "输出", preview: true, root: "Outputs", upload: false,
  },
  templates: {
    delete: true, download: true, edit: true, label: "输出模板", preview: true, root: "Templates", upload: true,
  },
} as const satisfies Record<ProjectFileSection, SectionCapabilities>;

export function isProjectFileSection(value: string | undefined): value is ProjectFileSection {
  return value !== undefined && Object.hasOwn(SECTION_CAPABILITIES, value);
}

// Check if section is a file section (not history, runtime, logs)
export function isFileBasedSection(section: string | undefined): boolean {
  return isProjectFileSection(section);
}
