import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const temporaryDirectory = mkdtempSync(join(tmpdir(), "manyselves-openapi-"));
const generated = join(temporaryDirectory, "schema.ts");
try {
  const result = spawnSync(
    process.execPath,
    ["node_modules/openapi-typescript/bin/cli.js", "../frontend-contract/openapi.json", "-o", generated],
    { cwd: resolve(import.meta.dirname, ".."), encoding: "utf8" },
  );
  if (result.status !== 0) {
    process.stderr.write(result.stderr || result.stdout);
    process.exit(result.status ?? 1);
  }
  const committed = readFileSync(resolve(import.meta.dirname, "../src/api/generated/schema.ts"), "utf8");
  const fresh = readFileSync(generated, "utf8");
  if (committed !== fresh) {
    console.error("Generated API types are stale. Run npm run generate:api and commit the result.");
    process.exit(1);
  }
} finally {
  rmSync(temporaryDirectory, { force: true, recursive: true });
}
