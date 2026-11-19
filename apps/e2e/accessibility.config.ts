import { defineConfig } from '@playwright/test'

// Read-only checks against an already running instance. No bootstrap or teardown.
export default defineConfig({
  testDir: './features/accessibility/tests',
  workers: 1,
  timeout: 45_000,
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://localhost:3000',
    viewport: { width: 1440, height: 1000 },
    launchOptions: process.env.E2E_CHROME_PATH ? { executablePath: process.env.E2E_CHROME_PATH } : undefined,
  },
})
