import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: Object.fromEntries(
      ["/health", "/docs", "/openapi.json", "/v1"].map((path) => [path, "http://127.0.0.1:8080"]),
    ),
  },
});
