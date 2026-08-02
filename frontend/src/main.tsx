import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router-dom";

import { App } from "./app/App";
import { getOrCreateBrowserClientId } from "./app/client-config";
import { AppProviders } from "./app/providers";
import { createAppRouter } from "./app/router";
import { createApiGateway } from "./api/gateway";

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Manyselves root element is missing");
}

const baseUrl = import.meta.env.VITE_API_BASE_URL?.trim() || window.location.origin;
const fetchImplementation = window.fetch.bind(window);
const getToken = () => window.sessionStorage.getItem("manyselves.deploymentToken");
const getLeaseToken = () => window.sessionStorage.getItem("manyselves.controlLeaseToken");
const gateway = createApiGateway({
  baseUrl,
  clientId: getOrCreateBrowserClientId(window.localStorage),
  fetch: fetchImplementation,
  getLeaseToken,
  getToken,
});
const router = createAppRouter(
  <App
    eventSource={{ baseUrl, fetch: fetchImplementation, getToken }}
    gateway={gateway}
  />,
);

createRoot(rootElement).render(
  <StrictMode>
    <AppProviders>
      <RouterProvider router={router} />
    </AppProviders>
  </StrictMode>,
);
