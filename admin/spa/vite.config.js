import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// Dev proxy targets the nginx container (basic auth). Export ADMIN_USER /
// ADMIN_PASSWORD (same values as ../.env) to have the proxy authenticate.
const { ADMIN_USER, ADMIN_PASSWORD } = process.env;
const headers =
  ADMIN_USER && ADMIN_PASSWORD
    ? { Authorization: "Basic " + Buffer.from(`${ADMIN_USER}:${ADMIN_PASSWORD}`).toString("base64") }
    : undefined;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { proxy: { "/api": { target: "http://localhost:8080", headers } } },
});
