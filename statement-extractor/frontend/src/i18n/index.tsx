import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

import type { Message } from '../types'
import { formatDate, formatDateTime, formatDecimal, formatMoney, formatNumber, formatPercent } from './format'
import ar from './locales/ar'
import de from './locales/de'
import en, { type Dictionary, type MessageKey } from './locales/en'
import es from './locales/es'
import fr from './locales/fr'

export type Locale = 'en' | 'es' | 'fr' | 'de' | 'ar'

export const LOCALES: { code: Locale; name: string; dir: 'ltr' | 'rtl' }[] = [
  { code: 'en', name: 'English', dir: 'ltr' },
  { code: 'es', name: 'Español', dir: 'ltr' },
  { code: 'fr', name: 'Français', dir: 'ltr' },
  { code: 'de', name: 'Deutsch', dir: 'ltr' },
  { code: 'ar', name: 'العربية', dir: 'rtl' },
]

export const DICTIONARIES: Record<Locale, Dictionary> = { en, es, fr, de, ar }

const STORAGE_KEY = 'stmt.locale'
const PLACEHOLDER = /\{(\w+)(?::(\w+))?\}/g

function list(values: string[], locale: string): string {
  try {
    return new Intl.ListFormat(locale, { style: 'short', type: 'conjunction' }).format(values)
  } catch {
    return values.join(', ')
  }
}

/** Fill a template's placeholders, formatting each value for the locale. Pure: used by tests. */
export function interpolate(template: string, params: Record<string, unknown>, locale: Locale): string {
  const dict = DICTIONARIES[locale]
  const label = (prefix: string, key: string) =>
    (dict[`${prefix}.${key}` as MessageKey] as string | undefined) ?? (en[`${prefix}.${key}` as MessageKey] as string | undefined) ?? key
  return template.replace(PLACEHOLDER, (_whole, name: string, type?: string) => {
    const value = params[name]
    if (value === undefined || value === null || value === '') return '—'
    const values = Array.isArray(value) ? value.map(String) : [String(value)]
    switch (type) {
      case 'money':
        return formatDecimal(String(value), locale)
      case 'date':
        return formatDate(String(value), locale)
      case 'number':
        return formatNumber(Number(value), locale)
      case 'rows':
        return list(values.map((v) => formatNumber(Number(v), locale)), locale)
      case 'list':
        return list(values, locale)
      case 'fields':
        return list(values.map((v) => label('fields', v)), locale)
      case 'checks':
        return list(values.map((v) => label('check', v)), locale)
      case 'engine':
        return label('engine', String(value))
      default:
        return values.join(', ')
    }
  })
}

export function translate(locale: Locale, key: MessageKey, params: Record<string, unknown> = {}): string {
  const template = DICTIONARIES[locale][key] ?? en[key] ?? key
  return interpolate(template, params, locale)
}

/** A backend message: its code translated, or the server's English text when the code is unknown. */
export function translateMessage(locale: Locale, message: Message | null | undefined): string {
  if (!message) return ''
  const key = `msg.${message.code}` as MessageKey
  if (key in en) return translate(locale, key, message.params ?? {})
  return message.text
}

function initialLocale(): Locale {
  try {
    const saved = localStorage.getItem(STORAGE_KEY) as Locale | null
    if (saved && saved in DICTIONARIES) return saved
  } catch {
    /* no storage */
  }
  const preferred = (typeof navigator !== 'undefined' ? navigator.language : 'en').slice(0, 2) as Locale
  return preferred in DICTIONARIES ? preferred : 'en'
}

interface I18nValue {
  locale: Locale
  dir: 'ltr' | 'rtl'
  setLocale: (locale: Locale) => void
  t: (key: MessageKey, params?: Record<string, unknown>) => string
  tm: (message: Message | null | undefined) => string
  /** Translate an error/stage code that may or may not have a message key. */
  tc: (code: string | null | undefined, fallback?: string, params?: Record<string, unknown>) => string
  money: (value: string | null | undefined, currency?: string | null) => string
  decimal: (value: string | null | undefined) => string
  date: (iso: string | null | undefined) => string
  dateTime: (iso: string | null | undefined) => string
  number: (value: number | null | undefined) => string
  percent: (value: number | null | undefined) => string
}

const I18nContext = createContext<I18nValue | null>(null)

export function I18nProvider({ children, initial }: { children: ReactNode; initial?: Locale }) {
  const [locale, setLocaleState] = useState<Locale>(initial ?? initialLocale)
  const dir = LOCALES.find((l) => l.code === locale)?.dir ?? 'ltr'

  useEffect(() => {
    document.documentElement.lang = locale
    document.documentElement.dir = dir
  }, [locale, dir])

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next)
    try {
      localStorage.setItem(STORAGE_KEY, next)
    } catch {
      /* no storage: the choice lasts for this page */
    }
  }, [])

  const value = useMemo<I18nValue>(
    () => ({
      locale,
      dir,
      setLocale,
      t: (key, params) => translate(locale, key, params),
      tm: (message) => translateMessage(locale, message),
      tc: (code, fallback = '', params = {}) => {
        if (!code) return fallback
        const key = `msg.${code}` as MessageKey
        return key in en ? translate(locale, key, params) : fallback || code
      },
      money: (value, currency) => formatMoney(value, locale, currency),
      decimal: (value) => formatDecimal(value, locale),
      date: (iso) => formatDate(iso, locale),
      dateTime: (iso) => formatDateTime(iso, locale),
      number: (value) => formatNumber(value, locale),
      percent: (value) => formatPercent(value, locale),
    }),
    [locale, dir, setLocale],
  )

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n(): I18nValue {
  const value = useContext(I18nContext)
  if (!value) throw new Error('useI18n must be used inside <I18nProvider>')
  return value
}

export type { MessageKey }
