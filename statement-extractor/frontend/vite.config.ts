import { defineConfig, type PluginOption } from 'vite'
import react from '@vitejs/plugin-react'

/** Uploads return at once (processing runs in the background and the UI polls
 *  its status), but a large file on a slow link can still take a while to send,
 *  so the dev server does not time requests out. */
const allowLongUploads: PluginOption = {
  name: 'allow-long-uploads',
  configureServer(server) {
    if (server.httpServer) server.httpServer.requestTimeout = 0
  },
}

export default defineConfig({
  plugins: [react(), allowLongUploads],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        // Same reason: no socket timeout on either leg of the proxied request.
        timeout: 0,
        proxyTimeout: 0,
      },
    },
  },
})
