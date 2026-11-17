# LearnHouse on Kubernetes with Helm

One Helm chart runs separate web (Next.js :3000) and API (FastAPI :9000) Deployments and Services. Image tags, replica counts, resources and extra environment settings are independent. Boards, collaboration, payments and built-in AI are absent. PostgreSQL and Redis are external. Durable uploaded media is stored in S3-compatible object storage (Cloudflare R2); the chart creates no upload PVC or storage class.

Run these commands from the repository root. Helm 3 or 4, kubectl, a working Kubernetes context, PostgreSQL, Redis, an Ingress controller, DNS and a TLS certificate are required. The chart has no subchart dependencies.

## 1. Build your service images

Use the architecture of your cluster (`linux/amd64` or `linux/arm64`), your registry and an immutable release tag:

```sh
docker buildx build --platform linux/amd64 --build-arg LEARNHOUSE_PUBLIC=true -t REGISTRY/learnhouse-api:RELEASE --push apps/api
docker buildx build --platform linux/amd64 --build-arg LEARNHOUSE_PUBLIC=true --build-arg BUILD_ID=RELEASE -t REGISTRY/learnhouse-web:RELEASE --push apps/web
```

GitHub Actions also builds separate web/API images on branch pushes and releases. This chart does not build or publish images.

## 2. Set your production values

```sh
cp deploy/helm/values-production.example.yaml deploy/helm/values-production.yaml
```

Edit that file. The example contains placeholders, not working production infrastructure:

```yaml
domain: school.example.com
https: true
api:
  replicas: 2
  image:
    repository: REGISTRY/learnhouse-api
    tag: "RELEASE"
web:
  replicas: 2
  image:
    repository: REGISTRY/learnhouse-web
    tag: "RELEASE"
contentStorage:
  region: auto
ingress:
  className: nginx
  tlsSecretName: learnhouse-tls
```

All available settings are in [values.yaml](values.yaml). Additional non-secret settings go in `api.env` or `web.env`; quote values such as `"true"`, `"false"` and numbers. Use Kubernetes Secrets for credentials, not values files. Neither application needs a rebuild when its runtime environment changes.

Media is stored in your private R2 bucket. No upload PVC, persistent disk or storage class is required. API Pods use temporary container disk space while uploading files, importing courses and processing videos; uploaded temporary files are cleaned up after the operation. That scratch space is not durable media storage. PostgreSQL remains necessary for courses/users/progress and Redis for shared runtime state; these do not move into R2.

Create an R2 bucket and an R2 S3 API token scoped to it with **Object Read & Write** permissions. Keep the bucket private: the application authorizes access and generates signed download URLs. Put the four variables below in the API Secret file:

```dotenv
LEARNHOUSE_S3_API_BUCKET_NAME=YOUR_BUCKET_NAME
LEARNHOUSE_S3_API_ENDPOINT_URL=https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com
AWS_ACCESS_KEY_ID=YOUR_R2_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY=YOUR_R2_SECRET_ACCESS_KEY
```

Use the R2 S3 endpoint, not a public `r2.dev` URL or custom media domain. The chart sets `LEARNHOUSE_CONTENT_DELIVERY_TYPE=s3api`, `LEARNHOUSE_S3_API_REGION=auto` and `AWS_DEFAULT_REGION=auto`. For real AWS S3, change `contentStorage.region` to your bucket's region and use the corresponding endpoint/credentials. The web Pods never receive the API storage credentials.

For browser media playback, add a CORS policy to the bucket in Cloudflare's dashboard (replace the origin):

```json
[
  {
    "AllowedOrigins": ["https://school.example.com"],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedHeaders": ["Range", "Content-Type"],
    "ExposeHeaders": ["ETag", "Accept-Ranges", "Content-Range", "Content-Length"],
    "MaxAgeSeconds": 3600
  }
]
```

