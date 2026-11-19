# 81 University branding

Deployment defaults live in `lib/branding.ts`. Replace the logo/icon paths there to change the platform fallback assets. The supplied logos are in `public/81university-*.png`; favicon and home-screen icon paths are in `app/layout.tsx`.

Organization uploads still override fallback logos and favicons. The upstream starter names (`Default Organization` and `LearnHouse`) display as `81 University`; custom names are preserved. This changes presentation, not stored organization data.

Use **Organization → Branding → Theme** for the existing saved primary color and font settings, and the Logos and sign-in appearance tabs for other brand settings. No new fixed colors or theme system were added.

`showLearnHouseBranding: false` disables upstream watermarks, footer marks, and player/embed badges. The platform hub's powered-by footer is also removed.

Required attribution is separate from promotional branding. Help menus offer Source code (browser icon), AGPL-3.0 (book icon), and bug reporting. The header Help menu is public. Mobile, login, editor and other screens without a header Help menu get a small floating Help button; no software footer is rendered. The optional upstream documentation, website and Discord links are removed.

`/software-notices` remains public with upstream attribution, a dated modification notice, warranty disclaimer and the full AGPL license. Repository licenses and third-party notices remain intact.

Self-hosted OSS email omits the optional powered-by line; fallback platform mail uses the 81 University wordmark. Upstream marketing-site Terms/Privacy links are removed because they are not this deployment’s policies.

Before deploying changes, publish their complete source in the public repository, including build/install scripts and lockfiles. Keep deployed revision history available. Private environment files, credentials, database dumps and learner content are excluded from the source offer.
