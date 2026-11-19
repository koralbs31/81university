import { test, expect } from '@playwright/test'

const helpName = /^(Help|עזרה|مساعدة)$/i

test('visitors can skip navigation and operate Help with a keyboard', async ({ page }) => {
  await page.goto('/', { waitUntil: 'networkidle' })
  const skip = page.locator('a[href="#main-content"]')
  await skip.focus()
  await page.keyboard.press('Enter')
  await expect(page.locator('#main-content h1').first()).toBeFocused()
  const help = page.getByRole('button', { name: helpName }).first()
  await help.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('menu')).toBeVisible()
  await expect(page.getByRole('menu')).not.toHaveCSS('outline-style', 'solid')
  await expect(page.getByRole('menuitem').filter({ hasText: 'AGPL-3.0' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(help).toBeFocused()
  await expect(page.getByRole('menu')).toHaveCount(0)
  await expect(page.locator('nav ul > a')).toHaveCount(0)
})

test('the accessibility statement is public and clearly identifies its draft contacts', async ({ page }) => {
  await page.goto('/accessibility')
  await expect(page.getByRole('heading', { name: 'הצהרת נגישות', exact: true })).toBeVisible()
  await expect(page.getByText('[ACCESSIBILITY_EMAIL]', { exact: true })).toBeVisible()
  await expect(page.getByText('טיוטה לבדיקה — פרטי הקשר טרם הושלמו.', { exact: true })).toBeVisible()
})

test('dashboard Help works by keyboard and lesson links have names', async ({ page }) => {
  test.skip(!process.env.E2E_A11Y_EMAIL || !process.env.E2E_A11Y_PASSWORD, 'Set credentials for an existing local staff test account')
  await page.addInitScript(() => localStorage.setItem('lh_onboarding', JSON.stringify({ dismissed: true, welcomeSeen: true, completedSteps: [], skippedSteps: [], minimized: true, expanded: false, showAllSteps: false })))
  await page.goto('/login')
  await page.locator('input[type=email]').fill(process.env.E2E_A11Y_EMAIL!)
  await page.locator('input[type=password]').fill(process.env.E2E_A11Y_PASSWORD!)
  await page.locator('input[type=password]').locator('xpath=ancestor::form').locator('button[type=submit]').click()
  await expect(page).not.toHaveURL(/\/login/)
  await page.goto('/dash/courses')
  const help = page.locator('[data-software-help="desktop"]')
  await help.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('menuitem').filter({ hasText: 'AGPL-3.0' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(help).toBeFocused()
  await page.goto('/')
  const courseHref = await page.locator('a[href^="/course/"]').first().getAttribute('href')
  expect(courseHref).toBeTruthy()
  await page.goto(courseHref!)
  const lessonHref = await page.locator('a[href*="/activity/"]').first().getAttribute('href')
  expect(lessonHref).toBeTruthy()
  await page.goto(lessonHref!)
  for (const link of await page.locator('a[href*="/activity/"]').all()) {
    await expect(link).toHaveAccessibleName(/\S/)
  }
})


test('mouse focus stays quiet and keyboard navigation shows the focus outline', async ({ page }) => {
  await page.goto('/login')
  const email = page.locator('input[type=email]').first()
  await email.click()
  await expect(page.locator('html')).toHaveAttribute('data-focus-input', 'pointer')
  await expect(email).not.toHaveCSS('outline-color', 'rgb(37, 99, 235)')
  await page.keyboard.press('Tab')
  await expect(page.locator('html')).toHaveAttribute('data-focus-input', 'keyboard')
  await expect(page.locator(':focus')).toHaveCSS('outline-color', 'rgb(37, 99, 235)')
  await email.click()
  await expect(email).not.toHaveCSS('outline-color', 'rgb(37, 99, 235)')
  // Browser heuristics also classify contenteditable regions as focus-visible
  // on click; the input mode gate must cover those, including menu autofocus.
  await page.evaluate(() => {
    const editor = document.createElement('div')
    editor.contentEditable = 'true'
    editor.setAttribute('role', 'textbox')
    editor.setAttribute('aria-label', 'Focus regression editor')
    editor.textContent = 'Editable content'
    document.body.append(editor)
  })
  const editor = page.getByRole('textbox', { name: 'Focus regression editor' })
  await editor.click()
  await expect(editor).not.toHaveCSS('outline-color', 'rgb(37, 99, 235)')
  await page.keyboard.press('Shift+Tab')
  await page.keyboard.press('Tab')
  await expect(editor).toBeFocused()
  await expect(editor).toHaveCSS('outline-color', 'rgb(37, 99, 235)')
})
