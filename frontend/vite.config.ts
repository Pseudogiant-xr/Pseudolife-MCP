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

// The 3D engine is a prebuilt ES module in public/vendor/, imported at
// runtime. A production build copies it and the daemon serves it as a plain
// file; Vite's dev server instead refuses to let source import a public
// file, so in dev it is served verbatim here, ahead of Vite's transforms.
function vendorPassthrough(): Plugin {
  const dir = new URL("./public/vendor/", import.meta.url);
  return {
    name: "vendor-passthrough",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use("/ui/vendor/", (req, res, next) => {
        const name = (req.url ?? "").split("?")[0].replace(/^\/+/, "");
        if (!/^[\w.-]+\.js$/.test(name)) return next();
        let body: string;
        try {
          body = readFileSync(new URL(name, dir), "utf8");
        } catch {
          return next();
        }
        res.setHeader("Content-Type", "text/javascript; charset=utf-8");
        res.end(body);
      });
    },
  };
}

export default defineConfig({
  base: "/ui/",
  plugins: [svelte(), fontLicense(), vendorPassthrough()],
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
