import type {
  LocalDirectoryRef,
  LocalFileRef,
  NotificationInput,
  PlatformBridge,
  SaveDownloadInput,
} from "./types";

export interface BrowserDirectorySelection {
  readonly files: readonly File[];
  readonly name: string;
}

export interface BrowserPlatformDriver {
  notify(input: NotificationInput): Promise<void>;
  openDownloadedFile?(path: string): Promise<void>;
  saveDownload(input: SaveDownloadInput): Promise<void>;
  selectDirectory(): Promise<BrowserDirectorySelection | null>;
  selectFiles(): Promise<readonly File[]>;
}

export interface DomBrowserPlatformOptions {
  readonly createObjectUrl?: (blob: Blob) => string;
  readonly document?: Document;
  readonly notify?: (input: NotificationInput) => Promise<void>;
  readonly revokeObjectUrl?: (url: string) => void;
}

function selectFromInput(
  documentRef: Document,
  directory: boolean,
): Promise<readonly File[]> {
  return new Promise((resolve) => {
    const input = documentRef.createElement("input");
    input.type = "file";
    input.multiple = true;
    input.hidden = true;
    if (directory) {
      input.setAttribute("webkitdirectory", "");
    }

    const finish = () => {
      const files = Array.from(input.files ?? []);
      input.remove();
      resolve(files);
    };
    input.addEventListener("change", finish, { once: true });
    input.addEventListener("cancel", finish, { once: true });
    documentRef.body.append(input);
    input.click();
  });
}

async function notifyInBrowser(input: NotificationInput): Promise<void> {
  if (!("Notification" in globalThis)) {
    return;
  }
  let permission = Notification.permission;
  if (permission === "default") {
    permission = await Notification.requestPermission();
  }
  if (permission === "granted") {
    const notificationOptions: NotificationOptions =
      input.body === undefined ? {} : { body: input.body };
    new Notification(input.title, notificationOptions);
  }
}

export function createDomBrowserPlatformDriver(
  options: DomBrowserPlatformOptions = {},
): BrowserPlatformDriver {
  const documentRef = options.document ?? document;
  const createObjectUrl = options.createObjectUrl ?? URL.createObjectURL.bind(URL);
  const revokeObjectUrl = options.revokeObjectUrl ?? URL.revokeObjectURL.bind(URL);

  return {
    notify: options.notify ?? notifyInBrowser,
    async saveDownload(input) {
      // Embedded browsers can download HTTP attachments but reject blob: downloads.
      const downloadUrl = input.sourceUrl ?? createObjectUrl(input.blob);
      const anchor = documentRef.createElement("a");
      anchor.download = input.suggestedName;
      anchor.href = downloadUrl;
      anchor.hidden = true;
      documentRef.body.append(anchor);
      try {
        anchor.click();
      } finally {
        anchor.remove();
        if (input.sourceUrl === undefined) revokeObjectUrl(downloadUrl);
      }
    },
    async selectDirectory() {
      const files = await selectFromInput(documentRef, true);
      const firstFile = files[0];
      if (!firstFile) {
        return null;
      }
      const relativePath = firstFile.webkitRelativePath;
      return {
        files,
        name: relativePath.split("/")[0] || firstFile.name,
      };
    },
    selectFiles: () => selectFromInput(documentRef, false),
  };
}

function toLocalFileRef(file: File): LocalFileRef {
  const reference: LocalFileRef = {
    file,
    name: file.name,
    size: file.size,
    type: file.type,
  };
  return file.webkitRelativePath
    ? { ...reference, relativePath: file.webkitRelativePath }
    : reference;
}

export class BrowserPlatformBridge implements PlatformBridge {
  readonly kind = "browser" as const;

  constructor(private readonly driver: BrowserPlatformDriver) {}

  async selectFiles(): Promise<readonly LocalFileRef[]> {
    const files = await this.driver.selectFiles();
    return files.map(toLocalFileRef);
  }

  async selectDirectory(): Promise<LocalDirectoryRef | null> {
    const selected = await this.driver.selectDirectory();
    if (selected === null) {
      return null;
    }
    return {
      files: selected.files.map(toLocalFileRef),
      name: selected.name,
    };
  }

  async saveDownload(input: SaveDownloadInput): Promise<void> {
    await this.driver.saveDownload(input);
  }

  async openDownloadedFile(path: string): Promise<void> {
    void path;
    throw new Error("Opening downloaded files is not supported in the browser");
  }

  async notify(input: NotificationInput): Promise<void> {
    await this.driver.notify(input);
  }
}
