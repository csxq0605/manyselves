import { z } from "zod";

export const channels = {
  notify: "manyselves:notify",
  openDownloadedFile: "manyselves:open-downloaded-file",
  saveDownload: "manyselves:save-download",
  selectDirectory: "manyselves:select-directory",
  selectFiles: "manyselves:select-files",
  shortcut: "manyselves:shortcut",
  tokenDelete: "manyselves:token-delete",
  tokenGet: "manyselves:token-get",
  tokenSet: "manyselves:token-set",
} as const;

export const notificationSchema = z.object({
  body: z.string().max(256).optional(),
  title: z.string().min(1).max(128),
}).strict();
export const downloadedPathSchema = z.string().min(1).max(4096);
export const saveDownloadSchema = z.object({
  bytes: z.instanceof(Uint8Array),
  suggestedName: z.string().min(1).max(255),
}).strict();
export const tokenSchema = z.string().min(1).max(16_384);

export interface NativeFileRef {
  readonly bytes: Uint8Array;
  readonly name: string;
  readonly relativePath?: string;
  readonly size: number;
  readonly type: string;
}

export interface NativeDirectoryRef {
  readonly files: readonly NativeFileRef[];
  readonly name: string;
  readonly skipped: readonly string[];
}

export interface DesktopPreloadApi {
  notify(input: z.infer<typeof notificationSchema>): Promise<void>;
  openDownloadedFile(path: string): Promise<void>;
  saveDownload(input: z.infer<typeof saveDownloadSchema>): Promise<string | null>;
  selectDirectory(): Promise<NativeDirectoryRef | null>;
  selectFiles(): Promise<readonly NativeFileRef[]>;
  secureToken: {
    delete(): Promise<void>;
    get(): Promise<string | null>;
    set(token: string): Promise<void>;
  };
}
