import { fileURLToPath, URL } from 'node:url'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'

export default defineConfig(({ mode }) => ({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  envDir: '..',
  server: {
    strictPort: true,
    proxy: { '/api': loadEnv(mode, '..', 'PUBLIC_BASE_URL').PUBLIC_BASE_URL ?? 'http://localhost:8080' },
  },
  test: { environment: 'jsdom' },
}))
