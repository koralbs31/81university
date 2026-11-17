# Production GitOps

Repository: https://github.com/koralbs31/81university

Push application changes to `main` (or open a pull request first). GitHub Actions runs the API suite and Ruff, web unit tests and TypeScript checks, Helm contract checks, and PostgreSQL migration checks. Successful main builds publish separate amd64 images to GHCR tagged with the source commit SHA. Only after both images publish, the bot commits their versions to `values-production.yaml`.

Argo CD watches `main`, renders `deploy/helm/learnhouse` with that values file, runs the migration Job as a PreSync hook, then rolls out web and API. A failed test/build never changes desired production image versions. Workflow concurrency serializes main deployments. Pull requests only run checks and never publish or promote images. Bot promotion commits do not trigger another workflow.

The public GHCR packages must allow anonymous pulls. They contain the application, not production environment files. Runtime configuration is rendered by Helm; credentials stay in the existing Kubernetes Secrets. No SSH key, kubeconfig or production password is needed in GitHub Actions.

## Bootstrap / inspect

On the VPS, with the existing namespace, database infrastructure, Secrets and TLS certificate already present:

```sh
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl apply -f deploy/gitops/application.yaml
kubectl -n argocd get application learnhouse
kubectl -n learnhouse get pods
```

Do not run another `helm upgrade` for the application once Argo CD manages it. Argo uses Helm to render manifests; the historical Helm release record is no longer the source of truth. `learnhouse-infra` remains separately managed; PostgreSQL/Redis and their PVCs are outside this Application, as are the existing TLS certificates, redirect Ingresses and Traefik settings.

## Roll back

Revert the relevant promotion commit in GitHub (or copy both image tags from a previous known-good promotion). Argo reconciles that Git state. This rolls back application images only; database migrations require a compatible schema or a separate restore plan. Do not rewrite main history.

Changing `values-production.yaml` directly deploys those values without a build. Other Helm or source changes go through checks/build/promotion. Documentation-only pushes skip CI. For a manual rebuild, use Actions → Test, publish and deploy → Run workflow on main.

External prerequisites remain: cluster Secrets `learnhouse-api-secrets` and `learnhouse-bootstrap-secrets`, PostgreSQL/Redis from `learnhouse-infra`, R2/CORS and DNS/TLS. None of their credential values belongs in this public repository.
