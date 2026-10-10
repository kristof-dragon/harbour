"""Disk writer process. Producers retain batches until this process commits."""
import json
import logging
import time

from . import network, store
from .network_buffer import Channel


def configuration():
    active = {}
    if not store.DEMO:
        for server in store.rows("SELECT * FROM servers WHERE server_type='openwrt' AND monitoring_enabled=1"):
            settings, revision = network.config(server['id'])
            if settings['enabled']:
                active[server['id']] = (server, network.Settings(**settings).model_dump(), revision)
    return active


def commit(job):
    rows = [json.loads(row) for row in job['rows']]
    if rows:
        rows.append({'at':time.time(), 'kind':'recorder_health', 'run_id':job['run']['id'],
                     **{k:v for k,v in job['status'].items() if k in (
                         'buffer_bytes', 'buffer_limit_bytes', 'buffer_samples', 'oldest_unsaved_seconds',
                         'dropped_before_storage', 'process_restarts', 'probe_online', 'router_collector_online')}})
    return network.persist(job['server_id'], rows,
                           job['status'], job['signature'], job['revision'], job['run'], job['through'])


def run(sock, stop, data_directory, demo):
    from pathlib import Path
    store.DATA, store.DEMO = Path(data_directory), demo
    channel = Channel(sock)
    try:
        while not stop.is_set() and not channel.closed:
            channel.flush()
            request = channel.receive() if not channel.outgoing else None
            if request is None:
                stop.wait(.005)
                continue
            ack, rejected, errors = [], [], []
            for job in request['jobs']:
                try:
                    accepted = commit(job)
                    ack.append((job['run']['id'], job['through']))
                    if not accepted:
                        rejected.append(job['run']['id'])
                except Exception:
                    logging.exception('Network batch write failed; retaining RAM buffer')
                    errors.append(job['run']['id'])
            channel.send({'ack':ack, 'rejected':rejected, 'errors':errors,
                          'written_at':time.time()})
    finally:
        channel.close()
