# Deployment moved to Helm

The Kustomize manifests have been replaced by the [LearnHouse Helm chart](../helm/learnhouse/README.md).
Use `deploy/helm/learnhouse/values.yaml` and the Helm install/upgrade instructions there.
Any existing private `.env` files in this directory were left untouched; their values can be reused when creating the external Kubernetes Secrets.
