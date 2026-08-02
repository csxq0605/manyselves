import { createBrowserRouter } from "react-router-dom";
import type { ReactElement } from "react";

export function createAppRouter(app: ReactElement) {
  return createBrowserRouter([
    {
      element: app,
      path: "*",
    },
  ]);
}
