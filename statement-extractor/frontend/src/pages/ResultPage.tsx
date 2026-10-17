import { useCallback, useState } from 'react'

import { ChecksList, CompletenessPanel } from '../components/Checks'
import { FieldCard } from '../components/FieldCard'
import { AlertIcon, CheckIcon, InfoIcon } from '../components/icons'
import { TransactionsTable, type TableActions } from '../components/TransactionsTable'
import { Amount, ErrorBanner, Original, Section, Stat } from '../components/ui'
import { useI18n, type MessageKey } from '../i18n'
import { ApiRequestError, api } from '../services/api'
import type { Account, Correction, Engine, ExtractionResult, Transaction, UnresolvedLine } from '../types'

const TONES = {
  ready: { box: 'border-ok/30 bg-ok-bg', text: 'text-ok', Icon: CheckIcon },
  review: { box: 'border-warn/30 bg-warn-bg', text: 'text-warn', Icon: InfoIcon },
  issue: { box: 'border-bad/30 bg-bad-bg', text: 'text-bad', Icon: AlertIcon },
} as const

interface Props {
  result: ExtractionResult
  onChange: (result: ExtractionResult) => void
  onReprocessed: () => void
}

export function ResultPage({ result, onChange, onReprocessed }: Props) {
  const { t, tm, tc, number, percent } = useI18n()
  const [accountKey, setAccountKey] = useState(result.accounts[0]?.key ?? 'A1')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [prefill, setPrefill] = useState<{ input: Partial<Transaction>; page: number; unresolved: UnresolvedLine } | null>(null)
  const [corrections, setCorrections] = useState<Correction[] | null>(null)
  const [reprocessEngine, setReprocessEngine] = useState<Engine>('local')

  const id = result.document.id
  const account: Account | undefined = result.accounts.find((a) => a.key === accountKey) ?? result.accounts[0]
  const tone = TONES[result.summary.tone]

  const message = useCallback(
    (caught: unknown) => (caught instanceof ApiRequestError ? tc(caught.code, caught.message, caught.params) : t('msg.network')),
    [t, tc],
  )

  /** Run a mutation that returns the updated result; returns an error message or null. */
  const mutate = useCallback(
    async (call: () => Promise<ExtractionResult>): Promise<string | null> => {
      try {
        const next = await call()
        onChange(next)
        setCorrections(null)
        return null
      } catch (caught) {
        return message(caught)
      }
    },
    [onChange, message],
  )

  const actions: TableActions = {
    update: (row, input) => mutate(() => api.updateTransaction(id, row.id!, input)),
    create: async (input, after) => {
      const problem = await mutate(() => api.createTransaction(id, { ...input, account: account?.key ?? 'A1', after_index: after }))
      if (!problem && prefill) {
        await mutate(() => api.dismissUnresolved(id, account?.key ?? 'A1', prefill.unresolved.index, true))
      }
      return problem
    },
    remove: (row) => mutate(() => api.deleteTransaction(id, row.id!)),
    restore: (row) => mutate(() => api.restoreTransaction(id, row.id!)),
  }

  const download = async (kind: 'csv' | 'json') => {
    setError(null)
    try {
      const base = result.document.filename.replace(/\.[^.]+$/, '')
      if (kind === 'csv') await api.downloadCsv(id, `${base}-transactions.csv`)
      else await api.downloadJson(id, `${base}.json`)
    } catch (caught) {
      setError(message(caught))
    }
  }

  const openOriginal = async () => {
    setError(null)
    try {
      const url = await api.previewUrl(id)
      window.open(url, '_blank', 'noopener')
    } catch (caught) {
      setError(message(caught))
    }
  }

  const simple = async (call: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await call()
    } catch (caught) {
      setError(message(caught))
    }
    setBusy(false)
  }

  const loadCorrections = () => simple(async () => setCorrections(await api.corrections(id)))

  const conv = result.conventions
  const numberExample = `1${conv.group === ' ' ? ' ' : conv.group}234${conv.decimal}56`
  const live = account ? account.transactions.filter((r) => !r.deleted) : []

  return (
    <main className="mx-auto w-full max-w-[1200px] space-y-5 px-4 pb-16 pt-8 xl:px-0">
      {/* Outcome */}
      <div className={`flex flex-wrap items-center gap-x-6 gap-y-3 rounded-card border px-5 py-4 ${tone.box}`}>
        <tone.Icon size={22} className={tone.text} />
        <div className="min-w-[240px] flex-1">
          <h1 className={`text-xl font-bold ${tone.text}`}>{tm(result.summary.headline)}</h1>
          <p className="text-base text-ink-muted">{tm(result.summary.detail)}</p>
          <p className="mt-0.5 truncate text-sm text-ink-subtle"><bdi>{result.document.filename}</bdi></p>
        </div>
        <dl className="flex flex-wrap gap-x-7 gap-y-2 text-sm">
          {[
            [t('result.stats.details'), `${number(result.summary.header_found)} / ${number(result.summary.header_total)}`],
            [t('result.stats.transactions'), number(result.summary.rows)],
            [t('result.stats.reconciled'), `${number(result.summary.rows_reconciled)} / ${number(result.summary.rows)}`],
            [t('result.stats.pages'), number(result.document.page_count)],
            ...(result.summary.overall_confidence != null ? [[t('result.stats.confidence'), percent(result.summary.overall_confidence)]] : []),
          ].map(([label, value]) => (
            <div key={label}>
              <dt className="text-ink-subtle">{label}</dt>
              <dd className="text-lg font-semibold tabular-nums text-ink">{value}</dd>
            </div>
          ))}
        </dl>
      </div>

      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}

      {/* Engine */}
      {result.engine.fallback_reason ? (
        <div className="flex items-start gap-2 rounded-control border border-warn/40 bg-warn-bg px-4 py-3 text-base text-warn">
          <AlertIcon size={16} className="mt-0.5 shrink-0" />
          <span>{t('engine.fallback', { reason: tc(result.engine.fallback_reason, result.engine.fallback_reason) })}</span>
        </div>
      ) : null}
      <div className="flex flex-wrap items-center gap-2 text-sm">
        {result.engine.used && (
          <span className="rounded-pill border border-line bg-white px-3 py-1 text-ink-muted">
            {t('engine.used', { engine: result.engine.used })}
            {result.engine.model ? ` · ${result.engine.model}` : ''}
          </span>
        )}
        {result.engine.disagreement_count > 0 && (
          <span className="rounded-pill border border-warn/40 bg-warn-bg px-3 py-1 text-warn">
            {t('engine.disagreements', { count: result.engine.disagreement_count })}
          </span>
        )}
        <span className="rounded-pill border border-line bg-white px-3 py-1 text-ink-muted">
          {t('result.conventions.numbers', { example: numberExample })} {conv.confident ? '' : t('result.conventions.assumed')}
        </span>
        <span className="rounded-pill border border-line bg-white px-3 py-1 text-ink-muted">
          {t('result.conventions.dates', { order: t(`result.conventions.${conv.date_order}` as MessageKey) })}
        </span>
        {result.corrected && (
          <span className="rounded-pill border border-navy/30 bg-navy-soft px-3 py-1 text-navy">
            {t('result.corrected')} · {t('result.corrections', { count: result.corrections })}
          </span>
        )}
      </div>

      {/* Actions */}
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className="btn-primary focus-ring h-9 px-4 text-base" disabled={!result.accounts.length} onClick={() => void download('csv')}>
          {t('result.download.csv')}
        </button>
        <button type="button" className="btn-ghost focus-ring h-9 px-4 text-base" onClick={() => void download('json')}>
          {t('result.download.json')}
        </button>
        <button type="button" className="btn-ghost focus-ring h-9 px-4 text-base" onClick={() => void openOriginal()}>
          {t('result.openOriginal')}
        </button>
        {result.accounts.length > 0 && (
          <button type="button" disabled={busy} className="btn-ghost focus-ring h-9 px-4 text-base" onClick={() => void simple(async () => onChange(await api.revalidate(id)))}>
            {t('result.revalidate')}
          </button>
        )}
        <span className="ms-auto flex items-center gap-2">
          <select className="field-input h-9 w-auto" value={reprocessEngine} onChange={(event) => setReprocessEngine(event.target.value as Engine)} aria-label={t('result.reprocess')}>
            {(['local', 'groq', 'hybrid'] as Engine[]).map((engine) => (
              <option key={engine} value={engine}>{t(`engine.${engine}` as MessageKey)}</option>
            ))}
          </select>
          <button
            type="button"
            disabled={busy}
            className="btn-ghost focus-ring h-9 px-4 text-base"
            onClick={() => void simple(async () => { await api.reprocess(id, reprocessEngine); onReprocessed() })}
          >
            {t('result.reprocess')}
          </button>
        </span>
      </div>

      <CompletenessPanel completeness={result.completeness} />

      {result.document.ocr_used && (
        <div className="flex items-start gap-2 rounded-control border border-warn/30 bg-warn-bg px-4 py-3 text-base text-warn">
          <InfoIcon size={16} className="mt-0.5 shrink-0" />
          <span>{t('result.ocrNotice')}</span>
        </div>
      )}

      {result.accounts.length > 1 && (
        <div role="tablist" aria-label={t('result.accounts')} className="flex flex-wrap gap-2">
          {result.accounts.map((a) => (
            <button
              key={a.key}
              type="button"
              role="tab"
              aria-selected={a.key === account?.key}
              onClick={() => setAccountKey(a.key)}
              className={`focus-ring rounded-pill border px-3.5 py-1.5 text-base ${
                a.key === account?.key ? 'border-navy bg-navy text-white' : 'border-line bg-white text-ink'
              }`}
            >
              {t('result.account', { key: a.key })}
              {a.label ? <span className="ms-1 opacity-80"><bdi>{a.label}</bdi></span> : null}
              {a.currency ? <span className="ms-1 opacity-80">· {a.currency}</span> : null}
            </button>
          ))}
        </div>
      )}

      {account && (
        <>
          <Section
            title={t('result.details')}
            subtitle={t('result.detailsFound', { found: account.header.filter((f) => f.found).length, total: account.header.length })}
          >
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {account.header.map((field) => (
                <FieldCard
                  key={field.id ?? field.name}
                  field={field}
                  currency={account.currency}
                  onSave={(value) => mutate(() => api.updateField(id, field.id!, { value }))}
                  onConfirm={() => mutate(() => api.updateField(id, field.id!, { confirm: true }))}
                />
              ))}
            </div>
          </Section>

          <Section
            title={t('result.reconciliation')}
            subtitle={
              <>
                {t(`result.mode.${account.totals.mode}` as MessageKey)}{' '}
                {account.totals.order === 'newest_first' ? t('result.order.newest_first') : ''}
              </>
            }
          >
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
              <Stat
                label={t('result.opening')}
                value={<Amount value={account.totals.opening_balance} />}
                hint={account.totals.opening_source ? t(`result.source.${account.totals.opening_source}` as MessageKey) : t('common.notAvailable')}
              />
              <Stat
                label={t('result.credits')}
                value={<Amount value={account.totals.total_credits} />}
                hint={account.totals.printed_credits ? t('result.printed', { value: account.totals.printed_credits }) : t('result.fromRows')}
              />
              <Stat
                label={t('result.debits')}
                value={<Amount value={account.totals.total_debits} />}
                hint={account.totals.printed_debits ? t('result.printed', { value: account.totals.printed_debits }) : t('result.fromRows')}
              />
              <Stat label={t('result.net')} value={<Amount value={account.totals.net_movement} />} />
              <Stat
                label={t('result.closing')}
                value={<Amount value={account.totals.closing_balance} />}
                hint={account.totals.closing_source ? t(`result.source.${account.totals.closing_source}` as MessageKey) : t('common.notAvailable')}
              />
            </div>
            {account.currency && <p className="mt-2 text-sm text-ink-subtle">{t('result.currency', { code: account.currency })}</p>}
            <div className="mt-4">
              <ChecksList checks={account.checks} />
            </div>
          </Section>

          <Section title={t('result.columns')} subtitle={t('result.columns.help')}>
            <div className="flex flex-wrap gap-2">
              {account.columns.map((column) => (
                <span
                  key={column.key}
                  className={`inline-flex items-center gap-1.5 rounded-pill border px-3 py-1 text-sm ${
                    column.found ? 'border-ok/30 bg-ok-bg text-ok' : column.required ? 'border-bad/40 bg-bad-tint text-bad' : 'border-dashed border-line bg-page/60 text-ink-subtle'
                  }`}
                >
                  {column.found ? <CheckIcon size={12} /> : <AlertIcon size={12} />}
                  <span className="font-medium">{t(`columns.${column.key}` as MessageKey)}</span>
                  <span className="text-xs opacity-80">
                    {column.found ? <Original>“{column.printed_as}”</Original> : column.required ? t('result.column.requiredMissing') : t('result.column.notFound')}
                  </span>
                </span>
              ))}
            </div>
            {account.note_codes.length > 0 && (
              <ul className="mt-3 list-disc ps-5 text-sm text-ink-muted">
                {account.note_codes.map((note, n) => (
                  <li key={n}>{tm(note)}</li>
                ))}
              </ul>
            )}
          </Section>

          {account.unresolved.length > 0 && (
            <Section title={t('result.unresolved')} subtitle={t('result.unresolved.help')}>
              <ul className="divide-y divide-line rounded-control border border-line">
                {account.unresolved.map((line) => (
                  <li key={line.index} className={`flex flex-wrap items-start gap-3 px-3.5 py-2.5 ${line.dismissed ? 'opacity-60' : ''}`}>
                    <div className="min-w-0 flex-1">
                      <pre dir="auto" className="whitespace-pre-wrap break-words font-mono text-xs text-ink">{line.text}</pre>
                      <p className="text-xs text-ink-subtle">
                        {t('common.page', { page: line.page })} · {t(`result.unresolved.reason.${line.reason}` as MessageKey)}
                        {line.dismissed ? ` · ${t('result.unresolved.dismissed')}` : ''}
                      </p>
                    </div>
                    {!line.dismissed ? (
                      <div className="flex gap-2">
                        <button
                          type="button"
                          className="btn-ghost focus-ring h-8 px-3 text-sm"
                          onClick={() => setPrefill({ input: { amount: line.amounts[0] ?? null, description: null }, page: line.page, unresolved: line })}
                        >
                          {t('result.unresolved.add')}
                        </button>
                        <button
                          type="button"
                          className="btn-ghost focus-ring h-8 px-3 text-sm"
                          onClick={() => void mutate(() => api.dismissUnresolved(id, account.key, line.index, true)).then((p) => p && setError(p))}
                        >
                          {t('result.unresolved.dismiss')}
                        </button>
                      </div>
                    ) : (
                      <button
                        type="button"
                        className="btn-ghost focus-ring h-8 px-3 text-sm"
                        onClick={() => void mutate(() => api.dismissUnresolved(id, account.key, line.index, false)).then((p) => p && setError(p))}
                      >
                        {t('result.unresolved.undismiss')}
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            </Section>
          )}

          <Section
            title={t('table.title')}
            subtitle={t('table.subtitle', {
              rows: live.length,
              high: live.filter((r) => r.status === 'high').length,
              review: live.filter((r) => r.status !== 'high').length,
            })}
            flush
          >
            {account.empty_table_text && !live.length && (
              <p className="px-5 pb-3 text-sm text-ink-muted">{t('result.empty', { text: account.empty_table_text })}</p>
            )}
            <TransactionsTable
              rows={account.transactions}
              hasBalance={account.has_balance_column}
              currency={account.currency}
              actions={actions}
              prefill={prefill}
              onPrefillDone={() => setPrefill(null)}
            />
          </Section>

          <Section
            title={t('result.history')}
            actions={
              corrections === null ? (
                <button type="button" className="btn-ghost focus-ring h-8 px-3 text-sm" onClick={() => void loadCorrections()}>
                  {t('result.corrections', { count: result.corrections })}
                </button>
              ) : undefined
            }
          >
            {corrections === null ? null : corrections.length === 0 ? (
              <p className="text-sm text-ink-subtle">{t('result.history.empty')}</p>
            ) : (
              <ul className="space-y-1 text-sm">
                {corrections.map((c) => (
                  <CorrectionLine key={c.id} correction={c} />
                ))}
              </ul>
            )}
          </Section>
        </>
      )}
    </main>
  )
}

function CorrectionLine({ correction }: { correction: Correction }) {
  const { t, dateTime } = useI18n()
  const key = `result.history.action.${correction.action}` as MessageKey
  const attribute = correction.attribute
    ? t(`columns.${correction.attribute}` as MessageKey) === `columns.${correction.attribute}`
      ? correction.attribute
      : t(`columns.${correction.attribute}` as MessageKey)
    : ''
  const action = t(key) === key ? correction.action : t(key, { attribute })
  return (
    <li className="flex flex-wrap gap-x-2 text-ink-muted">
      <span className="tabular-nums text-ink-subtle">{dateTime(correction.created_at)}</span>
      <span className="font-medium text-ink">{correction.user}</span>
      <span>{action}</span>
      {correction.action === 'update' && (
        <span>
          <bdi dir="auto" className="text-bad line-through">{correction.old_value ?? '—'}</bdi> →{' '}
          <bdi dir="auto" className="text-ok">{correction.new_value ?? '—'}</bdi>
        </span>
      )}
    </li>
  )
}
