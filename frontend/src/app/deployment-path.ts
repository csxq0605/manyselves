export interface ResolveWebDeploymentInput {
  readonly assetBaseUrl: string;
  readonly origin: string;
  readonly pathname: string;
}

export interface WebDeployment {
  readonly routerBasename: string;
  readonly serverUrl: string;
}

function normalizeMountPath(assetBaseUrl: string): string {
  const pathname = new URL(assetBaseUrl, "https://manyselves.invalid").pathname;
  const normalized = `/${pathname.replace(/^\/+|\/+$/g, "")}`;
  return normalized === "/" ? "/" : normalized;
}

export function resolveWebDeployment(input: ResolveWebDeploymentInput): WebDeployment {
  const mountPath = normalizeMountPath(input.assetBaseUrl);
  const mountedBelowOrigin = mountPath !== "/"
    && (input.pathname === mountPath || input.pathname.startsWith(`${mountPath}/`));
  const routerBasename = mountedBelowOrigin ? mountPath : "/";
  const serverUrl = `${input.origin.replace(/\/+$/, "")}${mountedBelowOrigin ? mountPath : ""}`;
  return { routerBasename, serverUrl };
}
