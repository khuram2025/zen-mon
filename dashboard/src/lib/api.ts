import axios, { AxiosError } from 'axios'

// Token storage — Zustand persists it but we also keep it here for
// the axios interceptor, which runs outside React.
let currentToken: string | null = null

export function setApiToken(token: string | null) {
  currentToken = token
}

export const api = axios.create({
  baseURL: '/api/v1',
  timeout: 30_000,
  headers: { 'Content-Type': 'application/json' },
})

api.interceptors.request.use((config) => {
  if (currentToken) {
    config.headers.Authorization = `Bearer ${currentToken}`
  }
  return config
})

api.interceptors.response.use(
  (r) => r,
  (err: AxiosError) => {
    if (err.response?.status === 401) {
      // Wipe token and bounce to login. The auth store listens for
      // this custom event so it can clear its own state too.
      currentToken = null
      window.dispatchEvent(new CustomEvent('zp-auth-expired'))
    }
    return Promise.reject(err)
  },
)
