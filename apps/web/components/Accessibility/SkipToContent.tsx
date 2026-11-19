'use client'
import { useTranslation } from 'react-i18next'
export default function SkipToContent() {
  const { t } = useTranslation()
  return <a href="#main-content" onClick={(event) => {
    const target = document.querySelector<HTMLElement>('#main-content h1') || document.getElementById('main-content')
    if (target) { event.preventDefault(); target.tabIndex = -1; target.focus(); target.scrollIntoView({ block: 'start' }) }
  }} className="sr-only focus:not-sr-only focus:fixed focus:start-4 focus:top-4 focus:z-[10000] focus:rounded-md focus:bg-white focus:px-4 focus:py-3 focus:text-black focus:shadow-lg">{t('accessibility.skip_content')}</a>
}
