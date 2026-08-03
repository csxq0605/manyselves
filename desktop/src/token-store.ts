import { chmod, mkdir, readFile, unlink, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import type { SafeStorage } from "electron";

export class SecureTokenStore {
  private memoryToken: string | null = null;

  constructor(private readonly path: string, private readonly storage: SafeStorage) {}

  async get(): Promise<string | null> {
    if (!this.storage.isEncryptionAvailable()) return this.memoryToken;
    try {
      const encrypted = await readFile(this.path);
      return this.storage.decryptString(encrypted);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
      throw error;
    }
  }

  async set(token: string): Promise<void> {
    if (!this.storage.isEncryptionAvailable()) {
      this.memoryToken = token;
      return;
    }
    await mkdir(dirname(this.path), { recursive: true, mode: 0o700 });
    await writeFile(this.path, this.storage.encryptString(token), { mode: 0o600 });
    await chmod(this.path, 0o600);
    this.memoryToken = null;
  }

  async delete(): Promise<void> {
    this.memoryToken = null;
    try {
      await unlink(this.path);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }
}
