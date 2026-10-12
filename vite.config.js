import { defineConfig } from 'vite';

export default defineConfig({
  root: '.',
  publicDir: 'public',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: { main: 'index.html', admin: 'admin.html' },   // /admin is the password admin console
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/admin': {                      // dev: let `npm run dev` serve admin.html instead of proxying
        target: 'http://localhost:5173',
        bypass: () => '/admin.html',
      },
      '/api': {
        target: 'http://localhost:5050',
        changeOrigin: true,
      },
    },
  },
});
