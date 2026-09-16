import { cp } from "node:fs/promises";
import { join } from "node:path";

export function resolvePublicBaseUrl(command: "build" | "serve"): string {
  return command === "build" ? "/manyselves/" : "/";
}

export async function duplicatePublicAssets(outputDirectory: string, publicBaseUrl: string): Promise<void> {
  const mountSegments = new URL(publicBaseUrl, "https://manyselves.invalid")
    .pathname
    .split("/")
    .filter(Boolean);
  if (mountSegments.length === 0) return;

  const sourceDirectory = join(outputDirectory, "assets");
  const destinationDirectory = join(outputDirectory, ...mountSegments, "assets");
  await cp(sourceDirectory, destinationDirectory, { force: true, recursive: true });
}
