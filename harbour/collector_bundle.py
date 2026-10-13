"""The same trusted payload for download and SSH delivery."""
import io
import zipfile
from pathlib import Path

BUNDLES = {
    'call-endpoint': ('harbour-c-remote', 'call_endpoint_install.py',
                      ('call_endpoint_install.py', 'call_endpoint_service.py', 'Dockerfile', 'compose.yaml', 'build-iperf.sh')),
    'resources': ('harbour-resource-recorder', 'resource_install.py',
                  ('resource_agent.py', 'resource_install.py', 'remote_probe.py', 'cpu.py', 'RESOURCE_RECORDER.md')),
    'logins': ('harbour-login-collector', 'collector_install.py',
               ('login_agent.py', 'collector_install.py', 'login_events.m', 'login_events.entitlements', 'LOGIN_COLLECTOR.md')),
}


def build(kind):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for name in BUNDLES[kind][2]:
            deploy = Path(__file__).parent / 'endpoint_deploy'
            if not deploy.exists():
                deploy = Path(__file__).parent.parent / 'deploy' / 'call-test-endpoint'
            source = (deploy / name
                      if kind == 'call-endpoint' and name in ('Dockerfile', 'compose.yaml', 'build-iperf.sh')
                      else Path(__file__).with_name(name))
            bundle.writestr(name, source.read_bytes())
    return archive.getvalue()
