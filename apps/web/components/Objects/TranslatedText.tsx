'use client'

import { useTranslation } from 'react-i18next'

export default function TranslatedText({ id, fallback }: { id: string; fallback: string }) {
  const { t } = useTranslation()
  return <>{t(id, { defaultValue: fallback })}</>
}
