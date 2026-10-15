"""Admin-only, bounded client for the host's narrow resource control socket."""
import json
import socket

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import auth, resource_config, store

router = APIRouter(prefix='/api/resource-settings')
SOCKET = '/run/harbour-control/resources.sock'


def exchange(message):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(20)
            client.connect(SOCKET)
            client.sendall(json.dumps(message).encode()+b'\n')
            with client.makefile('rb') as stream:
                raw = stream.readline(32769)
            if len(raw) > 32768:
                raise ValueError('Response too large')
            result = json.loads(raw)
    except (OSError, ValueError):
        raise HTTPException(503, 'Resource controls are unavailable on this deployment. Your current limits remain active.') from None
    if 'error' in result:
        raise HTTPException(result.get('code', 400), result['error'])
    return result


@router.get('')
def status(user=Depends(auth.admin)):
    if store.DEMO:
        return dict(available=False, reason='Resource changes are disabled in the demo.', profiles=resource_config.PROFILES)
    try:
        return {**exchange({'action': 'status'}), 'profiles': resource_config.PROFILES}
    except HTTPException as error:
        return dict(available=False, reason=error.detail, profiles=resource_config.PROFILES)


class Change(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(pattern=r'^[a-f0-9-]{36}$')
    revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    settings: dict


@router.post('', status_code=202)
def apply(body: Change, user=Depends(auth.admin)):
    if store.DEMO:
        raise HTTPException(409, 'Resource changes are disabled in the demo.')
    try:
        resource_config.validate(body.settings)
    except (ValueError, TypeError) as error:
        raise HTTPException(422, str(error)) from None
    return exchange({'action': 'apply', **body.model_dump()})
