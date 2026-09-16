import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import {
  duplicatePublicAssets,
  resolvePublicBaseUrl,
} from "../../build/duplicate-public-assets";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((path) => rm(path, { force: true, recursive: true })));
});

describe("duplicate public assets", () => {
  it("keeps the development server at root while production builds target the public mount", () => {
    expect(resolvePublicBaseUrl("serve")).toBe("/");
    expect(resolvePublicBaseUrl("build")).toBe("/manyselves/");
  });

  it("copies root assets under the configured public subpath for direct-port access", async () => {
    const outputDirectory = await mkdtemp(join(tmpdir(), "manyselves-assets-"));
    temporaryDirectories.push(outputDirectory);
    await mkdir(join(outputDirectory, "assets"));
    await writeFile(join(outputDirectory, "assets", "entry.js"), "export const ready = true;", "utf8");

    await duplicatePublicAssets(outputDirectory, "/manyselves/");

    await expect(readFile(
      join(outputDirectory, "manyselves", "assets", "entry.js"),
      "utf8",
    )).resolves.toBe("export const ready = true;");
  });

  it("does nothing when assets are already served from the origin root", async () => {
    const outputDirectory = await mkdtemp(join(tmpdir(), "manyselves-assets-"));
    temporaryDirectories.push(outputDirectory);
    await mkdir(join(outputDirectory, "assets"));
    await writeFile(join(outputDirectory, "assets", "entry.js"), "root", "utf8");

    await duplicatePublicAssets(outputDirectory, "/");

    await expect(readFile(join(outputDirectory, "assets", "entry.js"), "utf8")).resolves.toBe("root");
  });
});
