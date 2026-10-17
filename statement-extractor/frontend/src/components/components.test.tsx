import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider, type Locale } from '../i18n'
import type { Check, Completeness, Transaction } from '../types'
import { ChecksList, CompletenessPanel } from './Checks'
import { TransactionsTable, type TableActions } from './TransactionsTable'
import { Amount } from './ui'

afterEach(cleanup)

function wrap(ui: React.ReactNode, locale: Locale = 'en') {
  return render(<I18nProvider initial={locale}>{ui}</I18nProvider>)
}

function txn(overrides: Partial<Transaction>): Transaction {
  return {
    id: overrides.id ?? 't1', account: 'A1', index: 1, page: 1, date: '2026-08-02', value_date: null, date_inherited: false,
    description: 'Coffee', reference: null, debit: '4.50', credit: null, amount: null, balance: '95.50', currency: 'EUR',
    direction_inferred: false, balance_check: 'ok', confidence: 0.98, status: 'high', issues: [], issue_codes: [], reasons: [],
    reason_codes: [], bbox: null, source_engine: 'local', provenance: 'explicit', evidence: null, corrected: false, deleted: false,
    original: null, ...overrides,
  }
}

const noop: TableActions = {
  update: vi.fn(async () => null),
  create: vi.fn(async () => null),
  remove: vi.fn(async () => null),
  restore: vi.fn(async () => null),
}

describe('i18n wiring', () => {
  it('sets dir=rtl and lang for Arabic', () => {
    wrap(<Amount value="-1234.5" />, 'ar')
    expect(document.documentElement.dir).toBe('rtl')
    expect(document.documentElement.lang).toBe('ar')
  })

  it('keeps amounts left-to-right inside a right-to-left page', () => {
    wrap(<Amount value="-1234.5" currency="EUR" />, 'ar')
    const amount = document.querySelector('[dir="ltr"]')
    expect(amount).not.toBeNull()
    expect(amount!.textContent).toContain('EUR')
  })
})

describe('checks and completeness', () => {
  it('translates a check by its code and params', () => {
    const checks: Check[] = [{
      key: 'opening_closing', label: 'x', status: 'fail', detail: 'english', detail_code: 'oc_fail',
      params: { expected: '100.00', closing: '90.00', difference: '10.00' },
    }]
    wrap(<ChecksList checks={checks} />, 'de')
    expect(screen.getByText('Anfangssaldo + Gutschriften − Belastungen = Endsaldo')).toBeTruthy()
    expect(screen.getByText(/Differenz 10,00/)).toBeTruthy()
  })

  it('never renders a needs-review statement as complete', () => {
    const completeness: Completeness = {
      status: 'needs_review',
      reasons: [{ code: 'duplicates_found', params: { count: 2 }, text: '' }],
      metrics: { pages_detected: 2, pages_processed: 2 },
    }
    wrap(<CompletenessPanel completeness={completeness} />)
    expect(screen.getByText('Needs review')).toBeTruthy()
    expect(screen.getByText('Possible duplicate rows: 2.')).toBeTruthy()
  })
})

describe('transactions table', () => {
  const rows = [
    txn({ id: 'a', index: 1, description: 'Coffee' }),
    txn({ id: 'b', index: 2, description: 'Rent', debit: '900.00', balance_check: 'mismatch', status: 'needs_review', confidence: 0.4 }),
    txn({ id: 'c', index: 0, description: 'Old fee', deleted: true }),
  ]

  it('filters to rows that need attention and hides deleted rows by default', () => {
    wrap(<TransactionsTable rows={rows} hasBalance currency="EUR" actions={noop} />)
    expect(screen.getByText('Coffee')).toBeTruthy()
    expect(screen.queryByText('Old fee')).toBeNull()
    fireEvent.change(screen.getAllByRole('combobox')[0], { target: { value: 'mismatch' } })
    expect(screen.queryByText('Coffee')).toBeNull()
    expect(screen.getByText('Rent')).toBeTruthy()
    fireEvent.change(screen.getAllByRole('combobox')[0], { target: { value: 'deleted' } })
    expect(screen.getByText('Old fee')).toBeTruthy()
  })

  it('searches descriptions and shows original text untranslated', () => {
    wrap(<TransactionsTable rows={rows} hasBalance currency="EUR" actions={noop} />, 'fr')
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'rent' } })
    expect(screen.getByText('Rent')).toBeTruthy()
    expect(screen.queryByText('Coffee')).toBeNull()
  })

  it('offers restore for a deleted row and calls the action', async () => {
    wrap(<TransactionsTable rows={rows} hasBalance currency="EUR" actions={noop} />)
    fireEvent.change(screen.getAllByRole('combobox')[0], { target: { value: 'deleted' } })
    fireEvent.click(screen.getByText('Old fee'))
    fireEvent.click(screen.getByText('Restore'))
    expect(noop.restore).toHaveBeenCalled()
  })
})
