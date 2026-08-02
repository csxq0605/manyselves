import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Manyselves root element is missing");
}

createRoot(rootElement).render(
  <StrictMode>
    <main aria-label="Manyselves">
      <h1>Manyselves</h1>
    </main>
  </StrictMode>,
);
