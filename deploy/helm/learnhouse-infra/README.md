# Optional single-node databases

This chart is for the self-hosted VPS deployment. The application Helm chart still accepts external PostgreSQL/Redis URLs.

It creates one PostgreSQL 17 and one Redis 7.4 StatefulSet, internal-only headless Services, and local-path database volumes (10 GiB and 2 GiB). Media stays in R2; these disks hold the database and Redis persistence. Neither Service is exposed publicly. This is a single-node installation, not a highly available database service.

Create `learnhouse-database-secrets` in the release namespace with `POSTGRES_PASSWORD` and `REDIS_PASSWORD` before installing:

```sh
helm upgrade --install learnhouse-infra deploy/helm/learnhouse-infra -n learnhouse --wait --timeout 10m
```

App connection hosts are `learnhouse-infra-postgres:5432` and `learnhouse-infra-redis:6379` when using release name `learnhouse-infra` in the same namespace. Configure their passwords in the application's existing API Secret. Defaults use the K3s `local-path` storage class; choose another class through values when appropriate.

StatefulSet data PVCs are retained on deletion by Kubernetes' default retention policy. Keep off-server PostgreSQL backups; a volume on the same VPS does not protect against loss of the VPS.
