/** Deployment defaults. Organization branding settings still override these assets. */
export const branding = {
  name: '81 University',
  logo: '/81university-logo.png',
  icon: '/81university-icon.png',
  showLearnHouseBranding: false,
} as const

export function withBrandingDefaults<T extends { name?: string }>(org: T): T {
  if (!org || !['Default Organization', 'LearnHouse'].includes(org.name || '')) return org
  return { ...org, name: branding.name }
}
