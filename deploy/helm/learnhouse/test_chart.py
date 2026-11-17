"""Offline Helm contract checks. Run with Python + PyYAML and Helm on PATH."""
from pathlib import Path
import subprocess
import yaml

CHART = Path(__file__).resolve().parent


def render(*args):
    result = subprocess.run(
        ['helm', 'template', 'school', str(CHART), '--namespace', 'school-prod', *args],
        check=True, capture_output=True, text=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def find(docs, kind, name):
    return next(doc for doc in docs if doc['kind'] == kind and doc['metadata']['name'] == name)


def container(doc):
    return doc['spec']['template']['spec']['containers'][0]


def main():
    docs = render()
    assert len(docs) == 7
    assert all(doc['metadata']['namespace'] == 'school-prod' for doc in docs)
    api = find(docs, 'Deployment', 'school-api')
    web = find(docs, 'Deployment', 'school-web')
    job = find(docs, 'Job', 'school-migrate')
    assert job['metadata']['annotations']['helm.sh/hook'] == 'pre-install,pre-upgrade'
    assert container(job)['image'] == container(api)['image']
    # A pre-install hook cannot rely on the release's not-yet-created ConfigMap/PVC.
    assert all('configMapRef' not in item for item in container(job)['envFrom'])
    assert 'volumes' not in job['spec']['template']['spec']
    config = find(docs, 'ConfigMap', 'school-config')['data']
    assert {item['name']: item['value'] for item in container(job)['env']} == config
    assert config['LEARNHOUSE_AUTO_MIGRATE'] == config['LEARNHOUSE_AUTO_INSTALL'] == 'false'
    assert config['LEARNHOUSE_INTERNAL_API_URL'] == 'http://school-api:9000'
    assert container(web)['envFrom'] == [{'configMapRef': {'name': 'school-config'}}]
    assert container(api)['envFrom'][1] == {'secretRef': {'name': 'learnhouse-api-secrets'}}
    assert config['LEARNHOUSE_CONTENT_DELIVERY_TYPE'] == 's3api'
    assert config['LEARNHOUSE_S3_API_REGION'] == config['AWS_DEFAULT_REGION'] == 'auto'
    assert {item['name']: item['value'] for item in container(api)['env']} == {
        'LEARNHOUSE_CONTENT_DELIVERY_TYPE': 's3api', 'LEARNHOUSE_S3_API_REGION': 'auto',
        'AWS_DEFAULT_REGION': 'auto'}
    assert not any(d['kind'] == 'PersistentVolumeClaim' for d in docs)
    assert 'volumes' not in api['spec']['template']['spec']
    assert 'volumeMounts' not in container(api)
    assert 'AWS_ACCESS_KEY_ID' not in config and 'AWS_SECRET_ACCESS_KEY' not in config
    assert not any(d['kind'] in ('PersistentVolume', 'StorageClass') for d in docs)
    ingress = find(docs, 'Ingress', 'school')
    paths = ingress['spec']['rules'][0]['http']['paths']
    assert [(p['path'], p['backend']['service']['name']) for p in paths] == [
        ('/api/v1', 'school-api'), ('/content', 'school-api'), ('/', 'school-web')]
    # Independent tags/replicas, registry credentials and OAuth.
    custom = render('--set', 'domain=learn.example.org', '--set', 'api.image.tag=api-v2',
                    '--set', 'web.image.tag=web-v3', '--set', 'api.replicas=3',
                    '--set', 'web.existingSecret=google-oauth',
                    '--set', 'imagePullSecrets[0].name=private-registry',
                    '--set-string', 'api.env.LEARNHOUSE_GOOGLE_OAUTH_CLIENT_ID=google-id')
    assert not any(d['kind'] == 'PersistentVolumeClaim' for d in custom)
    api2 = find(custom, 'Deployment', 'school-api')
    web2 = find(custom, 'Deployment', 'school-web')
    job2 = find(custom, 'Job', 'school-migrate')
    assert api2['spec']['replicas'] == 3
    assert container(api2)['image'].endswith(':api-v2')
    assert container(job2)['image'] == container(api2)['image']
    assert container(web2)['image'].endswith(':web-v3')
    assert container(web2)['envFrom'][1] == {'secretRef': {'name': 'google-oauth'}}
    assert 'volumes' not in api2['spec']['template']['spec']
    assert job2['spec']['template']['spec']['imagePullSecrets'] == [{'name': 'private-registry'}]
    assert {'name': 'LEARNHOUSE_GOOGLE_OAUTH_CLIENT_ID', 'value': 'google-id'} in container(job2)['env']
    assert api['spec']['template']['metadata']['annotations'] != api2['spec']['template']['metadata']['annotations']
    # HTTP/no ingress, externally run migrations, and independently disabled apps.
    minimal = render('--set', 'https=false', '--set', 'ingress.enabled=false',
                     '--set', 'migration.enabled=false', '--set', 'api.enabled=false')
    assert not any(d['kind'] in ('Ingress', 'Job', 'PersistentVolumeClaim') for d in minimal)
    assert not any(d['metadata']['name'] == 'school-api' for d in minimal)
    http = find(render('--set', 'https=false'), 'Ingress', 'school')
    assert 'tls' not in http['spec']
    assert 'school-web' not in str(render('--set', 'web.enabled=false'))
    invalid = subprocess.run(['helm', 'template', 'school', str(CHART), '--set', 'api.env.BAD=true'],
                             capture_output=True, text=True)
    assert invalid.returncode != 0, 'Boolean env values must be rejected; use quoted strings'
    old_storage = subprocess.run(['helm', 'template', 'school', str(CHART), '--set', 'persistence.size=20Gi'],
                                 capture_output=True, text=True)
    assert old_storage.returncode != 0, 'Retired PVC settings must not be silently ignored'
    upgrade = render('--is-upgrade', '--set', 'contentStorage.region=us-east-1')
    assert find(upgrade, 'ConfigMap', 'school-config')['data']['AWS_DEFAULT_REGION'] == 'us-east-1'
    assert not any(d['kind'] == 'PersistentVolumeClaim' for d in upgrade)
    assert find(upgrade, 'Job', 'school-migrate')['metadata']['annotations']['helm.sh/hook'] == 'pre-install,pre-upgrade'
    assert container(web)['livenessProbe']['httpGet']['path'] == '/api/health'
    assert container(api)['readinessProbe']['httpGet']['path'] == '/api/v1/health'
    assert container(api)['livenessProbe']['httpGet']['path'] == '/api/v1/health/live'
    print('PASS: Helm hooks, secret isolation, image/replica overrides, routing, R2 without PVCs, optional services and value validation')


if __name__ == '__main__':
    main()
