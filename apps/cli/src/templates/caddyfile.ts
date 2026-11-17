import type { SetupConfig } from '../types.js'

export function generateCaddyfile(config: SetupConfig): string {
  const email = config.sslEmail || 'admin@example.com'

  return `{
  email ${email}
}

${config.domain} {
  @api path /api/v1 /api/v1/* /content /content/*
  handle @api {
    reverse_proxy learnhouse-api:9000
  }
  handle {
    reverse_proxy learnhouse-app:3000
  }
}
`
}
