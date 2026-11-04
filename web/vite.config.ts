import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// WEB_PORT picks the port for both `vite` (make web) and `vite preview` (e2e fixture mode).
const port = Number(process.env.WEB_PORT ?? "5173");

export default defineConfig({
  plugins: [react()],
  // The manifest tells scripts/bundle-size.mjs which chunks are the entry and which are lazy.
  build: { manifest: true },
  server: { port, strictPort: true },
  preview: { port, strictPort: true },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