Uploads go through the API; browser downloads and video segments use signed R2 URLs. See [Cloudflare's boto3 setup](https://developers.cloudflare.com/r2/examples/aws/boto3/), [R2 credentials](https://developers.cloudflare.com/r2/api/tokens/) and [CORS configuration](https://developers.cloudflare.com/r2/buckets/cors/).

When moving an existing installation, copy the complete `content/` object hierarchy to the bucket with the same keys before changing storage mode, then verify access to existing media. Merely switching the configuration does not upload existing local files. If an earlier chart already created a PVC, its old retention annotation preserves it; remove the old `persistence` values block and clean up that PVC only after its content is migrated and verified. The application still supports filesystem storage for native development, but this Helm chart now uses S3/R2 only.

The public domain must point to your Ingress controller. The chart routes `/api/v1` and `/content` to API, with everything else going to web. It references your TLS Secret; it does not create a certificate or install an Ingress controller. Set controller-specific annotations in `ingress.annotations`. For an intentional HTTP-only deployment, set `https: false`.

For private images, create a registry Secret in the release namespace and set:

```yaml
imagePullSecrets:
  - name: registry-credentials
```

Both application Pods and the migration hook use it.

## 3. Create the namespace and Secrets before installing

The migration hook runs before the release resources exist, so the namespace and external Secrets must already exist. Keep real `.env` files private; the deployment directory ignores them.

```sh
kubectl config current-context
kubectl create namespace learnhouse --dry-run=client -o yaml | kubectl apply -f -
cp deploy/helm/api-secrets.env.example deploy/helm/api-secrets.env
cp deploy/helm/bootstrap-secrets.env.example deploy/helm/bootstrap-secrets.env
```

Fill in the API file with:

```dotenv
LEARNHOUSE_SQL_CONNECTION_STRING=postgresql+asyncpg://USER:URL_ENCODED_PASSWORD@DB_HOST:5432/DB_NAME
LEARNHOUSE_REDIS_CONNECTION_STRING=rediss://USER:URL_ENCODED_PASSWORD@REDIS_HOST:6379/0
LEARNHOUSE_AUTH_JWT_SECRET_KEY=REPLACE_WITH_RANDOM_SECRET
LEARNHOUSE_S3_API_BUCKET_NAME=YOUR_BUCKET_NAME
LEARNHOUSE_S3_API_ENDPOINT_URL=https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com
AWS_ACCESS_KEY_ID=YOUR_R2_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY=YOUR_R2_SECRET_ACCESS_KEY
```

Use `redis://` if Redis does not use TLS. Generate the JWT secret with `openssl rand -hex 32`. All API replicas must share it. Optional email/Judge0 variables belong in this file too; examples are included in its template.

Fill in the bootstrap file with your initial admin email and password. These create an admin only on a new database; changing them does not reset an existing password.

```sh
kubectl -n learnhouse create secret generic learnhouse-api-secrets --from-env-file=deploy/helm/api-secrets.env --dry-run=client -o yaml | kubectl apply -f -
kubectl -n learnhouse create secret generic learnhouse-bootstrap-secrets --from-env-file=deploy/helm/bootstrap-secrets.env --dry-run=client -o yaml | kubectl apply -f -
```

Create the `learnhouse-tls` Secret from your certificate, or arrange for your certificate manager to create it in this namespace.

### Optional Google login

Copy/fill `deploy/helm/web-secrets.env.example` as `deploy/helm/web-secrets.env`:

```dotenv
LEARNHOUSE_GOOGLE_CLIENT_ID=YOUR_CLIENT_ID.apps.googleusercontent.com
LEARNHOUSE_GOOGLE_CLIENT_SECRET=YOUR_CLIENT_SECRET
```

Also add `LEARNHOUSE_GOOGLE_OAUTH_CLIENT_ID` with the same client ID to the API secret file and reapply that Secret. Create the web Secret:

```sh
kubectl -n learnhouse create secret generic learnhouse-web-secrets --from-env-file=deploy/helm/web-secrets.env --dry-run=client -o yaml | kubectl apply -f -
```

Enable its reference in your production values:

```yaml
web:
  existingSecret: learnhouse-web-secrets
```

In Google Cloud, use a Web application OAuth client with origin `https://school.example.com` and exact redirect URI `https://school.example.com/auth/callback/google`. Enable Google in the organization's allowed authentication methods. Credentials remain server-side; no `NEXT_PUBLIC_` secret is needed.

## 4. Preview and install

```sh
helm lint deploy/helm/learnhouse --strict -f deploy/helm/values-production.yaml
helm template learnhouse deploy/helm/learnhouse -n learnhouse -f deploy/helm/values-production.yaml > /tmp/learnhouse-rendered.yaml
helm upgrade --install learnhouse deploy/helm/learnhouse \
  --namespace learnhouse \
  --values deploy/helm/values-production.yaml \
  --wait --timeout 25m
```

The `pre-install,pre-upgrade` Job runs `scripts/prepare_deployment.py` using exactly the configured API image. It inlines non-secret configuration and reads the pre-created API/bootstrap Secrets; it does not depend on the release's ConfigMap or Services. Helm waits for successful completion before creating/updating workloads. A failed Job blocks the release; inspect its logs before retrying. The next install/upgrade replaces the prior hook Job; completed/failed Jobs otherwise expire after one day.

Serving API replicas have automatic migration and installation disabled. The bootstrap script uses the existing PostgreSQL migration advisory lock and initializes default roles/org/admin as needed. Do not skip the hook on a fresh database. Set `migration.enabled: false` only if you deliberately run the matching migration/bootstrap elsewhere first.

Before schema-breaking upgrades, back up PostgreSQL and the bucket, pause traffic and scale API to zero before running Helm. The chart's configured replica count restores it afterward. For compatible upgrades, the Deployments use Kubernetes rolling updates. `helm rollback` changes release resources; it cannot undo database migrations or recover deleted data.

This chart replaces the old Kustomize setup. If you previously applied those manifests to a real cluster, back up the database/uploads and plan resource ownership/media migration before installing over them; Helm does not automatically adopt arbitrary existing resources. No live deployment was performed during this conversion.

## 5. Inspect and update

```sh
helm status learnhouse -n learnhouse
helm get values learnhouse -n learnhouse
helm get manifest learnhouse -n learnhouse
kubectl -n learnhouse get pods,svc,ingress,jobs
kubectl -n learnhouse logs job/learnhouse-migrate
curl --fail https://school.example.com/api/v1/health
curl --fail https://school.example.com/api/health
```

After configuring R2, upload a lesson attachment and video, verify downloads/playback, then replace an API Pod and verify the same media still loads. Existing storage/media tests use mocked S3; no real Cloudflare bucket was contacted during this change.

Change an image tag, replicas, resources or environment in `values-production.yaml`, then repeat the same `helm upgrade --install` command. Keep that file as your source of deployment settings. ConfigMap changes trigger a Pod rollout through a checksum annotation. After changing an external Secret, restart its consumers:

```sh
kubectl -n learnhouse rollout restart deployment/learnhouse-api
kubectl -n learnhouse rollout restart deployment/learnhouse-web
```

A custom release name changes resource names and the frontend's internal API URL automatically. You can disable a service with `api.enabled` or `web.enabled`. When hosting the API outside this release, disable its migration hook and set `web.env.LEARNHOUSE_INTERNAL_API_URL` and the public backend URL to the actual API addresses.

## Offline verification

```sh
helm lint deploy/helm/learnhouse --strict
uv run --project apps/api python deploy/helm/learnhouse/test_chart.py
helm template learnhouse deploy/helm/learnhouse -n learnhouse
```

The Python check uses the repository's existing PyYAML dependency. It covers hook dependencies, namespaces, secret isolation, routing, separate image tags/replicas, absence of PVCs/volume mounts, private registries, Google credentials, optional services and invalid environment values. Kubernetes schema validation is separate from an actual cluster rollout.

See [Helm hook lifecycle](https://helm.sh/docs/topics/charts_hooks/) and [Helm values and upgrades](https://helm.sh/docs/helm/helm_upgrade/). Native development instructions remain in [LOCAL_NATIVE.md](../../../LOCAL_NATIVE.md).
