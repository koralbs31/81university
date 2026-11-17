import type { Direction } from './direction'

export interface Language {
  code: string
  translationKey: string
  nativeName: string
  /** Text direction of the language. Behaviour lives in lib/direction.ts. */
  dir: Direction
}

export const AVAILABLE_LANGUAGES: Language[] = [
  { code: 'en', translationKey: 'common.english', nativeName: 'English', dir: 'ltr' },
  { code: 'ar', translationKey: 'common.arabic', nativeName: 'العربية', dir: 'rtl' },
  { code: 'he', translationKey: 'common.hebrew', nativeName: 'עברית', dir: 'rtl' },
]

export const getLanguageByCode = (code: string): Language | undefined => {
  return AVAILABLE_LANGUAGES.find(lang => lang.code === code)
}

export const getCurrentLanguageNativeName = (currentLang: string): string => {
  const language = getLanguageByCode(currentLang)
  return language?.nativeName || 'English'
}
