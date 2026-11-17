import {
  House,
  BookOpen,
  Files,
  Users,
  CurrencyCircleDollar,
  Buildings,
  ChatsCircle,
  ChalkboardSimple,
  Cube,
  FolderSimple,
  Headphones,
  VideoCamera,
  ChartBar,
  Code,
  Steps,
} from '@phosphor-icons/react'
import { isOrgAnalyticsAvailable } from '@ee/components/Analytics/availability'

export interface DashboardMenuItem {
  id: string
  href: string
  icon: typeof House
  labelKey: string
  /** Feature key used for plan-based gating. If undefined, item is always shown. */
  featureKey?: string
  /** If true, the feature defaults to disabled (must be explicitly enabled). */
  defaultDisabled?: boolean
  /** Hides the item when it returns false (e.g. Enterprise-only pages). */
  available?: () => boolean
}

export const DASHBOARD_MENU_ITEMS: DashboardMenuItem[] = [
  {
    id: 'home',
    href: '/dash',
    icon: House,
    labelKey: 'common.home',
  },
  {
    id: 'courses',
    href: '/dash/courses',
    icon: BookOpen,
    labelKey: 'courses.courses',
  },
  {
    id: 'assignments',
    href: '/dash/assignments',
    icon: Files,
    labelKey: 'common.assignments',
  },
  {
    id: 'paths',
    href: '/dash/paths',
    icon: Steps,
    labelKey: 'paths.title',
    featureKey: 'paths',
  },

  {
    id: 'communities',
    href: '/dash/communities',
    icon: ChatsCircle,
    labelKey: 'communities.title',
    featureKey: 'communities',
  },

  {
    id: 'live_sessions',
    href: '/dash/live-sessions',
    icon: VideoCamera,
    labelKey: 'live_sessions.title',
    featureKey: 'live_sessions',
  },


  {
    id: 'users',
    href: '/dash/users/settings/users',
    icon: Users,
    labelKey: 'common.users',
  },

  {
    id: 'organization',
    href: '/dash/org/settings/general',
    icon: Buildings,
    labelKey: 'common.organization',
  },
  {
    id: 'analytics',
    href: '/dash/analytics',
    icon: ChartBar,
    labelKey: 'common.analytics',
    available: isOrgAnalyticsAvailable,
  },
  {
    id: 'developers',
    href: '/dash/developers/api',
    icon: Code,
    labelKey: 'dashboard.developers.breadcrumb',
  },
]
