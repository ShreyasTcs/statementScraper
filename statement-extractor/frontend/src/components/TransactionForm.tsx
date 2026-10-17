import { useState } from 'react'

import { useI18n, type MessageKey } from '../i18n'
import type { Transaction, TransactionInput } from '../types'
import { Modal } from './ui'

const FIELDS: { key: keyof TransactionInput; label: MessageKey; wide?: boolean; dir?: 'ltr' | 'auto' }[] = [
  { key: 'date', label: 'columns.date', dir: 'ltr' },
  { key: 'value_date', label: 'columns.value_date', dir: 'ltr' },
  { key: 'description', label: 'columns.description', wide: true, dir: 'auto' },
  { key: 'reference', label: 'columns.reference', dir: 'auto' },
  { key: 'currency', label: 'columns.currency', dir: 'ltr' },
  { key: 'debit', label: 'columns.debit', dir: 'ltr' },
  { key: 'credit', label: 'columns.credit', dir: 'ltr' },
  { key: 'amount', label: 'table.col.amount', dir: 'ltr' },
  { key: 'balance', label: 'columns.balance', dir: 'ltr' },
]

type Draft = Record<string, string>

function initial(row: Partial<Transaction> | null): Draft {
  const out: Draft = {}
  for (const f of FIELDS) {
    const value = row?.[f.key as keyof Transaction]
    out[f.key] = typeof value === 'string' ? value : ''
  }
  return out
}

interface Props {
  mode: 'edit' | 'add'
  row: Partial<Transaction> | null
  maxIndex: number
  defaultAfter?: number
  onCancel: () => void
  /** Returns an error message, or null when saved. */
  onSubmit: (input: TransactionInput, afterIndex: number | null) => Promise<string | null>
}

export function TransactionForm({ mode, row, maxIndex, defaultAfter, onCancel, onSubmit }: Props) {
  const { t } = useI18n()
  const [draft, setDraft] = useState<Draft>(() => initial(row))
  const [after, setAfter] = useState<number>(defaultAfter ?? maxIndex)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const original = initial(row)

  const submit = async () => {
    const input: TransactionInput = {}
    for (const f of FIELDS) {
      const value = draft[f.key].trim()
      // Edits send only what changed; null clears an attribute.
      if (mode === 'add' ? value : value !== original[f.key]) {
        ;(input as Record<string, string | null>)[f.key] = value || null
      }
    }
    setBusy(true)
    setError(null)
    const problem = await onSubmit(input, mode === 'add' ? after : null)
    setBusy(false)
    if (problem) setError(problem)
  }

  return (
    <Modal
      title={t(mode === 'edit' ? 'table.edit.title' : 'table.add.title')}
      onClose={onCancel}
      footer={
        <>
          <button type="button" className="btn-ghost focus-ring h-9 px-4 text-base" onClick={onCancel}>
            {t('common.cancel')}
          </button>
          <button type="button" disabled={busy} className="btn-primary focus-ring h-9 px-4 text-base" onClick={() => void submit()}>
            {t('common.save')}
          </button>
        </>
      }
    >
      <p className="mb-3 text-sm text-ink-subtle">{t('table.edit.help')}</p>
      <form
        className="grid gap-3 sm:grid-cols-2"
        onSubmit={(event) => {
          event.preventDefault()
          void submit()
        }}
      >
        {FIELDS.map((f) => (
          <label key={f.key} className={`block text-sm text-ink-muted ${f.wide ? 'sm:col-span-2' : ''}`}>
            {t(f.label)}
            <input
              className="field-input mt-1"
              dir={f.dir}
              value={draft[f.key]}
              onChange={(event) => setDraft({ ...draft, [f.key]: event.target.value })}
            />
          </label>
        ))}
        {mode === 'add' && (
          <label className="block text-sm text-ink-muted">
            {t('table.edit.after')}
            <input
              className="field-input mt-1"
              type="number"
              min={0}
              max={maxIndex}
              value={after}
              onChange={(event) => setAfter(Math.max(0, Math.min(maxIndex, Number(event.target.value) || 0)))}
            />
          </label>
        )}
        <button type="submit" className="hidden" />
      </form>
      {error && <p className="mt-3 text-sm text-bad" role="alert">{error}</p>}
    </Modal>
  )
}
