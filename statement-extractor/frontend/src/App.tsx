import { useCallback, useEffect, useState } from 'react'

import { AppHeader } from './components/AppHeader'
import { ErrorBanner, Spinner } from './components/ui'
import { useI18n } from './i18n'
import { HistoryPage } from './pages/HistoryPage'
import { ProcessingPage } from './pages/ProcessingPage'
import { ResultPage } from './pages/ResultPage'
import { SettingsPage } from './pages/SettingsPage'
import { UploadPage } from './pages/UploadPage'
import { ApiRequestError, api } from './services/api'
import type { AppConfig, ExtractionResult } from './types'

function currentRoute(): string {
  const hash = window.location.hash.replace(/^#/, '')
  return hash || '/'
}

function navigate(route: string): void {
  window.location.hash = route
}

export default function App() {
  const [route, setRoute] = useState(currentRoute)
  const [config, setConfig] = useState<AppConfig | null>(null)
  const [configError, setConfigError] = useState<ApiRequestError | null>(null)

  useEffect(() => {
    const onHash = () => setRoute(currentRoute())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const loadConfig = useCallback(async () => {
    try {
      setConfig(await api.config())
      setConfigError(null)
    } catch (caught) {
      setConfigError(caught instanceof ApiRequestError ? caught : null)
    }
  }, [])

  useEffect(() => {
    void loadConfig()
  }, [loadConfig])

  const documentMatch = /^\/documents\/([\w-]+)/.exec(route)

  return (
    <div className="flex h-full min-h-screen flex-col bg-page">
      <AppHeader route={route} />
      {configError?.code === 'unauthorized' && !route.startsWith('/settings') && <AuthNotice />}
      {route === '/' && <UploadPage config={config} onUploaded={(id) => navigate(`/documents/${id}`)} />}
      {route.startsWith('/history') && <HistoryPage />}
      {route.startsWith('/settings') && <SettingsPage config={config} onTokenSaved={() => void loadConfig()} />}
      {documentMatch && <DocumentView key={documentMatch[1]} documentId={documentMatch[1]} />}
    </div>
  )
}

function AuthNotice() {
  const { t } = useI18n()
  return (
    <div className="mx-auto mt-4 w-full max-w-[960px] px-4">
      <ErrorBanner message={t('msg.unauthorized')} />
      <a href="#/settings" className="mt-2 inline-block text-sm text-navy underline">{t('nav.settings')}</a>
    </div>
  )
}

/** Shows live processing while a run is active, the review screen once it is done. */
function DocumentView({ documentId }: { documentId: string }) {
  const { t, tc } = useI18n()
  const [phase, setPhase] = useState<'loading' | 'processing' | 'result' | 'error'>('loading')
  const [result, setResult] = useState<ExtractionResult | null>(null)
  const [filename, setFilename] = useState<string | undefined>()
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setPhase('loading')
    try {
      const status = await api.status(documentId)
      if (status.run.status === 'queued' || status.run.status === 'processing') {
        setPhase('processing')
        return
      }
      const next = await api.result(documentId)
      setResult(next)
      setFilename(next.document.filename)
      setPhase('result')
    } catch (caught) {
      if (caught instanceof ApiRequestError && caught.code === 'processing') {
        setPhase('processing')
        return
      }
      setError(caught instanceof ApiRequestError ? tc(caught.code, caught.message) : t('msg.network'))
      setPhase('error')
    }
  }, [documentId, t, tc])

  useEffect(() => {
    void load()
  }, [load])

  if (phase === 'loading') {
    return (
      <main className="flex flex-1 items-center justify-center gap-2 text-ink-subtle">
        <Spinner /> {t('common.loading')}
      </main>
    )
  }
  if (phase === 'processing') return <ProcessingPage documentId={documentId} filename={filename} onDone={() => void load()} />
  if (phase === 'error' || !result) {
    return (
      <main className="mx-auto w-full max-w-[720px] px-4 pt-10">
        <ErrorBanner message={error ?? t('error.title')} />
        <button type="button" className="btn-ghost focus-ring mt-3" onClick={() => void load()}>{t('common.retry')}</button>
      </main>
    )
  }
  return <ResultPage result={result} onChange={setResult} onReprocessed={() => setPhase('processing')} />
}
