/// <reference types="vitest/config" />
import { readFileSync } from "node:fs";
import { defineConfig, type Plugin } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// The daemon serves pseudolife_memory/web/static/** under /ui/, so the build
// is written there and served at /ui/ with no backend change. The output is
// committed: the Python wheel ships static/** and nothing installs Node.
// public/ (the vendored 3D galaxy bundle) is copied into the output as is.
const DAEMON = "http://127.0.0.1:8770"; // python -m pseudolife_memory.web.devserver

// The vendored Geist fonts are SIL OFL 1.1: ship the license beside them.
function fontLicense(): Plugin {
  return {
    name: "geist-font-license",
    apply: "build",
    generateBundle() {
      this.emitFile({
        type: "asset",
        fileName: "assets/Geist-LICENSE.txt",
        source: readFileSync(new URL("./src/fonts/LICENSE.txt", import.meta.url), "utf8"),
      });
    },
  };
}

export default defineConfig({
  base: "/ui/",
  plugins: [svelte(), fontLicense()],
  build: {
    // Emptied on every build: static/ holds nothing but this console's output.
    outDir: "../pseudolife_memory/web/static",
    emptyOutDir: true,
    sourcemap: false,
    // Fonts stay separate files (cacheable); nothing is inlined as data: URIs.
    assetsInlineLimit: 0,
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: DAEMON, changeOrigin: false },
      "/health": { target: DAEMON, changeOrigin: false },
    },
  },
  test: {
    include: ["src/**/*.test.ts"],
    environment: "node",
  },
});
