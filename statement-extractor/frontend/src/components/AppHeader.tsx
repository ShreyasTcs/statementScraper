import { LOCALES, useI18n, type Locale } from '../i18n'

export function AppHeader({ route }: { route: string }) {
  const { t, locale, setLocale } = useI18n()
  const links = [
    { href: '#/', label: t('nav.new'), active: route === '/' },
    { href: '#/history', label: t('nav.history'), active: route.startsWith('/history') || route.startsWith('/documents') },
    { href: '#/settings', label: t('nav.settings'), active: route.startsWith('/settings') },
  ]
  return (
    <header className="flex h-header shrink-0 items-center gap-3 border-b border-line bg-white px-4 xl:px-8">
      <a href="#/" className="focus-ring flex min-w-0 items-center gap-3 rounded-control">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[10px] bg-navy text-[13px] font-bold tracking-wide text-white">
          SE
        </span>
        <span className="min-w-0 leading-tight">
          <span className="block truncate text-md font-semibold text-ink">{t('app.name')}</span>
          <span className="hidden truncate text-sm text-ink-subtle sm:block">{t('app.tagline')}</span>
        </span>
      </a>

      <nav className="ms-auto flex items-center gap-1" aria-label="Main">
        {links.map((link) => (
          <a
            key={link.href}
            href={link.href}
            aria-current={link.active ? 'page' : undefined}
            className={`focus-ring rounded-control px-3 py-1.5 text-base ${
              link.active ? 'bg-navy-soft font-semibold text-navy' : 'text-ink-muted hover:bg-page'
            }`}
          >
            {link.label}
          </a>
        ))}
        <label className="ms-2 flex items-center gap-1.5 text-sm text-ink-subtle">
          <span className="sr-only">{t('nav.language')}</span>
          <select
            value={locale}
            onChange={(event) => setLocale(event.target.value as Locale)}
            className="focus-ring h-8 rounded-control border border-line bg-white px-2 text-sm text-ink"
            aria-label={t('nav.language')}
          >
            {LOCALES.map((l) => (
              <option key={l.code} value={l.code} lang={l.code}>
                {l.name}
              </option>
            ))}
          </select>
        </label>
      </nav>
    </header>
  )
}
