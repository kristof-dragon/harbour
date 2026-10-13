"""Versioned synthetic load presets; bandwidth guidance is not a codec model."""

VERSION = 1
TEAMS = 'https://learn.microsoft.com/en-us/microsoftteams/prepare-network#bandwidth-requirements'
ZOOM = 'https://support.zoom.com/hc/en/article?id=zm_kb&sysparm_article=KB0060748'
APPLE = 'https://support.apple.com/guide/security/facetime-security-seca331c55cd/web'

PROFILES = [
    {'id': 'facetime', 'name': 'FaceTime · provisional video', 'upload_kbps': 2000, 'download_kbps': 2000,
     'basis': 'Harbour approximation', 'source': APPLE,
     'note': 'Editable 2/2 Mbps starting load, not an Apple specification. Calibrate against the affected device and real calls. Apple documents SRTP and peer-to-peer connection negotiation, not a fixed bitrate here.'},
    {'id': 'teams-call', 'name': 'Teams · one-to-one video', 'upload_kbps': 1500, 'download_kbps': 1500,
     'basis': 'Microsoft recommended video bandwidth', 'source': TEAMS,
     'note': 'Recommended per-endpoint video bandwidth. Actual Teams media adapts; this test uses a fixed UDP load.'},
    {'id': 'teams-meeting', 'name': 'Teams · video meeting', 'upload_kbps': 2500, 'download_kbps': 4000,
     'basis': 'Microsoft recommended video bandwidth', 'source': TEAMS,
     'note': 'Recommended per-endpoint meeting video bandwidth. Upload and download intentionally differ.'},
    {'id': 'zoom-call', 'name': 'Zoom · one-to-one 720p', 'upload_kbps': 1200, 'download_kbps': 1200,
     'basis': 'Zoom recommended 720p bandwidth', 'source': ZOOM,
     'note': 'Recommended one-to-one 720p video bandwidth. This does not establish a Zoom call or use Zoom relays.'},
    {'id': 'zoom-meeting', 'name': 'Zoom · group 720p', 'upload_kbps': 2600, 'download_kbps': 1800,
     'basis': 'Zoom recommended 720p bandwidth', 'source': ZOOM,
     'note': 'Recommended group 720p video bandwidth. Gallery views and other resolutions have different requirements.'},
]


def profile(id_):
    return next((dict(p, version=VERSION, reviewed='2026-10-13') for p in PROFILES if p['id'] == id_), None)
