import { expect, test } from 'bun:test'
import { branding, withBrandingDefaults } from '../lib/branding'

test('starter branding uses 81 University while preserving custom org settings', () => {
  const starter = { name: 'Default Organization', logo_image: 'custom.png', config: { color: '#123456' } }
  expect(withBrandingDefaults(starter)).toEqual({ ...starter, name: branding.name })
  expect(starter.name).toBe('Default Organization')
  expect(withBrandingDefaults({ name: 'LearnHouse' }).name).toBe(branding.name)
  const custom = { name: 'My school' }
  expect(withBrandingDefaults(custom)).toBe(custom)
  expect(branding.showLearnHouseBranding).toBe(false)
})
