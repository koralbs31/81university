import type { SearchMeta } from './types';
import { searchMeta as home } from '@/app/(protected)/orgs/[orgslug]/dash/page.search';
import { searchMeta as courses } from '@/app/(protected)/orgs/[orgslug]/dash/courses/page.search';
import { searchMeta as assignments } from '@/app/(protected)/orgs/[orgslug]/dash/assignments/page.search';
import { searchMeta as communities } from '@/app/(protected)/orgs/[orgslug]/dash/communities/page.search';
import { searchMeta as paths } from '@/app/(protected)/orgs/[orgslug]/dash/paths/page.search';
import { searchMeta as liveSessions } from '@/app/(protected)/orgs/[orgslug]/dash/live-sessions/page.search';
import { searchMeta as analytics } from '@/app/(protected)/orgs/[orgslug]/dash/analytics/page.search';
import { searchMetas as users } from '@/app/(protected)/orgs/[orgslug]/dash/users/page.search';
import { searchMetas as org } from '@/app/(protected)/orgs/[orgslug]/dash/org/page.search';
import { searchMetas as account } from '@/app/(protected)/orgs/[orgslug]/(withmenu)/account/page.search';
export const dashboardPages: SearchMeta[] = [
    home,
    courses,
    assignments,
    communities,
    paths,
    liveSessions,
    analytics,
    ...users,
    ...org,
    ...account,
];
