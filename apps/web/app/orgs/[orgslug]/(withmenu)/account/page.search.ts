import {
  Gear,
  IdentificationCard,
  Lock,
  ShoppingBag,
} from '@phosphor-icons/react'
import type { SearchMeta } from '@/lib/dashboard-search/types'

export const searchMetas: SearchMeta[] = [
  {
    id: 'account.general',
    titleKey: 'account.general',
    descriptionKey: 'dashboard.search.entries.account_general.description',
    keywordsKey: 'dashboard.search.entries.account_general.keywords',
    icon: Gear,
    href: '/account/general',
    group: 'settings',
  },

  {
    id: 'account.security',
    titleKey: 'account.security',
    descriptionKey: 'dashboard.search.entries.account_security.description',
    keywordsKey: 'dashboard.search.entries.account_security.keywords',
    icon: Lock,
    href: '/account/security',
    group: 'settings',
  },

]
