# Accessibility regression checks

Run against an already running development instance without bootstrap or teardown:

```sh
cd apps/e2e
E2E_BASE_URL=http://localhost:3000 bunx playwright test -c accessibility.config.ts
```

Set `E2E_A11Y_EMAIL` and `E2E_A11Y_PASSWORD` to an existing staff test account to include the dashboard and lesson checks. Without credentials, that test is skipped. `E2E_CHROME_PATH` optionally selects an installed Chrome executable. Do not commit credentials. These tests do not submit feedback, save lessons or change user records.

Checks cover skip navigation, public Help keyboard operation and focus return, public statement visibility, dashboard menu keyboard use, and accessible names on lesson links. They supplement axe scans and manual screen-reader/content testing; passing is not proof of legal compliance.

19 November 2026 remediation: original 24-state audit found shared keyboard, naming, labeling, contrast and markup issues. The final local axe retest of 14 representative public, mobile, dashboard, course and lesson states reported zero selected WCAG 2/2.1 A/AA rule violations. Actual screen-reader speech, student-specific assessments, media and document content remain unverified. The Hebrew accessibility statement deliberately contains draft contact placeholders pending operator details.

The widget boundary tests stub only the vendor response and verify document-level separation between public and protected routes. Protected areas (authentication, account, administration, course editors, dashboard, LTI and embeds) use a root layout that never includes the vendor script. Public-to-protected navigation replaces the browser document, clearing already executed third-party code. Root route groups do not change public URL paths.
