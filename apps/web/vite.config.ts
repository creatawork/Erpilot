import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 开发期把 /api 代理到 FastAPI（uv run --package erpilot-api uvicorn erpilot_api.main:app）
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
