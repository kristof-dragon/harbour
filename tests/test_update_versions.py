import copy
import json

import pytest

from test_harbour import client, wait_job
from test_task_queue import submit
from harbour import app as module, demo, remote_probe, store


CONFIG = 'sha256:' + 'b' * 64
MANIFEST = 'sha256:' + 'c' * 64
INDEX = 'sha256:' + 'd' * 64
PLATFORM = {'os': 'linux', 'architecture': 'arm64', 'variant': 'v8'}


@pytest.mark.parametrize('running,candidate,result', [
    ('1.0.20260223-r0-ls124', '1.0.20260223-r0-ls124', 0),
    ('26.09.2', '26.09.2', 0),
    ('v2.1.5-ls230', 'v2.1.5-ls230', 0),
    ('v2.1.5-ls230', 'v2.1.5-ls231', 1),
    ('1.0.20260223-r0-ls124', '1.0.20260223-r1-ls125', 1),
    ('26.09.2', '26.10.0', 1),
    ('1.9.0', '1.10.0', 1),
    ('1.10.0', '1.9.0', -1),
    ('v1.2.0', '1.2', 0),
    ('1.0.0-rc.1', '1.0.0', 1),
    ('1.0.0', '1.0.0-rc.1', -1),
    ('1.0.0-beta.9', '1.0.0-rc.1', 1),
    ('1.0.0+build1', '1.0.0+build2', 0),
    ('abc123', 'abc124', None),
    ('latest', 'latest', 0),
    (None, '1.0.0', None),
    ('1.0.0', None, None),
    ('', '', None),
])
def test_version_precedence(running, candidate, result):
    assert remote_probe.compare_versions(running, candidate) == result


def manifest(version='2.1.0', architecture='arm64', digest=MANIFEST, config=CONFIG):
    return {'Descriptor': {'digest': digest, 'platform': {**PLATFORM, 'architecture': architecture}},
            'OCIManifest': {'config': {'digest': config}, 'annotations': {'org.opencontainers.image.version': version}}}


@pytest.mark.parametrize('running,checked,status', [
    ('26.09.2', '26.09.2', 'current'),
    ('1.0.20260223-r0-ls124', '1.0.20260223-r0-ls124', 'current'),
    ('v2.1.5-ls230', 'v2.1.5-ls230', 'current'),
    ('2.2.0', '2.1.0', 'current'),
    ('2.0.0', '2.1.0', 'available'),
    (None, '2.1.0', 'unverified'),
    ('commit-abc', 'commit-def', 'unverified'),
])
def test_update_check_requires_a_newer_version(monkeypatch, running, checked, status):
    service = demo.service('web', 'example/web:latest', version=running)
    service['platform'] = PLATFORM
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[:4] == ['docker', 'manifest', 'inspect', '--verbose']
        return json.dumps([manifest(checked)])
    monkeypatch.setattr(remote_probe, 'run', run)
    update = remote_probe.check_updates([service])[0]['update']
    assert update['status'] == status
    assert update['version'] == checked and service['version'] == running
    assert update['digest'] == CONFIG and update['manifest_digest'] == MANIFEST
    assert len(calls) == 1


def test_containerd_compares_running_platform_manifest_not_index_to_config(monkeypatch):
    service = demo.service('web', 'example/web:latest', version='2.1.0')
    service.update(image_id=INDEX, image_manifest_digest=MANIFEST, platform=PLATFORM)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[1] == 'manifest'  # No label lookup or image pull when already current.
        return json.dumps([manifest(), manifest('9.0.0', 'amd64', 'sha256:'+'e'*64, 'sha256:'+'f'*64)])
    monkeypatch.setattr(remote_probe, 'run', run)
    result = remote_probe.check_updates([service])[0]['update']
    assert result['status'] == 'current' and result['version'] == '2.1.0'
    assert result['manifest_digest'] == MANIFEST and len(calls) == 1


@pytest.mark.parametrize('containerd,legacy_client', [(False, False), (True, False), (True, True)])
def test_inventory_reads_the_running_immutable_image_and_platform(monkeypatch, containerd, legacy_client):
    descriptor = {'digest': MANIFEST, 'platform': PLATFORM} if containerd else None
    c = {'Id': 'web', 'Name': '/web', 'Image': INDEX if containerd else CONFIG,
         'Config': {'Image': 'example/web:latest', 'Labels': {}}, 'ImageManifestDescriptor': descriptor,
         'NetworkSettings': {}, 'State': {'Status': 'running'}, 'HostConfig': {}}
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == 'ps':
            return 'web'
        if argv[1] == 'inspect':
            return json.dumps([c])
        if legacy_client and '--platform' in argv:
            raise RuntimeError('unknown flag: --platform')
        expected = ['docker', 'image', 'inspect', c['Image']] + (['--platform', 'linux/arm64/v8'] if containerd and not legacy_client else [])
        assert argv == expected
        return json.dumps([{'Os': 'linux', 'Architecture': 'arm64', 'Variant': 'v8',
                            'Config': {'Labels': {'org.opencontainers.image.version': '2.1.0'}}}])
    monkeypatch.setattr(remote_probe, 'run', run)
    service = remote_probe.inventory()[0]
    assert service['version'] == '2.1.0' and service['platform'] == PLATFORM
    assert service['image_manifest_digest'] == (MANIFEST if containerd else None)
    assert len(calls) == (4 if legacy_client else 3)


