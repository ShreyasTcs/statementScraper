import { useState } from 'react'

import { Section } from '../components/ui'
import { LOCALES, useI18n, type Locale, type MessageKey } from '../i18n'
import { getToken, setToken } from '../services/api'
import type { AppConfig } from '../types'

export function SettingsPage({ config, onTokenSaved }: { config: AppConfig | null; onTokenSaved: () => void }) {
  const { t, tc, locale, setLocale } = useI18n()
  const [token, setTokenDraft] = useState(getToken())
  const [saved, setSaved] = useState(false)

  return (
    <main className="mx-auto w-full max-w-[860px] space-y-5 px-4 pb-16 pt-8 xl:px-0">
      <h1 className="text-2xl font-bold text-ink">{t('settings.title')}</h1>

      <Section title={t('nav.language')}>
        <div className="flex flex-wrap gap-2">
          {LOCALES.map((l) => (
            <button
              key={l.code}
              type="button"
              lang={l.code}
              onClick={() => setLocale(l.code as Locale)}
              className={`focus-ring rounded-pill border px-3.5 py-1.5 text-base ${l.code === locale ? 'border-navy bg-navy text-white' : 'border-line bg-white'}`}
            >
              {l.name}
            </button>
          ))}
        </div>
      </Section>

      <Section title={t('settings.engines')} subtitle={t('settings.engines.help')}>
        <ul className="space-y-2">
          {(config?.engines ?? []).map((engine) => (
            <li key={engine.name} className="rounded-control border border-line px-3.5 py-2.5">
              <p className="font-medium text-ink">
                {t(`engine.${engine.name}` as MessageKey)}
                {config?.default_engine === engine.name && <span className="ms-2 text-xs text-navy">({t('settings.default')})</span>}
              </p>
              <p className="text-sm text-ink-subtle">{t(`engine.${engine.name}.help` as MessageKey)}</p>
              {!engine.available && (
                <ul className="mt-1 list-disc ps-5 text-xs text-warn">
                  {engine.problems.map((problem) => (
                    <li key={problem.code}>{tc(problem.code, problem.message)}</li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
        {config && <p className="mt-2 text-xs text-ink-subtle">{t('settings.model', { model: config.model, version: config.prompt_version })}</p>}
      </Section>

      {config && (
        <Section title={t('settings.limits')}>
          <p className="text-base text-ink-muted">
            {t('settings.limits.body', { mb: Math.round(config.max_file_size_bytes / (1024 * 1024)), pages: config.max_pages })}
          </p>
          <p className="mt-1 text-base text-ink-muted">
            {config.retention_days > 0 ? t('settings.retention', { days: config.retention_days }) : t('settings.retention.off')}
          </p>
        </Section>
      )}

      <Section title={t('settings.auth')} subtitle={config?.auth_mode === 'none' ? undefined : t('settings.auth.help')}>
        {config?.auth_mode === 'none' ? (
          <p className="text-base text-warn">{t('settings.auth.none')}</p>
        ) : (
          <form
            className="flex flex-wrap gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              setToken(token.trim())
              setSaved(true)
              onTokenSaved()
            }}
          >
            <input
              type="password"
              autoComplete="off"
              className="field-input h-9 max-w-[420px] flex-1"
              value={token}
              onChange={(event) => {
                setSaved(false)
                setTokenDraft(event.target.value)
              }}
              aria-label={t('settings.auth')}
              dir="ltr"
            />
            <button type="submit" className="btn-primary focus-ring h-9 px-4 text-base">{t('common.save')}</button>
            {saved && <span className="self-center text-sm text-ok">{t('settings.auth.saved')}</span>}
          </form>
        )}
      </Section>
    </main>
  )
}
