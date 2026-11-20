import { test, expect } from '@playwright/test'

const vendor = '**/negishot.co.il/cdn/widget.php?code=*'

async function mockWidget(page: import('@playwright/test').Page) {
  let requests = 0
  await page.route(vendor, async (route) => {
    requests++
    await route.fulfill({ contentType: 'application/javascript', body: `window.__negishotLoaded = true;` })
  })
  return () => requests
}

test('public pages load the widget once', async ({ page }) => {
  const requests = await mockWidget(page)
  await page.goto('/accessibility')
  await expect.poll(() => page.evaluate(() => Boolean((window as any).__negishotLoaded))).toBe(true)
  expect(requests()).toBe(1)
})

test('authentication pages never request the widget', async ({ page }) => {
  const requests = await mockWidget(page)
  for (const route of ['/login', '/signup', '/forgot', '/admin/login']) {
    await page.goto(route, { waitUntil: 'networkidle' })
    await expect(page.locator('script[src*="negishot.co.il"]')).toHaveCount(0)
    expect(await page.evaluate(() => Boolean((window as any).__negishotLoaded))).toBe(false)
  }
  expect(requests()).toBe(0)
})

test('navigating to login discards previously executed widget code', async ({ page }) => {
  const requests = await mockWidget(page)
  await page.goto('/', { waitUntil: 'networkidle' })
  await expect.poll(() => page.evaluate(() => Boolean((window as any).__negishotLoaded))).toBe(true)
  const before = requests()
  await page.locator('a[href*="/login"]').first().click()
  await expect(page).toHaveURL(/\/login/)
  await page.waitForLoadState('networkidle')
  expect(await page.evaluate(() => Boolean((window as any).__negishotLoaded))).toBe(false)
  await expect(page.locator('script[src*="negishot.co.il"]')).toHaveCount(0)
  expect(requests()).toBe(before)
})

test('account, dashboard and editor documents never load the widget', async ({ page }) => {
  test.skip(!process.env.E2E_A11Y_EMAIL || !process.env.E2E_A11Y_PASSWORD, 'Set credentials for an existing staff test account')
  const requests = await mockWidget(page)
  await page.addInitScript(() => localStorage.setItem('lh_onboarding', JSON.stringify({ dismissed: true, welcomeSeen: true, completedSteps: [], skippedSteps: [], minimized: true, expanded: false, showAllSteps: false })))
  await page.goto('/login')
  await page.locator('input[type=email]').fill(process.env.E2E_A11Y_EMAIL!)
  await page.locator('input[type=password]').fill(process.env.E2E_A11Y_PASSWORD!)
  await page.locator('input[type=password]').locator('xpath=ancestor::form').locator('button[type=submit]').click()
  await expect(page).not.toHaveURL(/\/login/)
  await expect.poll(() => page.evaluate(() => Boolean((window as any).__negishotLoaded))).toBe(true)
  const before = requests()
  // This is a Next.js client link: crossing root layouts must replace the document.
  await page.locator('a[href*="/course/"]').first().click()
  await expect(page).toHaveURL(/\/course\//)
  await page.locator('a[href*="/activity/"]').first().click()
  const edit = page.locator('a[href$="/edit"]').first()
  await edit.click()
  await expect(page).toHaveURL(/\/edit$/)
  await page.waitForLoadState('networkidle')
  expect(await page.evaluate(() => Boolean((window as any).__negishotLoaded))).toBe(false)
  const protectedRequests = requests()
  for (const route of ['/account/general', '/dash/courses']) {
    await page.goto(route, { waitUntil: 'networkidle' })
    await expect(page.locator('script[src*="negishot.co.il"]')).toHaveCount(0)
    expect(await page.evaluate(() => Boolean((window as any).__negishotLoaded))).toBe(false)
    expect(requests()).toBe(protectedRequests)
  }
  expect(requests()).toBeGreaterThanOrEqual(before) // Public course navigation may start a fresh public document.
})
