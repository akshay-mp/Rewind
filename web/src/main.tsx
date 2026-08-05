import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./globals.css";

// Read-only timeline UI mount point. StrictMode is on to catch asymmetry in
// effects early — important because the timeline refetches on trace change
// and a missing cleanup would double-fetch.
const container = document.getElementById("root");
if (container === null) {
  throw new Error("Expected #root element in index.html");
}
createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
