import { useEffect, useState } from 'react'

import { CheckIcon } from '../components/icons'
import { ErrorBanner, Spinner } from '../components/ui'
import { useI18n, type MessageKey } from '../i18n'
import { ApiRequestError, api } from '../services/api'
import type { Run } from '../types'

const STAGES = ['queued', 'reading', 'ocr', 'analyzing', 'llm', 'validating', 'saving', 'done'] as const

/** Real progress: polls the run's stage and counters until it completes or fails. */
export function ProcessingPage({ documentId, filename, onDone }: { documentId: string; filename?: string; onDone: () => void }) {
  const { t, tc, tm } = useI18n()
  const [run, setRun] = useState<Run | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [retrying, setRetrying] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let stopped = false
    let timer = 0
    const poll = async () => {
      try {
        const status = await api.status(documentId)
        if (stopped) return
        setRun(status.run)
        setError(null)
        if (status.run.status === 'completed') {
          onDone()
          return
        }
        if (status.run.status === 'failed') return
      } catch (caught) {
        if (stopped) return
        setError(caught instanceof ApiRequestError ? tc(caught.code, caught.message) : t('msg.network'))
      }
      timer = window.setTimeout(poll, 1000)
    }
    void poll()
    return () => {
      stopped = true
      window.clearTimeout(timer)
    }
  }, [documentId, onDone, t, tc, attempt])

  const engine = run?.engine_requested ?? 'local'
  const stages = STAGES.filter((s) => (s === 'llm' ? engine !== 'local' : true))
  const currentIndex = run ? Math.max(0, stages.indexOf((run.stage ?? 'queued') as (typeof STAGES)[number])) : 0
  const failed = run?.status === 'failed'

  const retryLocal = async () => {
    setRetrying(true)
    try {
      await api.reprocess(documentId, 'local')
      setRun(null)
      setRetrying(false)
      setAttempt((n) => n + 1)
    } catch (caught) {
      setError(caught instanceof ApiRequestError ? tc(caught.code, caught.message) : t('msg.network'))
      setRetrying(false)
    }
  }

  return (
    <main className="mx-auto w-full max-w-[600px] px-4 pb-16 pt-14">
      <div className="card p-7">
        <div className="flex items-center gap-3">
          {!failed && <Spinner size={18} />}
          <div className="min-w-0">
            <p className="truncate text-md font-semibold text-ink">
              {t('processing.title', { file: filename ?? documentId.slice(0, 8) })}
            </p>
            <p className="text-sm text-ink-subtle">{t(`engine.${engine}` as MessageKey)}</p>
          </div>
        </div>

        {!failed && <p className="mt-3 text-sm text-ink-subtle">{t('processing.subtitle')}</p>}

        <ol className="mt-6 space-y-4" aria-live="polite">
          {stages.map((stage, index) => {
            const done = index < currentIndex || run?.status === 'completed'
            const current = index === currentIndex && !failed && run?.status !== 'completed'
            const showProgress = current && run?.progress_total
            return (
              <li key={stage} className="flex items-start gap-3">
                <span
                  className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-white ${
                    done ? 'bg-ok' : current ? 'bg-navy' : 'bg-line'
                  }`}
                >
                  {done ? <CheckIcon size={12} /> : current ? <span className="h-2 w-2 animate-pulse rounded-full bg-white" /> : null}
                </span>
                <span>
                  <span className={`block text-md ${done || current ? 'font-semibold text-ink' : 'text-ink-subtle'}`}>
                    {t(`processing.stage.${stage}` as MessageKey)}
                  </span>
                  {showProgress ? (
                    <span className="block text-sm text-ink-subtle">
                      {t('processing.progress', { current: run!.progress_current ?? 0, total: run!.progress_total })}
                    </span>
                  ) : null}
                </span>
              </li>
            )
          })}
        </ol>

        {failed && (
          <div className="mt-6 space-y-3">
            <ErrorBanner message={`${t('processing.failed')}: ${tm(run!.error)}`} />
            <div className="flex flex-wrap gap-2">
              {engine !== 'local' && (
                <button type="button" disabled={retrying} className="btn-primary focus-ring" onClick={() => void retryLocal()}>
                  {t('processing.useLocal')}
                </button>
              )}
              <a href="#/" className="btn-ghost focus-ring">
                {t('processing.backToUpload')}
              </a>
            </div>
          </div>
        )}
        {error && !failed && (
          <div className="mt-4">
            <ErrorBanner message={error} />
          </div>
        )}
      </div>
    </main>
  )
}
