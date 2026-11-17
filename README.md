# LearnHouse — simplified edition

A course platform with a block editor, assignments and exercises, student progress, communities, messaging, certificates and user management. The interface supports Hebrew (RTL), English and Arabic (RTL).

This edition removes podcasts, boards and their collaboration server, standalone playgrounds, the library, calendar, course updates, profile bios/custom profiles, payments and built-in AI. Ordinary code exercises, manual course creation, media uploads and deterministic grading remain.

## Run locally without Docker

Follow [LOCAL_NATIVE.md](LOCAL_NATIVE.md) for prerequisites, separate environment files and exact start/restart commands. The frontend runs at `http://localhost:3000` and the API at `http://localhost:1338`. PostgreSQL and Redis are external processes. Environment examples are in `apps/web/.env.example` and `apps/api/.env.example`.

## Production GitOps

Push changes to `main` to run checks, publish separate web/API images to GHCR, and let Argo CD deploy their tested versions. See [the pipeline and Argo CD guide](deploy/gitops/README.md). Production credentials stay in Kubernetes Secrets.

## Deploy separate containers to Kubernetes

Follow [deploy/helm/learnhouse/README.md](deploy/helm/learnhouse/README.md). There are two application images:

```sh
docker build --build-arg LEARNHOUSE_PUBLIC=true -t learnhouse-api:local apps/api
docker build --build-arg LEARNHOUSE_PUBLIC=true -t learnhouse-web:local apps/web
```

The Helm chart provides separate API/frontend Deployments and Services, an Ingress, environment and Secret references, S3/R2 media storage and a pre-install/pre-upgrade migration hook. Configure them through `values-production.yaml`. Supply your PostgreSQL/Redis URLs, secrets, registry, hostname, TLS certificate and R2 bucket credentials before deploying. No collaboration service is needed.

The old combined Dockerfile/supervisor is retired. The CLI's generated Compose configuration also uses separate frontend and API images; Kubernetes deployments are managed through Helm.

The deployed VPS configuration and maintenance commands are in [deploy/vps/README.md](deploy/vps/README.md).

## Review and verification

- [Removal scope and backups](FEATURE_REMOVAL.md)
- API surface check: `cd apps/api && uv run python scripts/check_simplified_surface.py`
- UI type check: `cd apps/web && bunx next typegen && bunx tsc --noEmit`
- Hebrew/RTL checks: `cd apps/web && bun test --isolate tests/rtl-guard.test.mjs`

Original project: [LearnHouse](https://github.com/learnhouse/learnhouse). See [LICENSE](LICENSE).
