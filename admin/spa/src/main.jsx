import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "@fontsource-variable/bricolage-grotesque";
import "./index.css";
import "./theme.js";

createRoot(document.getElementById("root")).render(<App />);
