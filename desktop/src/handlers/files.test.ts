import { mkdtemp, mkdir, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";

vi.mock("electron", () => ({ dialog: {} }));

import { enumerateImportDirectory } from "./files.js";

describe("directory import", () => {
  it("returns normalized regular files and excludes symbolic links", async () => {
    const root = await mkdtemp(join(tmpdir(), "manyselves-import-"));
    await mkdir(join(root, "nested"));
    await writeFile(join(root, "a.txt"), "a");
    await writeFile(join(root, "nested", "b.csv"), "b");
    await symlink(join(root, "nested"), join(root, "linked"), "junction");
    const result = await enumerateImportDirectory(root);
    expect(result.files.map((entry) => entry.relativePath)).toEqual(["a.txt", "nested/b.csv"]);
    expect(result.skipped).toEqual(["linked"]);
  });
});
