'use client'
// Deployment copyright; software license links live in SoftwareHelp.
import React from 'react'
import { useTranslation } from 'react-i18next'

// License and attribution links are supplied by the shared SoftwareHelp.
export function AuthFooter(_props: { className?: string }) {
  return null
}

export function CopyrightFooter({
  year,
  className = '',
  tone = 'light',
}: {
  year: number
  className?: string
  // `light` → dark text on light bg; `dark` → light text on dark bg.
  tone?: 'light' | 'dark'
}) {
  const { t } = useTranslation()
  const base = tone === 'dark' ? 'text-white/40' : 'text-black/35'
  return (
    <footer className={`w-full py-6 px-6 ${className}`}>
      <div className="flex flex-col sm:flex-row items-center justify-center gap-x-5 gap-y-2 text-[13px] font-medium">
        <p className={base}>
          {t('common.copyright', { defaultValue: '© {{year}} 81 University', year })}
        </p>
      </div>
    </footer>
  )
}
