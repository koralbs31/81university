export const VERSION = '1.6.0';
export const APP_IMAGE = 'ghcr.io/learnhouse/learnhouse-web:main';
export const DEV_IMAGE = 'ghcr.io/learnhouse/learnhouse-web:dev';
// Third-party images are pinned to a minor line: patch releases (security
// fixes) still arrive on pull, new minors/majors only with a CLI release.
export const NGINX_IMAGE = 'nginx:1.30-alpine';
export const POSTGRES_IMAGE = 'postgres:17-alpine';
export const REDIS_IMAGE = 'redis:7.4-alpine';
export const CADDY_IMAGE = 'caddy:2.11-alpine';
export const ALPINE_IMAGE = 'alpine:3.24';
export const HEALTH_CHECK_URL_PATH = '/api/v1/health';
export const HEALTH_CHECK_TIMEOUT_MS = 180000; // 3 minutes
export const HEALTH_CHECK_INTERVAL_MS = 3000;
export const CONFIG_FILENAME = 'learnhouse.config.json';
// ── Enterprise Edition ───────────────────────────────────────────────────────
// EE images are pulled from the license-gated registry; the license key is the
// docker-login password.
export const EE_REGISTRY = 'images.learnhouse.app';
export const EE_REGISTRY_USERNAME = 'license';
export const EE_BACKEND_IMAGE = 'images.learnhouse.app/enterprise-backend';
export const EE_FRONTEND_IMAGE = 'images.learnhouse.app/enterprise-frontend';
export const EE_LICENSE_SERVER = 'https://partners.learnhouse.app';
export const EE_DEFAULT_IMAGE_TAG = 'prod';
export const INSTANCE_INFO_PATH = '/api/v1/instance/info';
export const EE_READY_TIMEOUT_MS = 360000; // 6 minutes (image pull + license activation)
