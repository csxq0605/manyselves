import { spawn } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

const serverUrl = "http://127.0.0.1:4173";
const forwardedArguments = process.argv.slice(2);

async function serverReady() {
  try {
    const response = await fetch(serverUrl);
    return response.ok;
  } catch {
    return false;
  }
}

async function waitForServer(child) {
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`Vite exited before becoming ready (${child.exitCode})`);
    }
    if (await serverReady()) return;
    await delay(200);
  }
  throw new Error("Timed out waiting for the E2E Vite server");
}

function waitForExit(child) {
  return new Promise((resolve) => child.once("exit", (code, signal) => resolve({ code, signal })));
}

async function stop(child) {
  if (!child || child.exitCode !== null) return;
  const exited = waitForExit(child);
  child.kill("SIGTERM");
  await Promise.race([
    exited,
    delay(5_000),
  ]);
  if (child.exitCode === null) {
    child.kill("SIGKILL");
    await exited;
  }
}

let vite = null;
try {
  if (!await serverReady()) {
    vite = spawn(
      process.execPath,
      ["node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", "4173"],
      { stdio: "inherit", windowsHide: true },
    );
    await waitForServer(vite);
  }

  const playwright = spawn(
    process.execPath,
    ["node_modules/@playwright/test/cli.js", "test", ...forwardedArguments],
    { stdio: "inherit", windowsHide: true },
  );
  const result = await waitForExit(playwright);
  process.exitCode = result.code ?? 1;
} finally {
  await stop(vite);
}
