# 81 University branding

Deployment defaults live in `lib/branding.ts`. Replace the logo/icon paths there to change the platform fallback assets. The supplied logos are in `public/81university-*.png`; favicon and home-screen icon paths are in `app/layout.tsx`.

Organization uploads still override fallback logos and favicons. The upstream starter names (`Default Organization` and `LearnHouse`) display as `81 University`; custom names are preserved. This changes presentation, not stored organization data.

Use **Organization → Branding → Theme** for the existing saved primary color and font settings, and the Logos and sign-in appearance tabs for other brand settings. No new fixed colors or theme system were added.

`showLearnHouseBranding: false` disables upstream watermarks, footer marks, and player/embed badges. The platform hub's powered-by footer is also removed.
