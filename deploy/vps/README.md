# Deployed VPS

Site: https://81university.com

The deployment uses the custom domain `81university.com`, a Let's Encrypt certificate, K3s/Traefik, the two application images, PostgreSQL/Redis, and Cloudflare R2. Google and email-link login are disabled; password login is enabled. The site's default language is Hebrew. A fresh production database was initialized; the local development database was not restored into it. A small Hebrew demonstration course is provided for verification.

## Files on the VPS

`/root/learnhouse-deploy` contains the application source snapshot, charts, `values-production.yaml`, image-build logs, a root-only Secret manifest and database backups. Credentials are in Kubernetes Secrets; none are committed here. The existing Argo CD installation was left intact; this deployment is managed through Helm directly. Connecting it to Argo CD later requires your own repository/registry containing this modified project, not the original upstream repository.

The demo video uses optimized MP4 playback; optional HLS transcoding is not enabled.

The non-secret configuration is recorded in [values.yaml](values.yaml), [certificate.yaml](certificate.yaml), and [traefik-config.yaml](traefik-config.yaml), and [domain-redirects.yaml](domain-redirects.yaml). The optional database chart is [learnhouse-infra](../helm/learnhouse-infra/README.md).

## Inspect the deployment

SSH to the VPS, then:

```sh
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
cd /root/learnhouse-deploy
helm list -A
helm status learnhouse -n learnhouse
kubectl -n learnhouse get pods,svc,ingress,pvc,certificate
kubectl -n learnhouse logs deployment/learnhouse-api --tail=100
kubectl -n learnhouse logs deployment/learnhouse-web --tail=100
curl --fail https://81university.com/api/v1/health
curl --fail https://81university.com/api/health
```

K3s's kubeconfig must be selected explicitly on this VPS; the preexisting Helm environment otherwise points to an incorrect endpoint.

## Change configuration

Edit `/root/learnhouse-deploy/values-production.yaml` and apply:

```sh
helm upgrade learnhouse ./learnhouse -n learnhouse -f values-production.yaml --wait --timeout 15m
```

The pre-upgrade Job migrates/bootstrap-checks the database before rollout. Secrets remain external to Helm. After modifying an external Secret, restart its consuming Deployment.

Images are currently built on this VPS and loaded directly into its containerd image store. The values use `imagePullPolicy: Never`. For additional nodes, publish the images to your own registry, configure registry access and change the pull policy; otherwise other nodes cannot obtain these local images. Use a new image tag for every application update.

## Back up PostgreSQL

On the VPS:

```sh
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
umask 077
mkdir -p /root/learnhouse-deploy/backups
kubectl -n learnhouse exec learnhouse-infra-postgres-0 -- pg_dump -U learnhouse -d learnhouse -Fc > /root/learnhouse-deploy/backups/manual.dump
```

Copy backups off the VPS. R2 holds uploaded media; include it in your backup/recovery plan. The initial dump is at `/root/learnhouse-deploy/backups/initial-production.dump`. No automatic backup schedule has been installed.

## Domain and redirects

Namecheap DNS has an A record `@ → 194.163.156.252` and CNAME `www → 81university.com`. The main Helm release serves the apex domain using the `learnhouse-domain-tls` Secret. The separate redirects manifest sends HTTP, HTTPS `www` and the old temporary hostname to `https://81university.com`, preserving paths and query strings. The old temporary certificate is retained for its HTTPS redirect.

The R2 CORS allowlist includes the new apex/WWW origins and the old temporary origin. Updated on 17 November 2026. Public DNS resolvers were verified; the workstation's DNS cache still held old Namecheap parking entries during testing. TLS, both logins, API/web health, signed video downloads/CORS and canonical redirects were verified against the authoritative VPS address without disabling TLS checks. See [verification results](../../browser-checks/vps/domain-verification.json).

The domain transition backup is `/root/learnhouse-deploy/backups/before-domain-change.dump`; previous Helm values are in `/root/learnhouse-deploy/values-before-domain.json`.

## Later: Google login and email

For Google, create a Web OAuth client with origin `https://81university.com` and redirect URI `https://81university.com/auth/callback/google`, create the web Secret, add the matching client ID to the API Secret, and enable Google in the organization's authentication policy. SMTP/Resend credentials are also still needed if you want invitations, password resets and email notifications.

## GitOps

The application is now managed through [GitHub Actions and Argo CD](../gitops/README.md). The manual image-loading and Helm-upgrade instructions above describe the initial bootstrap. Use Git commits for subsequent application deployments; keep infrastructure and credentials separate.
