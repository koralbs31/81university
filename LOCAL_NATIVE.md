# Run without Docker

The frontend and API are separate processes. PostgreSQL and Redis store shared state; the applications have separate environment files. Boards and the collaboration server have been removed.

## Restart this computer

The database, demo course, dependencies and `.env` files are already configured. Start the dependencies:

```sh
brew services start postgresql@17
brew services start redis
```

Open two Terminal tabs and keep these commands running:

```sh
cd /Users/koralsteinberg/learnhouse/apps/api
uv run uvicorn app:app --host 127.0.0.1 --port 1338
```

```sh
cd /Users/koralsteinberg/learnhouse/apps/web
bun run dev
```

Open http://localhost:3000/login. If a port is already in use, the app is already running; stop its existing Terminal process before restarting.

## Set up another computer

Install Bun, uv, PostgreSQL 17 and Redis (`brew install bun uv postgresql@17 redis` on macOS). Run `brew services start postgresql@17` and `brew services start redis`.

Create a PostgreSQL user/database named `learnhouse` with your chosen password. Copy `apps/api/.env.example` to `apps/api/.env` and `apps/web/.env.example` to `apps/web/.env.local`. Set the database password, a random JWT secret (`openssl rand -hex 32`), and your initial admin email/password in the API file. Keep the frontend URL settings from the example for localhost. Real environment files are ignored by Git.

```sh
cd /path/to/learnhouse/apps/api
uv sync --frozen
uv run python scripts/prepare_deployment.py
uv run uvicorn app:app --host 127.0.0.1 --port 1338
```

In a second Terminal:

```sh
cd /path/to/learnhouse/apps/web
bun install --frozen-lockfile
bun run dev
```

To test a production frontend locally, stop `bun run dev`, then run `bun run build` followed by `bun run start`.

## Check and back up

```sh
curl --fail http://localhost:1338/api/v1/health
curl --fail http://localhost:3000/api/health
redis-cli ping
```

Back up the PostgreSQL database with `pg_dump -Fc` and copy `apps/api/content` together with the private environment files to a safe location. The pre-removal local database backup is `.local-backups/before-feature-removal.dump`; do not publish that directory or environment files.

For independent containers and Kubernetes, see [deployment instructions](deploy/helm/learnhouse/README.md). Built-in AI and payments need no keys because their implementations have been removed.
