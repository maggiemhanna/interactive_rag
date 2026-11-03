import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

export default defineConfig(({ mode }) => {
  const rootEnv = loadEnv(mode, path.resolve(__dirname, '..'), '')
  const localEnv = loadEnv(mode, __dirname, '')

  const allowedLlmModels =
    localEnv.VITE_ALLOWED_LLM_MODELS ||
    localEnv.ALLOWED_LLM_MODELS ||
    rootEnv.VITE_ALLOWED_LLM_MODELS ||
    rootEnv.ALLOWED_LLM_MODELS ||
    ''
  const allowedEmbeddingModels =
    localEnv.VITE_ALLOWED_EMBEDDING_MODELS ||
    localEnv.ALLOWED_EMBEDDING_MODELS ||
    rootEnv.VITE_ALLOWED_EMBEDDING_MODELS ||
    rootEnv.ALLOWED_EMBEDDING_MODELS ||
    ''

  return {
    plugins: [react()],
    define: {
      'import.meta.env.VITE_ALLOWED_LLM_MODELS': JSON.stringify(allowedLlmModels),
      'import.meta.env.VITE_ALLOWED_EMBEDDING_MODELS': JSON.stringify(allowedEmbeddingModels),
    },
    server: {
      host: true,
      port: 3000,
      proxy: {
        '/api/ingest': {
          target: 'http://localhost:8000',
          changeOrigin: true,
          rewrite: (p) => p.replace(/^\/api\/ingest/, ''),
        },
        '/api/agent': {
          target: 'http://localhost:8001',
          changeOrigin: true,
          rewrite: (p) => p.replace(/^\/api\/agent/, ''),
        },
        '/api': {
          target: 'http://localhost:8000',
          changeOrigin: true,
          rewrite: (p) => p.replace(/^\/api/, ''),
        },
      },
    },
  }
})
