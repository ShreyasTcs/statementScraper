import { describe, expect, it } from 'vitest'

import { DICTIONARIES, interpolate, translate, translateMessage, type Locale } from './index'
import { formatDate, formatDecimal, formatMoney } from './format'
import en from './locales/en'

const placeholders = (text: string) => [...text.matchAll(/\{(\w+)(?::(\w+))?\}/g)].map((m) => `${m[1]}:${m[2] ?? ''}`).sort()

describe('dictionaries', () => {
  const reference = Object.keys(en).sort()
  for (const [locale, dict] of Object.entries(DICTIONARIES)) {
    it(`${locale} has exactly the English keys`, () => {
      expect(Object.keys(dict).sort()).toEqual(reference)
    })
    it(`${locale} uses the same placeholders as English`, () => {
      for (const key of reference) {
        expect(placeholders(dict[key as keyof typeof dict]), `${locale}:${key}`).toEqual(placeholders(en[key as keyof typeof en]))
      }
    })
    it(`${locale} has no empty strings`, () => {
      for (const [key, value] of Object.entries(dict)) expect(value.trim(), `${locale}:${key}`).not.toBe('')
    })
  }
})

describe('exact decimal formatting', () => {
  it('never rounds through a float', () => {
    expect(formatDecimal('12345678901234567.891', 'en')).toBe('12,345,678,901,234,567.891')
    expect(formatDecimal('0.10', 'en')).toBe('0.10')
    expect(formatDecimal('-1234.5', 'en')).toBe('-1,234.50')
  })

  it('uses the locale separators', () => {
    expect(formatDecimal('1234567.89', 'de')).toBe('1.234.567,89')
    expect(formatDecimal('1234567.89', 'fr').replace(/\s/g, ' ')).toBe('1 234 567,89')
    expect(formatDecimal('1234567.89', 'en-IN')).toBe('12,34,567.89')
  })

  it('keeps non-decimal input unchanged and handles zero', () => {
    expect(formatDecimal('n/a', 'en')).toBe('n/a')
    expect(formatDecimal('-0.00', 'en')).toBe('0.00')
    expect(formatDecimal(null, 'en')).toBe('')
  })

  it('formats money with an ISO code and dates in UTC', () => {
    expect(formatMoney('99.5', 'en', 'EUR')).toBe('99.50 EUR')
    expect(formatDate('2026-08-31', 'en')).toContain('2026')
    expect(formatDate('2026-08-31', 'de')).toContain('2026')
  })
})

describe('translation', () => {
  it('formats typed placeholders for the locale', () => {
    const text = interpolate('{a:money} | {d:date} | {n:number} | {f:fields}', { a: '1234.5', d: '2026-01-02', n: 1500, f: ['iban', 'currency'] }, 'de')
    expect(text).toContain('1.234,50')
    expect(text).toContain('1.500')
    expect(text).toContain('IBAN')
    expect(text).toContain('Währung')
  })

  it('translates backend message codes with params', () => {
    const msg = { code: 'oc_fail', params: { expected: '100.00', closing: '90.00', difference: '10.00' }, text: 'English fallback' }
    expect(translateMessage('es', msg)).toBe('Las filas extraídas dan 100,00, pero el extracto muestra 90,00 (diferencia 10,00).')
  })

  it('falls back to the server text for an unknown code', () => {
    expect(translateMessage('fr', { code: 'brand_new_code', params: {}, text: 'Server says hi' })).toBe('Server says hi')
  })

  it('shows a dash for a missing parameter instead of a raw placeholder', () => {
    expect(translate('en' as Locale, 'msg.chain_result', { ok: 3 })).toBe('Rows that reconcile: 3 of —.')
  })
})
