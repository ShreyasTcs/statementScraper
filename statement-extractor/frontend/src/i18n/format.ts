/**
 * Locale formatting that never turns money into a float.
 *
 * Amounts arrive as exact decimal strings ("-1234567.891"). The integer part is
 * grouped by Intl (as a BigInt, so any size is exact and Indian lakh grouping
 * etc. come from the locale); the fraction digits are copied as they are; the
 * decimal mark and minus sign are the locale's own.
 */

const partsCache = new Map<string, { decimal: string; minus: string }>()

function symbols(locale: string): { decimal: string; minus: string } {
  let found = partsCache.get(locale)
  if (!found) {
    const parts = new Intl.NumberFormat(locale).formatToParts(-1.5)
    found = {
      decimal: parts.find((p) => p.type === 'decimal')?.value ?? '.',
      minus: parts.find((p) => p.type === 'minusSign')?.value ?? '-',
    }
    partsCache.set(locale, found)
  }
  return found
}

const DECIMAL_RE = /^([+-])?(\d+)(?:\.(\d+))?$/

/** Format an exact decimal string; returns the input unchanged if it is not a plain decimal. */
export function formatDecimal(value: string | null | undefined, locale: string, minFraction = 2): string {
  if (value === null || value === undefined || value === '') return ''
  const match = DECIMAL_RE.exec(value.trim())
  if (!match) return value
  const [, sign, whole, fraction = ''] = match
  const { decimal, minus } = symbols(locale)
  const grouped = new Intl.NumberFormat(locale, { useGrouping: true }).format(BigInt(whole))
  const digits = fraction.padEnd(minFraction, '0')
  const localDigits = digits ? new Intl.NumberFormat(locale, { useGrouping: false }).format(BigInt('1' + digits)).slice(1) : ''
  const body = localDigits ? `${grouped}${decimal}${localDigits}` : grouped
  return sign === '-' && /[1-9]/.test(whole + fraction) ? `${minus}${body}` : body
}

/** Money with an optional ISO currency code (kept as a code: symbols are ambiguous across countries). */
export function formatMoney(value: string | null | undefined, locale: string, currency?: string | null): string {
  const number = formatDecimal(value, locale)
  if (!number) return ''
  return currency ? `${number} ${currency}` : number
}

export function formatDate(iso: string | null | undefined, locale: string): string {
  if (!iso) return ''
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso)
  if (!match) return iso
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])))
  return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeZone: 'UTC' }).format(date)
}

export function formatDateTime(iso: string | null | undefined, locale: string): string {
  if (!iso) return ''
  const date = new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : `${iso}Z`)
  if (Number.isNaN(date.getTime())) return iso
  return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

export function formatNumber(value: number | null | undefined, locale: string): string {
  if (value === null || value === undefined) return ''
  return new Intl.NumberFormat(locale).format(value)
}

export function formatPercent(value: number | null | undefined, locale: string): string {
  if (value === null || value === undefined) return ''
  return new Intl.NumberFormat(locale, { style: 'percent', maximumFractionDigits: 0 }).format(value)
}

export function formatBytes(bytes: number, locale: string): string {
  const mb = bytes / (1024 * 1024)
  if (mb >= 1) return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(mb)} MB`
  return `${new Intl.NumberFormat(locale).format(Math.max(1, Math.round(bytes / 1024)))} KB`
}
