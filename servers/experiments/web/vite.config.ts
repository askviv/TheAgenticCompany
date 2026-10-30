import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const projectRoot = path.dirname(fileURLToPath(import.meta.url))

export default defineConfig({
  base: '/static/app/',
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': path.resolve(projectRoot, 'src') } },
  build: { outDir: '../static/app', emptyOutDir: true },
})
