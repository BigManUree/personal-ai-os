import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  base: '/static/',
  build: {
    outDir: '../app/static',
    // Empty the output dir so stale hashed bundles cannot accumulate. app/static
    // now contains only Vite output (index.html + assets/); the legacy hand-written
    // scripts that used to live here were removed on 2026-10-22.
    emptyOutDir: true,
    assetsDir: 'assets',
  },
})
