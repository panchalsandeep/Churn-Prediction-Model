import { defineConfig } from 'vite';

export default defineConfig({
  root: 'static',
  publicDir: '.',
  build: {
    outDir: "../dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:5050',
        changeOrigin: true,
      },
    },
  },
});
