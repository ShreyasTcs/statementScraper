import { useState } from 'react'

import { useI18n, type MessageKey } from '../i18n'
import type { HeaderField } from '../types'
import { Amount, Original, StatusBadge } from './ui'

interface Props {
  field: HeaderField
  currency?: string | null
  onSave: (value: string | null) => Promise<string | null>
  onConfirm: () => Promise<string | null>
}

function fieldLabel(t: (k: MessageKey) => string, field: HeaderField): string {
  const key = `fields.${field.name}` as MessageKey
  const label = t(key)
  return label === key ? field.label : label
}

export function FieldCard({ field, currency, onSave, onConfirm }: Props) {
  const { t, tm, date } = useI18n()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(field.value ?? '')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const label = fieldLabel(t, field)

  const run = async (action: () => Promise<string | null>) => {
    setBusy(true)
    setError(null)
    const problem = await action()
    setBusy(false)
    if (problem) setError(problem)
    else setEditing(false)
  }

  if (editing) {
    const placeholder =
      field.value_type === 'money' ? t('field.placeholder.money') : field.value_type === 'date' ? t('field.placeholder.date') : ''
    return (
      <form
        className="rounded-control border border-navy bg-white px-3.5 py-3"
        onSubmit={(event) => {
          event.preventDefault()
          void run(() => onSave(draft.trim() ? draft : null))
        }}
      >
        <label className="block text-sm text-ink-muted">
          {label}
          <input
            className="field-input mt-1"
            value={draft}
            placeholder={placeholder}
            onChange={(event) => setDraft(event.target.value)}
            dir="auto"
            autoFocus
          />
        </label>
        {error && <p className="mt-1 text-xs text-bad">{error}</p>}
        <div className="mt-2 flex flex-wrap gap-2">
          <button type="submit" disabled={busy} className="btn-primary focus-ring h-8 px-3 text-sm">
            {t('common.save')}
          </button>
          {field.found && (
            <button type="button" disabled={busy} className="btn-ghost focus-ring h-8 px-3 text-sm" onClick={() => void run(() => onSave(null))}>
              {t('field.clear')}
            </button>
          )}
          <button type="button" className="btn-ghost focus-ring h-8 px-3 text-sm" onClick={() => setEditing(false)}>
            {t('common.cancel')}
          </button>
        </div>
      </form>
    )
  }

  const actions = (
    <div className="mt-2 flex gap-3 text-xs">
      <button type="button" className="focus-ring rounded text-navy hover:underline" onClick={() => { setDraft(field.value ?? ''); setEditing(true) }}>
        {t('field.edit')}
      </button>
      {field.found && !field.corrected && field.status !== 'high' && (
        <button type="button" disabled={busy} className="focus-ring rounded text-navy hover:underline" onClick={() => void run(onConfirm)}>
          {t('field.confirm')}
        </button>
      )}
    </div>
  )

  if (!field.found) {
    return (
      <div className={`rounded-control border border-dashed px-3.5 py-3 ${field.required ? 'border-bad/40 bg-bad-tint' : 'border-line bg-page/60'}`}>
        <div className="flex items-center justify-between gap-2">
          <p className="text-sm text-ink-muted">{label}</p>
          {field.required && <span className="text-2xs font-semibold uppercase tracking-wide text-bad">{t('field.required')}</span>}
        </div>
        <p className={`mt-1 text-md ${field.required ? 'font-medium text-bad' : 'text-ink-subtle'}`}>{t('field.notFound')}</p>
        {actions}
      </div>
    )
  }

  const shown =
    field.value_type === 'money' ? <Amount value={field.normalized} currency={currency} />
      : field.value_type === 'date' && field.normalized ? date(field.normalized)
        : <Original>{field.value}</Original>
  const provenance = field.provenance ? t(`field.provenance.${field.provenance}` as MessageKey) : null
  const reasons = field.reason_codes.map((r) => tm(r)).join(' · ')

  return (
    <div className="rounded-control border border-line bg-white px-3.5 py-3" title={reasons}>
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm text-ink-muted">{label}</p>
        <StatusBadge status={field.status} confidence={field.confidence} />
      </div>
      <p className="mt-1 break-words text-md font-semibold text-ink">{shown}</p>
      <p className="mt-0.5 text-2xs text-ink-subtle">
        {field.source?.page ? `${t('common.page', { page: field.source.page })} · ` : ''}
        {field.source?.anchor ? t('field.near', { anchor: field.source.anchor }) : t('field.layout')}
        {provenance ? ` · ${provenance}` : ''}
      </p>
      {field.corrected && field.original_value !== null && field.original_value !== field.value && (
        <p className="mt-1 text-xs text-ink-subtle">{t('field.original', { value: field.original_value ?? '—' })}</p>
      )}
      {field.validation !== 'valid' && field.validation_code && (
        <p className="mt-1 text-xs text-warn">{tm({ code: field.validation_code, params: {}, text: field.validation_message ?? '' })}</p>
      )}
      {field.alternatives.length > 0 && (
        <p className="mt-1 text-xs text-ink-subtle">{t('field.alsoSeen', { values: field.alternatives.slice(0, 2).join(', ') })}</p>
      )}
      {error && <p className="mt-1 text-xs text-bad">{error}</p>}
      {actions}
    </div>
  )
}
