"""Disposable Linux workload; never point HARBOUR_DATA at a live installation.

Run in an isolated, memory-capped container with this repo mounted read-only.
Exercises actual SQLite recording, archive checkpoints, daily compilation and
call analysis. No network, credentials or live host access is required.
"""
import datetime as dt
import json
import os
from pathlib import Path
import resource
import tempfile
import time

from harbour import call_evidence, demo, logins, network, network_archives, network_reports, store
from harbour import resource_limits as limits


def io_bytes():
    return int(dict(line.split(': ',1) for line in Path('/proc/self/io').read_text().splitlines())['write_bytes'])


def memory():
    root=Path('/sys/fs/cgroup')
    return {name:(root/name).read_text().strip() for name in ('memory.current','memory.peak','memory.max','memory.events')}


def main():
    with tempfile.TemporaryDirectory(prefix='harbour-resource-check-') as directory:
        store.DATA=Path(directory);store.DEMO=True
        os.environ['HARBOUR_SECRET']='isolated-test-secret-'+'x'*40
        os.environ['HARBOUR_PASSWORD']='isolated-test-password'
        store.initialize();network.initialize();demo.seed()
        from harbour import network_diagnostics
        network_diagnostics.initialize()
        store.execute("UPDATE servers SET server_type='openwrt',monitoring_enabled=1 WHERE id='atlas'")
        store.DEMO=False
        settings=limits.settings({**network.DEFAULTS,'enabled':True,'interval':.5,'targets':['192.0.2.1','192.0.2.2','192.0.2.3']})
        store.execute('INSERT OR REPLACE INTO network_settings VALUES (?,?,?)',('atlas',json.dumps(settings),'test-revision'))
        server=store.one("SELECT * FROM servers WHERE id='atlas'")
        signature=logins.connection_signature(server)
        base=dt.datetime(2026,10,13,12,tzinfo=dt.timezone.utc).timestamp()
        run={'id':'synthetic-run','started':base,'config':settings,'observer':'Isolated synthetic fixture'}
        metric={'cpu':95, 'boot_id':'fixture', 'memory':{'percent':50},
            'inventory':'x'*55000, 'openwrt':{'sample_seconds':settings['telemetry_seconds'], 'cpu_percent':{'cpu':95},
            'interfaces':{str(n):{'rx_mbps':10,'tx_mbps':5,'rx_errors_delta':0,'tx_errors_delta':0,'rx_dropped_delta':0,'tx_dropped_delta':0} for n in range(19)},
            'stations':[{'mac':str(n),'interface':'wifi','tx_packets_delta':20,'tx_retries_delta':1,'tx_failed_delta':0} for n in range(11)],
            'queue_stats':[{'device':str(n),'discipline':'fq','backlog_bytes':0} for n in range(51)],
            'radio_stats':{'wifi':{'busy_percent':20}}, 'routes':'default dev wan','rules':'lookup main'}}
        commits=logical=through=0
        before=io_bytes()
        # Ten minutes of continuous collection, accelerated; one DB commit per configured cadence.
        for tick in range(0,600,limits.flush_seconds()):
            rows=[]
            for second in range(tick,tick+limits.flush_seconds()):
                if second%settings['telemetry_seconds']==0:
                    row={'at':base+second,'kind':'telemetry','metrics':limits.telemetry(metric) if limits.small() else metric,'run_id':run['id']}
                    rows.append(row)
                if second%int(max(1,settings['interval']))==0:
                    for target in settings['targets']:
                        rows.append({'at':base+second,'kind':'probe','target':target,'source':'recorder','status':'reply','rtt_ms':200 if second%60==0 else 4,'run_id':run['id']})
            logical+=sum(len(json.dumps(row).encode()) for row in rows)
            through+=len(rows)
            assert network.persist('atlas',rows,{},signature,'test-revision',run,through)
            commits+=1
        written=io_bytes()-before
        # Archive the actual stored telemetry, including large repeated inventory on standard.
        while store.one("SELECT 1 FROM network_samples WHERE server_id='atlas' AND archived=0 LIMIT 1"):
            network_archives.maintain('atlas',settings)
            status=store.one("SELECT error FROM network_archive_status WHERE server_id='atlas'")
            assert not status['error'],status
        archive=store.one("SELECT * FROM network_archives WHERE server_id='atlas'")
        # Re-import the legacy path with the same CRC/checkpoint validation.
        store.execute('DELETE FROM report_archives')
        network_reports.import_archive(archive)
        assert network_reports.ready(archive)
        # Generate 24 hours of compact facts, streaming minute batches (three targets).
        day_start=network_reports.bounds('2026-10-13','Europe/London')[0]
        for minute in range(1440):
            samples=[{'at':day_start+minute*60+n,'kind':'probe','source':'recorder','target':t,'status':'reply','rtt_ms':200 if n==0 else 4}
                     for n in (0,20,40) for t in settings['targets']]
            compact=network_reports.facts.compact(samples,[],settings,[],archive['id'],100000+minute*9)
            with store.db() as con:
                network_reports.save_chunk(con,archive,'fixture:'+str(minute),compact)
        report=network_reports.compile_day('atlas',dt.date(2026,10,13),network_reports.policy('atlas'))
        assert report['minutes_with_recordings']==1440
        # An hour of realistic multi-interface call-window evidence must fit the profile's normal analysis workload.
        rows,truncated=network.samples('atlas',base,base+600,300000,diagnostic=True)
        assert not truncated
        hour=[]
        for part in range(6):
            for row in rows:
                sample=json.loads(json.dumps(row))
                sample['at']+=part*600
                sample['id']=len(hour)+1
                hour.append(sample)
        rows=hour
        evidence={'remote':{'records':[]},'local':{'samples':rows,'truncated':False,'settings':settings,'runs':[{'id':run['id'],'config':settings}],'server_name':'Fixture'},'window':{'start':base,'end':base+3600}}
        test={'id':'test','server_id':'atlas','status':'complete','result':{'load_started':base+10,'load_finished':base+3590}}
        evidence['analysis']=call_evidence.analyze(test,evidence)
        payload=limits.pack(evidence)
        assert limits.unpack(payload)['local']['truncated'] is False
        print(json.dumps({'profile':'sbc-1g' if limits.small() else 'standard','simulated_capture_seconds':600,'commits':commits,
            'logical_recording_bytes':logical,'recording_kernel_write_bytes':written,'report_minutes':report['minutes_with_recordings'],
            'archive_bytes':archive['size'],'call_evidence_bytes':len(payload),'call_window_seconds':3600,'call_samples':len(rows),'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'memory':memory()},indent=2))


if __name__=='__main__': main()
