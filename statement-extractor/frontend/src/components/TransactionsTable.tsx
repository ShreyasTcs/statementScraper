import { Fragment, useMemo, useState, type ReactNode } from 'react'

import { useI18n, type MessageKey } from '../i18n'
import type { Transaction, TransactionInput } from '../types'
import { TransactionForm } from './TransactionForm'
import { Amount, Original } from './ui'

type Filter = 'all' | 'attention' | 'mismatch' | 'unverified' | 'corrected' | 'deleted'
type Sort = 'index' | 'date' | 'amount' | 'confidence'

export interface TableActions {
  update: (row: Transaction, input: TransactionInput) => Promise<string | null>
  create: (input: TransactionInput, afterIndex: number | null) => Promise<string | null>
  remove: (row: Transaction) => Promise<string | null>
  restore: (row: Transaction) => Promise<string | null>
}

interface Props {
  rows: Transaction[]
  hasBalance: boolean
  currency: string | null
  actions: TableActions
  /** A row to pre-fill the "add" form with (e.g. from an unresolved line). */
  prefill?: { input: Partial<Transaction>; page: number } | null
  onPrefillDone?: () => void
}

const CHECK_STYLE = {
  ok: 'bg-ok-bg text-ok',
  mismatch: 'bg-bad-bg text-bad',
  unchecked: 'bg-page text-ink-subtle',
} as const

function unverified(row: Transaction): string[] {
  const verified = row.evidence?.verified
  if (!verified || typeof verified !== 'object') return []
  return Object.entries(verified).filter(([, ok]) => !ok).map(([key]) => key)
}

function magnitude(row: Transaction): number {
  return Number(row.debit ?? row.credit ?? row.amount ?? 0)
}

