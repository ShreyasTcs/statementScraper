import { useI18n, type MessageKey } from '../i18n'
import type { Check, Completeness } from '../types'
import { AlertIcon, CheckIcon, InfoIcon } from './icons'
import { CompletenessBadge } from './ui'

const CHECK_STYLE: Record<Check['status'], string> = {
  pass: 'bg-ok-bg text-ok',
  fail: 'bg-bad-bg text-bad',
  warn: 'bg-warn-bg text-warn',
  skipped: 'bg-page text-ink-subtle',
  info: 'bg-navy-soft text-navy',
}

export function ChecksList({ checks }: { checks: Check[] }) {
  const { t, tm } = useI18n()
  return (
    <ul className="divide-y divide-line rounded-control border border-line">
      {checks.map((check) => {
        const labelKey = `check.${check.key}` as MessageKey
        const label = t(labelKey) === labelKey ? check.label : t(labelKey)
        return (
          <li key={check.key} className="flex flex-wrap items-start gap-x-3 gap-y-1 px-3.5 py-2.5">
            <span className={`mt-0.5 inline-flex h-5 min-w-[68px] shrink-0 items-center justify-center rounded-md px-1.5 text-xs font-semibold ${CHECK_STYLE[check.status]}`}>
              {t(`checks.${check.status}`)}
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-base font-medium text-ink">{label}</p>
              <p className="text-sm text-ink-muted">
                {check.detail_code ? tm({ code: check.detail_code, params: check.params, text: check.detail }) : check.detail}
              </p>
            </div>
          </li>
        )
      })}
    </ul>
  )
}

const TONE = {
  complete: { box: 'border-ok/30 bg-ok-bg', text: 'text-ok', Icon: CheckIcon },
  needs_review: { box: 'border-warn/30 bg-warn-bg', text: 'text-warn', Icon: InfoIcon },
  partial: { box: 'border-bad/30 bg-bad-bg', text: 'text-bad', Icon: AlertIcon },
  failed: { box: 'border-bad/30 bg-bad-bg', text: 'text-bad', Icon: AlertIcon },
} as const

export function CompletenessPanel({ completeness }: { completeness: Completeness }) {
  const { t, tm } = useI18n()
  const tone = TONE[completeness.status]
  const metrics = completeness.metrics as { pages_detected?: number; pages_processed?: number; chunks_total?: number; chunks_failed?: number }
  return (
    <div className={`rounded-card border px-5 py-4 ${tone.box}`}>
      <div className="flex flex-wrap items-center gap-3">
        <tone.Icon size={20} className={tone.text} />
        <h2 className={`text-lg font-semibold ${tone.text}`}>{t('completeness.title')}</h2>
        <CompletenessBadge status={completeness.status} />
      </div>
      <p className="mt-1 text-sm text-ink-muted">{t(`completeness.${completeness.status}.help` as MessageKey)}</p>
      {completeness.reasons.length > 0 && (
        <ul className="mt-2 list-disc space-y-0.5 ps-5 text-sm text-ink">
          {completeness.reasons.map((reason, index) => (
            <li key={`${reason.code}-${index}`}>{tm(reason)}</li>
          ))}
        </ul>
      )}
      <p className="mt-2 text-xs text-ink-subtle">
        {metrics.pages_detected ? t('completeness.pages', { done: metrics.pages_processed ?? 0, total: metrics.pages_detected }) : ''}
        {metrics.chunks_total ? ` · ${t('completeness.chunks', { failed: metrics.chunks_failed ?? 0, total: metrics.chunks_total })}` : ''}
      </p>
    </div>
  )
}
