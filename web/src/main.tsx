import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import "./index.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("#root is missing from index.html");
}

// BrowserRouter, not HashRouter: whatever serves web/dist in production must fall back to
// index.html for unknown paths, or a reload on /history/<id> 404s. Vite's dev server does
// this already.
createRoot(container).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