export function TransactionsTable({ rows, hasBalance, currency, actions, prefill, onPrefillDone }: Props) {
  const { t, tm, date, percent } = useI18n()
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const [sort, setSort] = useState<Sort>('index')
  const [open, setOpen] = useState<string | null>(null)
  const [editing, setEditing] = useState<Transaction | null>(null)
  const [adding, setAdding] = useState(false)
  const [rowError, setRowError] = useState<{ id: string; message: string } | null>(null)

  const live = rows.filter((r) => !r.deleted)
  const hasAmount = live.some((r) => r.amount)
  const maxIndex = live.length

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase()
    let out = rows.filter((row) => {
      switch (filter) {
        case 'deleted':
          return row.deleted
        case 'attention':
          return !row.deleted && (row.status !== 'high' || row.balance_check === 'mismatch' || row.issue_codes.length > 0)
        case 'mismatch':
          return !row.deleted && row.balance_check === 'mismatch'
        case 'unverified':
          return !row.deleted && unverified(row).length > 0
        case 'corrected':
          return row.corrected
        default:
          return !row.deleted
      }
    })
    if (needle) {
      out = out.filter((row) =>
        [row.description, row.reference, row.date, row.debit, row.credit, row.amount, row.balance]
          .some((value) => value && value.toLowerCase().includes(needle)),
      )
    }
    const sorted = [...out]
    if (sort === 'date') sorted.sort((a, b) => (a.date ?? '').localeCompare(b.date ?? ''))
    if (sort === 'amount') sorted.sort((a, b) => magnitude(b) - magnitude(a))
    if (sort === 'confidence') sorted.sort((a, b) => (a.confidence ?? 0) - (b.confidence ?? 0))
    return sorted
  }, [rows, query, filter, sort])

  const run = async (row: Transaction, action: () => Promise<string | null>) => {
    setRowError(null)
    const problem = await action()
    if (problem) setRowError({ id: row.id ?? '', message: problem })
  }

  const columns = 6 + (hasAmount ? 1 : 0) + (hasBalance ? 1 : 0)

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 px-5 pb-3">
        <input
          type="search"
          className="field-input h-9 max-w-[320px] flex-1"
          placeholder={t('table.search')}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          aria-label={t('common.search')}
        />
        <select className="field-input h-9 w-auto" value={filter} onChange={(event) => setFilter(event.target.value as Filter)} aria-label={t('table.filter.all')}>
          {(['all', 'attention', 'mismatch', 'unverified', 'corrected', 'deleted'] as Filter[]).map((f) => (
            <option key={f} value={f}>
              {t(`table.filter.${f}` as MessageKey)}
            </option>
          ))}
        </select>
        <select className="field-input h-9 w-auto" value={sort} onChange={(event) => setSort(event.target.value as Sort)} aria-label={t('table.sort')}>
          {(['index', 'date', 'amount', 'confidence'] as Sort[]).map((s) => (
            <option key={s} value={s}>
              {t(`table.sort.${s}` as MessageKey)}
            </option>
          ))}
        </select>
        <button type="button" className="btn-ghost focus-ring ms-auto h-9 px-3.5 text-base" onClick={() => setAdding(true)}>
          {t('table.add')}
        </button>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[860px] border-t border-line text-base">
          <thead className="bg-page text-xs uppercase tracking-wide text-ink-subtle">
            <tr>
              <th className="px-3 py-2 text-start">{t('table.col.index')}</th>
              <th className="px-3 py-2 text-start">{t('table.col.date')}</th>
              <th className="px-3 py-2 text-start">{t('table.col.description')}</th>
              <th className="px-3 py-2 text-end">{t('table.col.debit')}</th>
              <th className="px-3 py-2 text-end">{t('table.col.credit')}</th>
              {hasAmount && <th className="px-3 py-2 text-end">{t('table.col.amount')}</th>}
              {hasBalance && <th className="px-3 py-2 text-end">{t('table.col.balance')}</th>}
              <th className="px-3 py-2 text-start">{t('table.col.check')}</th>
              <th className="px-3 py-2 text-end">{t('table.col.confidence')}</th>
            </tr>
          </thead>
          <tbody>
            {visible.length === 0 && (
              <tr>
                <td colSpan={columns + 1} className="px-5 py-6 text-center text-ink-subtle">
                  {t('table.noRows')}
                </td>
              </tr>
            )}
            {visible.map((row) => {
              const id = row.id ?? String(row.index)
              const expanded = open === id
              const missing = unverified(row)
              return (
                <Fragment key={id}>
                  <tr
                    className={`cursor-pointer border-t border-line align-top hover:bg-page/70 ${row.deleted ? 'opacity-60' : ''} ${
                      row.balance_check === 'mismatch' ? 'bg-bad-tint' : ''
                    }`}
                    onClick={() => setOpen(expanded ? null : id)}
                    aria-expanded={expanded}
                  >
                    <td className="px-3 py-2 tabular-nums text-ink-subtle">{row.deleted ? '–' : row.index}</td>
                    <td className="whitespace-nowrap px-3 py-2 tabular-nums">
                      {row.date ? date(row.date) : <span className="text-bad">—</span>}
                      {row.date_inherited && <span className="block text-2xs text-ink-subtle">{t('table.inherited')}</span>}
                    </td>
                    <td className="px-3 py-2">
                      <Original className="block break-words text-ink">{row.description || '—'}</Original>
                      {row.reference && <Original className="block text-xs text-ink-subtle">{row.reference}</Original>}
                      <span className="mt-0.5 flex flex-wrap gap-1">
                        {row.deleted && <Tag tone="bad">{t('table.deleted')}</Tag>}
                        {row.source_engine === 'user' && <Tag tone="navy">{t('table.userRow')}</Tag>}
                        {row.corrected && row.source_engine !== 'user' && <Tag tone="navy">{t('table.corrected')}</Tag>}
                        {row.direction_inferred && <Tag tone="muted">{t('table.inferred')}</Tag>}
                        {missing.length > 0 && <Tag tone="warn">{t('table.evidence.unverified')}</Tag>}
                        {row.issue_codes.slice(0, 2).map((issue, n) => (
                          <Tag key={n} tone="warn">{tm(issue)}</Tag>
                        ))}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-end"><Amount value={row.debit} /></td>
                    <td className="px-3 py-2 text-end"><Amount value={row.credit} /></td>
                    {hasAmount && <td className="px-3 py-2 text-end"><Amount value={row.amount} /></td>}
                    {hasBalance && <td className="px-3 py-2 text-end"><Amount value={row.balance} /></td>}
                    <td className="px-3 py-2">
                      <span className={`inline-flex h-5 items-center rounded-md px-1.5 text-xs font-semibold ${CHECK_STYLE[row.balance_check]}`}>
                        {t(`table.check.${row.balance_check}` as MessageKey)}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-end tabular-nums text-ink-muted">{row.confidence != null ? percent(row.confidence) : '—'}</td>
                  </tr>
                  {expanded && (
                    <tr className="border-t border-dashed border-line bg-page/50">
                      <td colSpan={columns + 1} className="px-5 py-3">
                        <Evidence row={row} currency={currency} />
                        {rowError?.id === row.id && <p className="mt-2 text-sm text-bad" role="alert">{rowError.message}</p>}
                        <div className="mt-3 flex flex-wrap gap-2">
                          {!row.deleted && (
                            <>
                              <button type="button" className="btn-ghost focus-ring h-8 px-3 text-sm" onClick={() => setEditing(row)}>
                                {t('common.edit')}
                              </button>
                              {(missing.length > 0 || row.status !== 'high') && (
                                <button type="button" className="btn-ghost focus-ring h-8 px-3 text-sm" onClick={() => void run(row, () => actions.update(row, { confirmed: true }))}>
                                  {t('table.actions.confirm')}
                                </button>
                              )}
                              {row.issue_codes.some((i) => i.code === 'duplicate_candidate') && (
                                <button type="button" className="btn-ghost focus-ring h-8 px-3 text-sm" onClick={() => void run(row, () => actions.update(row, { not_duplicate: true }))}>
                                  {t('table.actions.notDuplicate')}
                                </button>
                              )}
                              <button type="button" className="btn-ghost focus-ring h-8 px-3 text-sm text-bad" onClick={() => void run(row, () => actions.remove(row))}>
                                {t('common.delete')}
                              </button>
                            </>
                          )}
                          {row.deleted && (
                            <button type="button" className="btn-ghost focus-ring h-8 px-3 text-sm" onClick={() => void run(row, () => actions.restore(row))}>
                              {t('common.restore')}
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>

      {editing && (
        <TransactionForm
          mode="edit"
          row={editing}
          maxIndex={maxIndex}
          onCancel={() => setEditing(null)}
          onSubmit={async (input) => {
            const problem = await actions.update(editing, input)
            if (!problem) setEditing(null)
            return problem
          }}
        />
      )}
      {(adding || prefill) && (
        <TransactionForm
          mode="add"
          row={prefill?.input ?? null}
          maxIndex={maxIndex}
          defaultAfter={prefill ? Math.max(0, live.filter((r) => r.page <= prefill.page).length) : maxIndex}
          onCancel={() => {
            setAdding(false)
            onPrefillDone?.()
          }}
          onSubmit={async (input, after) => {
            const problem = await actions.create(input, after)
            if (!problem) {
              setAdding(false)
              onPrefillDone?.()
            }
            return problem
          }}
        />
      )}
    </div>
  )
}

function Tag({ children, tone }: { children: ReactNode; tone: 'bad' | 'warn' | 'navy' | 'muted' }) {
  const style = { bad: 'bg-bad-bg text-bad', warn: 'bg-warn-bg text-warn', navy: 'bg-navy-soft text-navy', muted: 'bg-page text-ink-subtle' }[tone]
  return <span className={`inline-flex items-center rounded px-1.5 text-2xs font-medium ${style}`}>{children}</span>
}

function Evidence({ row, currency }: { row: Transaction; currency: string | null }) {
  const { t, tm, date } = useI18n()
  const evidence = row.evidence ?? {}
  const verified = evidence.verified && typeof evidence.verified === 'object' ? evidence.verified : null
  const original = row.original
  return (
    <div className="grid gap-3 text-sm md:grid-cols-2">
      <div>
        <p className="font-semibold text-ink">{t('table.evidence')}</p>
        <p className="text-ink-subtle">
          {t('table.evidence.page', { page: evidence.page ?? row.page })}
          {evidence.engine ? ` · ${t('table.evidence.engine', { engine: evidence.engine })}` : ''}
          {evidence.lines?.length ? ` · ${t('table.evidence.lines', { lines: evidence.lines.join(', ') })}` : ''}
        </p>
        {evidence.excerpt && (
          <pre dir="auto" className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap rounded-control border border-line bg-white p-2 font-mono text-xs text-ink">
            {evidence.excerpt}
          </pre>
        )}
        {verified && (
          <ul className="mt-1 flex flex-wrap gap-1">
            {Object.entries(verified).map(([key, ok]) => (
              <li key={key} className={`rounded px-1.5 text-2xs ${ok ? 'bg-ok-bg text-ok' : 'bg-warn-bg text-warn'}`}>
                {t(`columns.${key}` as MessageKey) === `columns.${key}` ? key : t(`columns.${key}` as MessageKey)}:{' '}
                {ok ? t('table.evidence.verified') : t('table.evidence.unverified')}
              </li>
            ))}
          </ul>
        )}
        {evidence.confirmed_by && <p className="mt-1 text-xs text-ink-subtle">{t('table.evidence.confirmedBy', { user: evidence.confirmed_by })}</p>}
      </div>
      <div>
        {row.reason_codes.length > 0 && (
          <ul className="list-disc space-y-0.5 ps-5 text-ink-muted">
            {row.reason_codes.map((reason, n) => (
              <li key={n}>{tm(reason)}</li>
            ))}
          </ul>
        )}
        {row.corrected && original && (
          <div className="mt-2 rounded-control border border-line bg-white p-2 text-xs">
            <p className="font-semibold text-ink">{t('table.evidence.original')}</p>
            <p className="text-ink-muted">
              {original.date ? date(String(original.date)) : '—'} · <Original>{String(original.description ?? '—')}</Original>
              {(['debit', 'credit', 'amount', 'balance'] as const).map((key) =>
                original[key] ? (
                  <span key={key}>
                    {' · '}
                    {t(`columns.${key}`)} <Amount value={String(original[key])} currency={currency} />
                  </span>
                ) : null,
              )}
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
