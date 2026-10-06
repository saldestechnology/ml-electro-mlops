import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const api = { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: false } };

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: api },
  preview: { port: 4173, proxy: api },
  build: { outDir: 'dist', emptyOutDir: true },
});
