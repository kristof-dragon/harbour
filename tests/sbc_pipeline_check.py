"""Real small-board recorder process tree in an isolated, capped Linux container."""
import json
import os
from pathlib import Path
import tempfile
import time

os.environ['HARBOUR_RESOURCE_PROFILE']='sbc-1g'
os.environ['HARBOUR_SECRET']='isolated-process-fixture-'+'x'*32
os.environ['HARBOUR_PASSWORD']='isolated-process-password'
os.environ['HARBOUR_DEMO']='false'

from harbour import network, network_recorder, network_runtime, store


def main():
    with tempfile.TemporaryDirectory(prefix='harbour-sbc-') as directory:
        store.DATA=Path(directory);store.DEMO=False
        store.initialize();network.initialize()
        from harbour import network_diagnostics
        network_diagnostics.initialize()
        store.execute("INSERT INTO servers(id,name,host,port,username,fingerprint,server_type) VALUES ('fixture','Fixture','127.0.0.1',9,'fixture','FIXTURE','openwrt')")
        store.execute('INSERT INTO network_settings VALUES (?,?,?)',('fixture',json.dumps({**network.DEFAULTS,'enabled':True,'interval':.25,'targets':['192.0.2.1'],'archive_enabled':False}),'fixture'))
        pipeline,status=network_recorder.Pipeline(),network_runtime.StatusServer()
        try:
            end=time.monotonic()+24
            largest=0
            while time.monotonic()<end:
                pipeline.tick();status.snapshot=pipeline.health()
                largest=max(largest,sum(w.buffer.size for w in pipeline.workers.values()))
                time.sleep(.01)
            worker=pipeline.active['fixture']
            assert worker.settings['interval']==2 and worker.settings['telemetry_seconds']==10 and not worker.settings['router_probes']
            assert worker.buffer.limit==16*1024*1024 and not worker.buffer.dropped
            assert worker.buffer.committed_samples>=5 and worker.buffer.sequence>=10, worker.health()
            assert worker.restarts==0 and pipeline.writer.is_alive() and pipeline.archiver.is_alive()
            events=Path('/sys/fs/cgroup/memory.events').read_text()
            assert '\noom_kill 0\n' in '\n'+events
            print(json.dumps({'profile':'sbc-1g','committed_samples':worker.buffer.committed_samples,'produced_samples':worker.buffer.sequence,'largest_buffer_bytes':largest,
                'memory_current':int(Path('/sys/fs/cgroup/memory.current').read_text()),'memory_peak':int(Path('/sys/fs/cgroup/memory.peak').read_text()),'events':events},indent=2),flush=True)
        finally:
            pipeline.close();status.close()
        assert not pipeline.workers


if __name__=='__main__':main()
