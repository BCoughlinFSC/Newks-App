import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ command }) => ({
  plugins: [react()],
  base: command === "build" ? "/menuboard/" : "/",
  build: {
    outDir: "../backend/menuboard",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    host: true,
    proxy: {
      "/api": "http://localhost:8787",
      "/uploads": "http://localhost:8787"
    }
  }
}));
