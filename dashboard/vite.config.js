import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// AutoClaw operator dashboard (Synergy #15 — OmniClaw "Local React
// Dashboard"). Built bundle is committed to ../ui/dashboard and served by
// the Flask proxy at /dashboard — no Node runtime needed in production.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../ui/dashboard",
    emptyOutDir: true,
    assetsDir: "assets",
  },
});
