import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const page = (name: string) => fileURLToPath(new URL(name, import.meta.url));

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://localhost:8000" } },
  // Два входа: Mini App и отдельная страница админ-панели (АР-52, задача 2.8).
  build: { rollupOptions: { input: { main: page("index.html"), admin: page("admin.html") } } },
  test: { environment: "jsdom", globals: true, setupFiles: ["./src/test/setup.ts"], restoreMocks: true },
});
