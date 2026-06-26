import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backendTarget = process.env.VITE_BACKEND_URL || "http://192.168.0.196:8000";

export default defineConfig({
  plugins: [react()],
  root: "frontend",
  build: {
    outDir: "../static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": backendTarget,
      "/events": backendTarget,
      "/health": backendTarget,
    },
  },
});
