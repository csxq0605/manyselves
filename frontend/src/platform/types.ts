export interface LocalFileRef {
  readonly file: File;
  readonly name: string;
  readonly size: number;
  readonly type: string;
}

export interface LocalDirectoryRef {
  readonly name: string;
  readonly files: readonly LocalFileRef[];
}

export interface SaveDownloadInput {
  readonly blob: Blob;
  readonly suggestedName: string;
}

export interface NotificationInput {
  readonly body?: string;
  readonly title: string;
}

export interface PlatformBridge {
  readonly kind: "browser" | "electron";
  selectFiles(): Promise<readonly LocalFileRef[]>;
  selectDirectory(): Promise<LocalDirectoryRef | null>;
  saveDownload(input: SaveDownloadInput): Promise<void>;
  openDownloadedFile(path: string): Promise<void>;
  notify(input: NotificationInput): Promise<void>;
}
