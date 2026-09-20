import path from "path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  // `pnpm dev` serves the app but not the tracker feed, so hand /api to the
  // data server. Without this, development falls back to sample data.
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:5174",
        changeOrigin: true,
        // `/api/live` is a WebSocket. Vite only registers an upgrade handler
        // for a proxy entry that asks for one, so without this the socket
        // fails to connect and the dev UI silently goes back to polling
        // snapshots — REST works, live deltas never arrive.
        ws: true,
      },
    },
  },
})
