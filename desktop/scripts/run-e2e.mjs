import { spawn } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

const children = [];
async function wait(child) {
  await delay(1_000);
  if (child.exitCode !== null) throw new Error(`Vite exited (${child.exitCode})`);
}
async function stop(child) {
  if (child.exitCode !== null) return;
  child.kill("SIGTERM");
  await Promise.race([new Promise((resolve) => child.once("exit", resolve)), delay(5_000)]);
  if (child.exitCode === null) child.kill("SIGKILL");
}
try {
  const vite = spawn(process.execPath, ["../frontend/node_modules/vite/bin/vite.js", "../frontend", "--host", "127.0.0.1", "--port", "4173"], { stdio: "inherit", windowsHide: true });
  children.push(vite);
  await wait(vite);
  const test = spawn(process.execPath, ["node_modules/@playwright/test/cli.js", "test", ...process.argv.slice(2)], { stdio: "inherit", windowsHide: true });
  const result = await new Promise((resolve) => test.once("exit", (code) => resolve(code)));
  process.exitCode = result ?? 1;
} finally {
  await Promise.all(children.map(stop));
}
