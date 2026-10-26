import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { ToastViewport } from './components/ui/Toast'
import { resetQueryCacheOnSessionChange, useAuth } from './stores/auth'
import '@fontsource-variable/inter'
import '@fontsource-variable/jetbrains-mono'
import './styles/fonts.css'
import './styles/globals.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
})

// Responses depend on who asked (Inventory is tag-scoped per caller) and query keys carry no identity: when the
// signed-in user changes or the token goes away, nothing fetched for the previous session may be shown to the next.
resetQueryCacheOnSessionChange(useAuth, queryClient)

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
        <ToastViewport />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
