import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./index.css";

if (new URLSearchParams(window.location.search).get("embed") === "1") {
  document.documentElement.classList.add("embedded");
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
