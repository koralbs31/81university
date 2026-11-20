import '../styles/globals.css'
import { branding } from '@/lib/branding'
import React from 'react'
import type { Metadata, Viewport } from 'next'
import Providers from '@components/Providers'
import SoftwareHelp from '@components/Footers/SoftwareHelp'
import SkipToContent from '@components/Accessibility/SkipToContent'
import localFont from 'next/font/local'
import Script from 'next/script'

const wixMadeforText = localFont({
  src: './fonts/WixMadeforText.ttf',
  weight: '400 800',
  display: 'swap',
  variable: '--font-default',
})

// Wix Madefor Text has no Arabic subset, so Arabic would otherwise fall back to
// whatever the OS provides (Geeza Pro, Segoe UI, Noto) and look like a
// different product on every platform.
//
// Tajawal is the Arabic face for the whole product. It is FORCED whenever the
// UI is Arabic (see globals.css), not merely offered as a fallback: Tajawal
// ships a Latin subset too, so a mixed Arabic screen renders in one typeface
// instead of switching per glyph between two designs with different
// proportions.
//
// Weights are 200-900 with no 600, so a `font-semibold` element rounds up to 700,
// which is the intended reading.
const tajawal = localFont({
  src: [
    { path: './fonts/Tajawal-Light.ttf', weight: '300' },
    { path: './fonts/Tajawal-Regular.ttf', weight: '400' },
    { path: './fonts/Tajawal-Medium.ttf', weight: '500' },
    { path: './fonts/Tajawal-Bold.ttf', weight: '700' },
    { path: './fonts/Tajawal-ExtraBold.ttf', weight: '800' },
  ],
  display: 'swap',
  variable: '--font-arabic',
  // Only RTL/Arabic screens use it, so don't make every visitor download it.
  preload: false,
})

// viewport-fit=cover lets the phone shell paint under the notch and home
// indicator; the bars pad themselves with env(safe-area-inset-*). Pinch zoom
// stays enabled: inputs are 16px on phones so focusing one never auto-zooms.
export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  viewportFit: 'cover',
  themeColor: '#ffffff',
}

// Saved to the home screen, the app opens without browser chrome.
export const metadata: Metadata = {
  title: branding.name,
  applicationName: branding.name,
  icons: { icon: '/81university-favicon-v3.png', apple: '/81university-apple-touch-icon.png' },
  appleWebApp: { capable: true, statusBarStyle: 'default' },
  formatDetection: { telephone: false },
}

export default function RootLayout({
  children,
  allowAccessibilityWidget = false,
}: {
  children: React.ReactNode
  allowAccessibilityWidget?: boolean
}) {
  // `dir` is deliberately absent from the <html> below. React only reconciles
  // attributes present in its virtual tree, so leaving it out means React never
  // clobbers what dir-init.js wrote before paint. `lang="en"` stays as the
  // no-JS baseline for crawlers; the script overwrites it for everyone else.
  return (
    <html
      className={`${wixMadeforText.variable} ${tajawal.variable}`}
      lang="en"
      suppressHydrationWarning
    >
      <head>
        {/* Synchronous script: sets <html lang/dir> before body paints so an
            RTL locale never flashes an LTR layout. Must run first. */}
        {/* eslint-disable-next-line @next/next/no-sync-scripts */}
        <script src="/dir-init.js" />
        {/* eslint-disable-next-line @next/next/no-sync-scripts */}
        <script src="/focus-input.js" />
        {/* Synchronous script: blocks parsing to guarantee window.__RUNTIME_CONFIG__ exists before any JS runs.
            Next.js <Script strategy="beforeInteractive"> is not truly blocking in all browsers (Safari). */}
        {/* eslint-disable-next-line @next/next/no-sync-scripts */}
        <script src="/runtime-config.js" />
        {/* Prevent white flash on embed routes: set html+body bg before body is painted.
            Reads the optional ?bgcolor param (hex-validated) or defaults to dark. */}
        {/* eslint-disable-next-line @next/next/no-sync-scripts */}
        <script src="/embed-bg.js" />
      </head>
      <body suppressHydrationWarning>
        <Providers>
          <SkipToContent />
          <main id="main-content" tabIndex={-1} className="animate-fade-in">
            {children}
          </main>
          <SoftwareHelp />
        </Providers>
        {allowAccessibilityWidget && <Script
          src="https://negishot.co.il/cdn/widget.php?code=NGS_A605A0FFAD6B"
          strategy="afterInteractive"
        />}
      </body>
    </html>
  )
}
