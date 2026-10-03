import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  esbuild: {
    target: 'esnext',
  },
  build: {
    target: 'esnext',
  },
  server: {
    host: "0.0.0.0",
    port: 6001,
    allowedHosts: [
      "netguard.notoriousdev.in",
      "traefik-dashboard.notoriousdev.in",
    ],
  },
})