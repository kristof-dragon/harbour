#!/usr/bin/env python3
"""Set deployment resource keys without revealing or replacing existing secrets."""
import argparse
import fcntl
import os
from pathlib import Path
import tempfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harbour import resource_config

if __package__:
    from . import start
else:
    import start

PROFILES = {name: {k.upper(): v for k, v in values.items()} for name, values in resource_config.PROFILES.items()}


def validate(values):
    settings = resource_config.from_env(values)
    return {key.upper(): settings[key] for key in resource_config.FIELDS}


def apply(path, profile, overrides=None):
    if profile not in PROFILES or set(overrides or {}) - set(PROFILES['standard']):
        raise ValueError('Unknown resource setting')
    text, values=start.read_env(path)
    settings={'HARBOUR_RESOURCE_PROFILE':profile, **{'HARBOUR_'+k:str(v) for k,v in PROFILES[profile].items()}}
    settings.update({'HARBOUR_'+k:str(v) for k,v in (overrides or {}).items() if v is not None})
    validate({**values,**settings})
    lines=[line for line in text.splitlines() if line.split('=',1)[0].strip() not in settings]
    content='\n'.join(lines+[k+'='+v for k,v in settings.items()])+'\n'
    fd, name=tempfile.mkstemp(prefix='.env.resources-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as output:
            output.write(content); output.flush(); os.fsync(output.fileno())
        if path.is_symlink() or path.read_text()!=text:
            raise RuntimeError('.env changed; resource settings were not applied')
        os.replace(name,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        Path(name).unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile',choices=PROFILES)
    for name in PROFILES['standard']:
        parser.add_argument('--'+name.lower().replace('_','-'),type=int)
    args=parser.parse_args()
    root=start.ROOT
    fd=os.open(root/'.env.setup.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        apply(root/'.env',args.profile,{key:getattr(args,key.lower()) for key in PROFILES['standard']})
    print('Resource profile saved:',args.profile+'. Run python3 scripts/start.py to apply it.')


if __name__=='__main__':
    main()
