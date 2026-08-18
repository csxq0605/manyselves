import { copyFileSync } from "node:fs";
import { resolve } from "node:path";

copyFileSync(resolve(import.meta.dirname, "../src/preload.cjs"), resolve(import.meta.dirname, "../dist/preload.cjs"));
