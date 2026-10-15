#!/usr/bin/env python3
"""Set deployment resource keys without revealing or replacing existing secrets."""
import argparse
import fcntl
import os
from pathlib import Path
import tempfile

if __package__:
    from . import start
else:
    import start

PROFILES = {
    'standard': {'WEB_MEMORY_MIB':768, 'RECORDER_MEMORY_MIB':768, 'PROCESSING_MIB':32, 'BUFFER_TOTAL_MIB':128, 'WRITE_INTERVAL_SECONDS':1},
    'sbc-1g': {'WEB_MEMORY_MIB':320, 'RECORDER_MEMORY_MIB':384, 'PROCESSING_MIB':8, 'BUFFER_TOTAL_MIB':16, 'WRITE_INTERVAL_SECONDS':10},
}


def validate(values):
    profile=values.get('HARBOUR_RESOURCE_PROFILE','standard')
    if profile not in PROFILES:
        raise ValueError('Unknown Harbour resource profile')
    limits={key:int(values.get('HARBOUR_'+key,default)) for key,default in PROFILES['standard'].items()}
    if not 128<=limits['WEB_MEMORY_MIB']<=65536 or not 192<=limits['RECORDER_MEMORY_MIB']<=65536:
        raise ValueError('Web memory must be at least 128 MiB; recorder memory at least 192 MiB')
    if not 2<=limits['PROCESSING_MIB']<=128 or not 1<=limits['BUFFER_TOTAL_MIB']<=512 or not 1<=limits['WRITE_INTERVAL_SECONDS']<=60:
        raise ValueError('Processing, buffer or write interval is outside its supported range')
    if limits['PROCESSING_MIB']*8>min(limits['WEB_MEMORY_MIB'],limits['RECORDER_MEMORY_MIB']):
        raise ValueError('Processing budget must leave at least eight times its allowance in each memory limit')
    if limits['BUFFER_TOTAL_MIB']>limits['RECORDER_MEMORY_MIB']//4:
        raise ValueError('Recorder payload buffers may use at most one quarter of the recorder memory limit')
    if profile=='sbc-1g' and (limits['WEB_MEMORY_MIB']+limits['RECORDER_MEMORY_MIB']>704 or limits['WRITE_INTERVAL_SECONDS']<5):
        raise ValueError('The sbc-1g profile reserves at least 320 MiB for the OS and batches writes for at least five seconds. Use resource_profile.py to select it.')
    return limits


def apply(path, profile, overrides=None):
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
