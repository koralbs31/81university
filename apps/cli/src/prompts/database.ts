import crypto from 'node:crypto'
import * as p from '../utils/prompt.js'
import pc from 'picocolors'
import { validateRequired } from '../utils/validators.js'
import { checkTcpConnection, parsePostgresUrl, parseRedisUrl } from '../utils/network.js'

export interface DatabaseConfig {
  useExternalDb: boolean
  externalDbConnectionString?: string
  dbPassword?: string
  useExternalRedis: boolean
  externalRedisConnectionString?: string
}

async function promptAndVerifyPostgres(): Promise<string> {
  while (true) {
    const connString = await p.text({
      message: 'PostgreSQL connection string?',
      placeholder: 'postgresql://user:password@host:5432/learnhouse',
      validate: (value) => {
        const err = validateRequired(value)
        if (err) return err
        if (!value.startsWith('postgresql://') && !value.startsWith('postgres://')) {
          return 'Must start with postgresql:// or postgres://'
        }
        return undefined
      },
    })
    if (p.isCancel(connString)) { p.cancel(); process.exit(0) }

    const parsed = parsePostgresUrl(connString as string)
    if (!parsed) {
      p.log.error('Could not parse the connection string. Please check the format.')
      continue
    }

    const s = p.spinner()
    s.start(`Checking connection to ${parsed.host}:${parsed.port}`)
    const reachable = await checkTcpConnection(parsed.host, parsed.port)

    if (reachable) {
      s.stop(`${pc.green('Connected')} to ${parsed.host}:${parsed.port}`)
      return connString as string
    }

    s.stop(`${pc.red('Connection failed')} to ${parsed.host}:${parsed.port}`)
    const retry = await p.confirm({
      message: 'Could not reach the database. Try a different connection string?',
      initialValue: true,
    })
    if (p.isCancel(retry) || !retry) { p.cancel(); process.exit(0) }
  }
}

async function promptAndVerifyRedis(): Promise<string> {
  while (true) {
    const connString = await p.text({
      message: 'Redis connection string?',
      placeholder: 'redis://user:password@host:6379/0',
      validate: (value) => {
        const err = validateRequired(value)
        if (err) return err
        if (!value.startsWith('redis://') && !value.startsWith('rediss://')) {
          return 'Must start with redis:// or rediss://'
        }
        return undefined
      },
    })
    if (p.isCancel(connString)) { p.cancel(); process.exit(0) }

    const parsed = parseRedisUrl(connString as string)
    if (!parsed) {
      p.log.error('Could not parse the connection string. Please check the format.')
      continue
    }

    const s = p.spinner()
    s.start(`Checking connection to ${parsed.host}:${parsed.port}`)
    const reachable = await checkTcpConnection(parsed.host, parsed.port)

    if (reachable) {
      s.stop(`${pc.green('Connected')} to ${parsed.host}:${parsed.port}`)
      return connString as string
    }

    s.stop(`${pc.red('Connection failed')} to ${parsed.host}:${parsed.port}`)
    const retry = await p.confirm({
      message: 'Could not reach Redis. Try a different connection string?',
      initialValue: true,
    })
    if (p.isCancel(retry) || !retry) { p.cancel(); process.exit(0) }
  }
}

export async function promptDatabase(): Promise<DatabaseConfig> {
  // --- PostgreSQL ---
  const dbChoice = await p.select({
    message: 'PostgreSQL database setup?',
    options: [
      { value: 'local', label: 'Create a new database (Docker)', hint: 'recommended' },
      { value: 'external', label: 'Use an external database', hint: 'bring your own PostgreSQL' },
    ],
  })
  if (p.isCancel(dbChoice)) { p.cancel(); process.exit(0) }

  let useExternalDb = false
  let externalDbConnectionString: string | undefined
  let dbPassword: string | undefined

  if (dbChoice === 'external') {
    externalDbConnectionString = await promptAndVerifyPostgres()
    useExternalDb = true
  } else {

    dbPassword = crypto.randomBytes(24).toString('base64url')
    // The password is not printed (terminal scrollback, screen shares); it is
    // saved in the install's .env as POSTGRES_PASSWORD.
    p.log.info(`Database credentials generated (user ${pc.cyan('learnhouse')}, host db:5432); the password is saved in .env.`)
  }

  // --- Redis ---
  const redisChoice = await p.select({
    message: 'Redis setup?',
    options: [
      { value: 'local', label: 'Create a new Redis instance (Docker)', hint: 'recommended' },
      { value: 'external', label: 'Use an external Redis', hint: 'bring your own Redis' },
    ],
  })
  if (p.isCancel(redisChoice)) { p.cancel(); process.exit(0) }

  let useExternalRedis = false
  let externalRedisConnectionString: string | undefined

  if (redisChoice === 'external') {
    externalRedisConnectionString = await promptAndVerifyRedis()
    useExternalRedis = true
  }

  return {
    useExternalDb,
    externalDbConnectionString,
    dbPassword,
    useExternalRedis,
    externalRedisConnectionString,
  }
}
