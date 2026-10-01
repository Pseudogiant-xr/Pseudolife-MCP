import { readFileSync } from "node:fs";
import { defineConfig, type Plugin } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// The daemon serves pseudolife_memory/web/static/** under /ui/, so a build
// written to static/next/ is served at /ui/next/ with no backend change.
// The output is committed: the Python wheel ships static/** without Node.
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
  base: "/ui/next/",
  plugins: [svelte(), fontLicense()],
  build: {
    outDir: "../pseudolife_memory/web/static/next",
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
});
