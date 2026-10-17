import { useEffect, useRef, type ReactNode } from 'react'

import { useI18n } from '../i18n'
import type { CompletenessStatus, Status } from '../types'
import { AlertIcon, CheckIcon, InfoIcon } from './icons'

export function Section({
  title,
  subtitle,
  actions,
  children,
  flush = false,
}: {
  title: string
  subtitle?: ReactNode
  actions?: ReactNode
  children: ReactNode
  flush?: boolean
}) {
  return (
    <section className="card overflow-hidden">
      <div className="flex flex-wrap items-start gap-3 px-5 pb-3 pt-4">
        <div className="min-w-0 flex-1">
          <h2 className="text-lg font-semibold text-ink">{title}</h2>
          {subtitle && <p className="mt-0.5 text-sm text-ink-subtle">{subtitle}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
      <div className={flush ? '' : 'px-5 pb-5'}>{children}</div>
    </section>
  )
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="rounded-control border border-line bg-white px-3.5 py-2.5">
      <p className="text-xs text-ink-subtle">{label}</p>
      <p className="mt-0.5 text-lg font-semibold tabular-nums text-ink">{value}</p>
      {hint && <p className="text-2xs text-ink-subtle">{hint}</p>}
    </div>
  )
}

/** Amounts stay left-to-right (sign, digits) even inside a right-to-left page. */
export function Amount({ value, currency, className = '' }: { value: string | null | undefined; currency?: string | null; className?: string }) {
  const { money } = useI18n()
  if (value === null || value === undefined || value === '') return <span className="text-ink-subtle">—</span>
  return (
    <span dir="ltr" className={`tabular-nums ${className}`}>
      {money(value, currency)}
    </span>
  )
}

/** Text copied from the statement: shown exactly as extracted, in its own direction. */
export function Original({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <bdi dir="auto" className={className}>
      {children}
    </bdi>
  )
}

const STATUS_STYLE: Record<Status, { className: string; Icon: typeof CheckIcon }> = {
  high: { className: 'bg-ok-bg text-ok', Icon: CheckIcon },
  review: { className: 'bg-warn-bg text-warn', Icon: InfoIcon },
  needs_review: { className: 'bg-bad-bg text-bad', Icon: AlertIcon },
  not_found: { className: 'bg-page text-ink-subtle', Icon: AlertIcon },
}

export function StatusBadge({ status, confidence, className = '' }: { status: Status; confidence?: number | null; className?: string }) {
  const { t, percent } = useI18n()
  const style = STATUS_STYLE[status]
  return (
    <span className={`inline-flex h-5 shrink-0 items-center gap-1 rounded-md px-1.5 text-xs font-semibold ${style.className} ${className}`}>
      <style.Icon size={11} />
      {t(`status.${status}`)}
      {confidence != null && status !== 'not_found' ? ` ${percent(confidence)}` : ''}
    </span>
  )
}

const COMPLETENESS_STYLE: Record<CompletenessStatus, string> = {
  complete: 'bg-ok-bg text-ok border-ok/30',
  partial: 'bg-bad-bg text-bad border-bad/30',
  needs_review: 'bg-warn-bg text-warn border-warn/30',
  failed: 'bg-bad-bg text-bad border-bad/30',
}

export function CompletenessBadge({ status }: { status: CompletenessStatus | null }) {
  const { t } = useI18n()
  if (!status) return <span className="text-ink-subtle">—</span>
  return (
    <span className={`inline-flex h-6 items-center rounded-pill border px-2.5 text-xs font-semibold ${COMPLETENESS_STYLE[status]}`}>
      {t(`completeness.${status}`)}
    </span>
  )
}

export function ErrorBanner({ message, onDismiss }: { message: string; onDismiss?: () => void }) {
  const { t } = useI18n()
  return (
    <div role="alert" className="flex items-start gap-2 rounded-control border border-bad/30 bg-bad-bg px-3.5 py-3 text-base text-bad">
      <AlertIcon size={16} className="mt-0.5 shrink-0" />
      <span className="flex-1">{message}</span>
      {onDismiss && (
        <button type="button" onClick={onDismiss} className="focus-ring rounded px-1 text-sm underline">
          {t('common.close')}
        </button>
      )}
    </div>
  )
}

export function Spinner({ size = 16 }: { size?: number }) {
  return (
    <span
      aria-hidden="true"
      className="inline-block animate-spin rounded-full border-2 border-navy/30 border-t-navy"
      style={{ width: size, height: size }}
    />
  )
}

export function Modal({ title, onClose, children, footer }: { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode }) {
  const { t } = useI18n()
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    ref.current?.querySelector<HTMLElement>('input, select, textarea, button')?.focus()
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 p-4" role="dialog" aria-modal="true" aria-label={title}>
      <div ref={ref} className="card max-h-[90vh] w-full max-w-[640px] overflow-y-auto shadow-pop">
        <div className="flex items-center justify-between border-b border-line px-5 py-3">
          <h2 className="text-lg font-semibold text-ink">{title}</h2>
          <button type="button" onClick={onClose} className="focus-ring rounded px-2 py-1 text-sm text-ink-subtle hover:bg-page">
            {t('common.close')}
          </button>
        </div>
        <div className="px-5 py-4">{children}</div>
        {footer && <div className="flex justify-end gap-2 border-t border-line px-5 py-3">{footer}</div>}
      </div>
    </div>
  )
}
