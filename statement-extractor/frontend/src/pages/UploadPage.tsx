import { useEffect, useRef, useState } from 'react'

import { AlertIcon, ArrowRightIcon, FileTypeTile, ShieldIcon, UploadCloudIcon } from '../components/icons'
import { ErrorBanner } from '../components/ui'
import { formatBytes } from '../i18n/format'
import { useI18n, type MessageKey } from '../i18n'
import { ApiRequestError, api } from '../services/api'
import type { AppConfig, Engine } from '../types'

const FALLBACK_LIMIT = 25 * 1024 * 1024

export function UploadPage({ config, onUploaded }: { config: AppConfig | null; onUploaded: (documentId: string) => void }) {
  const { t, tc, locale } = useI18n()
  const [file, setFile] = useState<File | null>(null)
  const [dragging, setDragging] = useState(false)
  const [engine, setEngine] = useState<Engine>(config?.default_engine ?? 'local')
  const [consent, setConsent] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (config) setEngine(config.default_engine)
  }, [config])

  const limit = config?.max_file_size_bytes ?? FALLBACK_LIMIT
  const notSupported = Boolean(file) && !/\.(pdf|jpe?g|png)$/i.test(file!.name)
  const tooLarge = Boolean(file) && file!.size > limit
  const engines = config?.engines ?? [{ name: 'local' as Engine, available: true, external: false, problems: [] }]
  const selected = engines.find((e) => e.name === engine)
  const external = Boolean(selected?.external)
  const blocked = !file || notSupported || tooLarge || !selected?.available || (external && !consent) || busy
  const mb = Math.round(limit / (1024 * 1024))

  const pick = (next: File | null) => {
    setError(null)
    setFile(next)
  }

  const submit = async () => {
    if (!file) return
    setBusy(true)
    setError(null)
    try {
      const upload = await api.upload(file, engine)
      onUploaded(upload.document_id)
    } catch (caught) {
      setError(caught instanceof ApiRequestError ? tc(caught.code, caught.message, caught.params) : t('msg.network'))
      setBusy(false)
    }
  }

  const problem = notSupported ? t('upload.notSupported') : tooLarge ? t('upload.tooLarge', { mb }) : null

  return (
    <main className="mx-auto w-full max-w-[960px] px-4 pb-16 pt-10 xl:px-0">
      <h1 className="text-3xl font-bold tracking-[-0.02em] text-ink">{t('upload.title')}</h1>
      <p className="mt-2 max-w-[760px] text-lg text-ink-muted">{t('upload.intro')}</p>

      <div className="mt-8 grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
        <section className="card p-6">
          <h2 className="text-md font-semibold text-ink">{t('upload.file')}</h2>
          <div
            onDragOver={(event) => {
              event.preventDefault()
              setDragging(true)
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => {
              event.preventDefault()
              setDragging(false)
              pick(event.dataTransfer.files?.[0] ?? null)
            }}
            className={`mt-2.5 rounded-card border-2 border-dashed px-6 py-10 text-center transition-colors ${
              dragging ? 'border-navy bg-navy-soft' : 'border-navy-border bg-page'
            }`}
          >
            <UploadCloudIcon size={34} className="mx-auto text-navy" />
            <p className="mt-3 text-md font-semibold text-ink">{t('upload.drop')}</p>
            <p className="mt-1 text-base text-ink-subtle">{t('upload.types', { mb, pages: config?.max_pages ?? 200 })}</p>
            <button type="button" onClick={() => inputRef.current?.click()} className="btn-ghost focus-ring mt-4">
              {t('upload.browse')}
            </button>
            <input
              ref={inputRef}
              type="file"
              accept="application/pdf,image/jpeg,image/png,.pdf,.jpg,.jpeg,.png"
              className="hidden"
              onChange={(event) => pick(event.target.files?.[0] ?? null)}
            />
          </div>

          {file && (
            <div className="mt-4 flex items-center gap-3 rounded-control border border-line bg-white px-3.5 py-3">
              <FileTypeTile type={file.name.split('.').pop()?.slice(0, 4) ?? 'pdf'} tone={problem ? 'slate' : 'red'} />
              <div className="min-w-0 flex-1">
                <p className="truncate text-md font-medium text-ink"><bdi>{file.name}</bdi></p>
                <p className="text-sm text-ink-subtle">{formatBytes(file.size, locale)}</p>
              </div>
              <button type="button" onClick={() => pick(null)} className="focus-ring rounded-md px-2 py-1 text-sm text-ink-subtle hover:bg-page">
                {t('upload.remove')}
              </button>
            </div>
          )}

          <fieldset className="mt-5">
            <legend className="text-md font-semibold text-ink">{t('upload.engine')}</legend>
            <div className="mt-2 space-y-2">
              {engines.map((option) => (
                <label
                  key={option.name}
                  className={`flex cursor-pointer items-start gap-3 rounded-control border px-3.5 py-2.5 ${
                    engine === option.name ? 'border-navy bg-navy-soft/50' : 'border-line bg-white'
                  } ${option.available ? '' : 'cursor-not-allowed opacity-60'}`}
                >
                  <input
                    type="radio"
                    name="engine"
                    className="mt-1"
                    value={option.name}
                    checked={engine === option.name}
                    disabled={!option.available}
                    onChange={() => {
                      setEngine(option.name)
                      setConsent(false)
                    }}
                  />
                  <span className="min-w-0">
                    <span className="block text-base font-medium text-ink">{t(`engine.${option.name}` as MessageKey)}</span>
                    <span className="block text-sm text-ink-subtle">{t(`engine.${option.name}.help` as MessageKey)}</span>
                    {!option.available && (
                      <span className="mt-0.5 block text-xs text-warn">
                        {t('engine.unavailable')}: {option.problems.map((p) => tc(p.code, p.message)).join(' ')}
                      </span>
                    )}
                  </span>
                </label>
              ))}
            </div>
          </fieldset>

          {external && selected?.available && (
            <div className="mt-4 rounded-control border border-warn/40 bg-warn-bg px-3.5 py-3 text-sm text-ink">
              <p className="flex items-center gap-2 font-semibold text-warn">
                <ShieldIcon size={15} /> {t('upload.privacyTitle')}
              </p>
              <p className="mt-1 text-ink-muted">{t('upload.privacyBody')}</p>
              <label className="mt-2 flex items-center gap-2">
                <input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} />
                {t('upload.privacyAccept')}
              </label>
            </div>
          )}

          {(problem || error) && (
            <div className="mt-4">
              {problem ? (
                <div role="alert" className="flex items-start gap-2 rounded-control bg-bad-bg px-3.5 py-3 text-base text-bad">
                  <AlertIcon size={16} className="mt-0.5 shrink-0" />
                  {problem}
                </div>
              ) : (
                <ErrorBanner message={error!} onDismiss={() => setError(null)} />
              )}
            </div>
          )}

          <button type="button" disabled={blocked} onClick={() => void submit()} className="btn-primary focus-ring mt-5 w-full gap-2">
            {t('upload.submit')}
            <ArrowRightIcon size={15} className="rtl:rotate-180" />
          </button>
        </section>

        <aside className="card self-start p-6">
          <h2 className="text-md font-semibold text-ink">{t('upload.whatTitle')}</h2>
          <ul className="mt-3 space-y-3">
            {(['upload.what1', 'upload.what2', 'upload.what3', 'upload.what4', 'upload.what5'] as MessageKey[]).map((key) => (
              <li key={key} className="flex gap-2.5 text-base text-ink-muted">
                <span className="mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full bg-navy" aria-hidden="true" />
                {t(key)}
              </li>
            ))}
          </ul>
        </aside>
      </div>
    </main>
  )
}