def test_mixed_running_versions_share_one_registry_check(monkeypatch):
    old = demo.service('old', 'example/web:latest', version='2.0.0')
    new = demo.service('new', 'example/web:latest', version='2.1.0')
    old['platform'] = new['platform'] = PLATFORM
    new.update(image_id=INDEX, image_manifest_digest=MANIFEST)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return json.dumps([manifest()])
    monkeypatch.setattr(remote_probe, 'run', run)
    for services in ([old, new], [new, old]):
        remote_probe.check_updates(services)
        assert old['update']['status'] == 'available' and new['update']['status'] == 'current'
    assert len(calls) == 2


def test_cached_same_version_notifications_clear_without_registry_calls(client, monkeypatch):
    server = module.get_server('atlas')
    snapshot = json.loads(server['snapshot'])
    services = [demo.service(name, 'example/'+name+':latest', update=True, version='2.0.0')
                for name in ('equal', 'older', 'newer', 'unknown')]
    for service, version in zip(services, ('2.0.0', '1.9.0', '2.1.0', None)):
        service['update']['version'] = version
    snapshot['services'] = services
    store.execute('UPDATE servers SET snapshot=? WHERE id=?', (json.dumps(snapshot), 'atlas'))
    monkeypatch.setattr(module.ssh, 'request', lambda *a, **kw: pytest.fail('No registry or SSH calls during dashboard reads'))
    for _ in range(2):
        current = next(s for s in client.get('/api/dashboard').json()['servers'] if s['id'] == 'atlas')
        assert current['updates'] == 1
        assert [s['update']['status'] for s in current['services']] == ['current', 'current', 'available', 'unverified']
        assert [s['version'] for s in current['services']] == ['2.0.0'] * 4
    # The same policy controls the dismissal endpoint / notification counts.
    assert client.post('/api/dismiss', json={'server_id': 'atlas', 'service_id': services[0]['id']}).status_code == 400
    assert client.post('/api/dismiss', json={'server_id': 'atlas', 'service_id': services[2]['id']}).status_code == 200


@pytest.mark.parametrize('action,applied,ok', [('pull', False, True), ('pull_up', True, True), ('up', True, True), ('pull_up', False, False)])
@pytest.mark.parametrize('containerd', [False, True])
def test_action_refresh_clears_only_applied_updates(client, monkeypatch, action, applied, ok, containerd):
    monkeypatch.setattr(store, 'DEMO', False)
    before = module.get_server('atlas')
    snapshot = json.loads(before['snapshot'])
    web = snapshot['services'][0]
    web['update'].update(version='1.143.0', digest=CONFIG, manifest_digest=MANIFEST)
    if containerd:
        web.update(image_id='sha256:'+'e'*64, image_manifest_digest='sha256:'+'f'*64)
    store.execute("UPDATE servers SET snapshot=? WHERE id='atlas'", (json.dumps(snapshot),))
    observed = copy.deepcopy(snapshot)
    changed = observed['services'][0]
    if applied:
        changed.update(id='recreated-web', image_id=INDEX if containerd else CONFIG, version='1.143.0')
        if containerd:
            changed['image_manifest_digest'] = MANIFEST
    for service in observed['services']:
        service['update'] = {'status': 'unchecked'}
    calls = []
    def request(server, payload, on_event=None):
        calls.append(payload['operation'])
        if payload['operation'] == 'execute':
            return {'ok': ok, 'output': 'Done' if ok else 'Apply failed'}
        assert payload['operation'] == 'snapshot' and payload['updates'] is False
        return observed
    monkeypatch.setattr(module.ssh, 'request', request)
    task = submit(client, action)
    assert wait_job(client, task)['status'] == ('succeeded' if ok else 'failed')
    module.refresh_server('atlas', updates=False)
    current = next(s for s in client.get('/api/dashboard').json()['servers'] if s['id'] == 'atlas')
    assert current['services'][0]['update']['status'] == ('current' if applied else 'available')
    assert current['updates'] == (2 if applied else 3)
    assert calls == ['execute', 'snapshot']
    assert module.get_server('atlas')['update_checked'] == before['update_checked']
