"""The same trusted payload for download and SSH delivery."""
import io
import zipfile
from pathlib import Path

BUNDLES = {
    'resources': ('harbour-resource-recorder', 'resource_install.py',
                  ('resource_agent.py', 'resource_install.py', 'remote_probe.py', 'cpu.py', 'RESOURCE_RECORDER.md')),
    'logins': ('harbour-login-collector', 'collector_install.py',
               ('login_agent.py', 'collector_install.py', 'login_events.m', 'login_events.entitlements', 'LOGIN_COLLECTOR.md')),
}


def build(kind):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for name in BUNDLES[kind][2]:
            bundle.writestr(name, Path(__file__).with_name(name).read_bytes())
    return archive.getvalue()
