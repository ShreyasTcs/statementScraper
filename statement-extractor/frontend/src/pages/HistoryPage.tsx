import { useCallback, useEffect, useState } from 'react'

import { CompletenessBadge, ErrorBanner, Spinner } from '../components/ui'
import { useI18n, type MessageKey } from '../i18n'
import { ApiRequestError, api } from '../services/api'
import type { DocumentListItem } from '../types'

const PAGE = 25

export function HistoryPage() {
  const { t, tc, dateTime, number } = useI18n()
  const [items, setItems] = useState<DocumentListItem[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const page = await api.list({ limit: PAGE, offset, q: query.trim() || undefined })
      setItems(page.items)
      setTotal(page.total)
      setError(null)
    } catch (caught) {
      setError(caught instanceof ApiRequestError ? tc(caught.code, caught.message) : t('msg.network'))
    }
    setLoading(false)
  }, [offset, query, t, tc])

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 200)
    return () => window.clearTimeout(timer)
  }, [load])

  // Keep refreshing while anything is still processing.
  useEffect(() => {
    if (!items.some((item) => item.status === 'queued' || item.status === 'processing')) return
    const timer = window.setInterval(() => void load(), 2000)
    return () => window.clearInterval(timer)
  }, [items, load])

  const remove = async (item: DocumentListItem) => {
    if (!window.confirm(t('history.deleteConfirm', { file: item.filename }))) return
    try {
      await api.remove(item.id)
      await load()
    } catch (caught) {
      setError(caught instanceof ApiRequestError ? tc(caught.code, caught.message) : t('msg.network'))
    }
  }

  return (
    <main className="mx-auto w-full max-w-[1100px] space-y-4 px-4 pb-16 pt-8 xl:px-0">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold text-ink">{t('history.title')}</h1>
        {loading && <Spinner />}
        <input
          type="search"
          className="field-input ms-auto h-9 max-w-[320px]"
          placeholder={t('history.search')}
          value={query}
          onChange={(event) => {
            setOffset(0)
            setQuery(event.target.value)
          }}
          aria-label={t('history.search')}
        />
      </div>
      {error && <ErrorBanner message={error} />}
      <div className="card overflow-x-auto">
        <table className="w-full min-w-[760px] text-base">
          <thead className="bg-page text-xs uppercase tracking-wide text-ink-subtle">
            <tr>
              <th className="px-4 py-2 text-start">{t('history.col.file')}</th>
              <th className="px-4 py-2 text-start">{t('history.col.uploaded')}</th>
              <th className="px-4 py-2 text-start">{t('history.col.status')}</th>
              <th className="px-4 py-2 text-start">{t('history.col.engine')}</th>
              <th className="px-4 py-2 text-end">{t('history.col.rows')}</th>
              <th className="px-4 py-2" />
            </tr>
          </thead>
          <tbody>
            {!loading && items.length === 0 && (
              <tr>
                <td colSpan={6} className="px-4 py-8 text-center text-ink-subtle">{t('history.empty')}</td>
              </tr>
            )}
            {items.map((item) => {
              const active = item.status === 'queued' || item.status === 'processing'
              return (
                <tr key={item.id} className="border-t border-line">
                  <td className="max-w-[320px] truncate px-4 py-2.5">
                    <a href={`#/documents/${item.id}`} className="focus-ring rounded font-medium text-navy hover:underline">
                      <bdi>{item.filename}</bdi>
                    </a>
                    <span className="block text-xs text-ink-subtle">{t('common.page', { page: item.page_count })}</span>
                  </td>
                  <td className="whitespace-nowrap px-4 py-2.5 text-ink-muted">{dateTime(item.created_at)}</td>
                  <td className="px-4 py-2.5">
                    {active ? (
                      <span className="inline-flex items-center gap-2 text-sm text-ink-muted"><Spinner size={12} /> {t('history.processing')}</span>
                    ) : (
                      <>
                        <CompletenessBadge status={item.completeness} />
                        {item.error_code && <span className="block text-xs text-bad">{tc(item.error_code, item.error_code)}</span>}
                      </>
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-sm text-ink-muted">
                    {item.engine_used ? t(`engine.${item.engine_used}` as MessageKey) : item.engine_requested ? t(`engine.${item.engine_requested}` as MessageKey) : '—'}
                  </td>
                  <td className="px-4 py-2.5 text-end tabular-nums">{number(item.rows)}</td>
                  <td className="whitespace-nowrap px-4 py-2.5 text-end">
                    <a href={`#/documents/${item.id}`} className="btn-ghost focus-ring h-8 px-3 text-sm">{t('history.open')}</a>
                    {!active && (
                      <button type="button" className="btn-ghost focus-ring ms-2 h-8 px-3 text-sm text-bad" onClick={() => void remove(item)}>
                        {t('common.delete')}
                      </button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {total > PAGE && (
        <div className="flex items-center justify-end gap-2 text-sm">
          <button type="button" className="btn-ghost focus-ring h-8 px-3" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
            {t('common.back')}
          </button>
          <span className="tabular-nums text-ink-subtle">
            {number(offset + 1)}–{number(Math.min(offset + PAGE, total))} / {number(total)}
          </span>
          <button type="button" className="btn-ghost focus-ring h-8 px-3" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>
            →
          </button>
        </div>
      )}
    </main>
  )
}
