"""Standalone, escaped HTML reports. Printing/PDF belongs to the browser."""
import datetime as dt
import html
import json
import math
from collections import Counter
from zoneinfo import ZoneInfo

from . import report_facts


CSS = '''
:root{color-scheme:light;font-family:Inter,ui-sans-serif,system-ui,sans-serif;color:#172b3a;background:#eef2f5}
*{box-sizing:border-box}body{margin:0}main{max-width:1120px;margin:36px auto;background:white;padding:48px 56px;box-shadow:0 8px 35px #182b3a12}
header{border-top:6px solid #167b80;padding-top:24px;border-bottom:1px solid #d5dfe4;padding-bottom:24px}
.eyebrow{font-size:12px;font-weight:750;letter-spacing:.14em;text-transform:uppercase;color:#167b80}h1{font-size:34px;line-height:1.15;margin:12px 0}h2{font-size:23px;margin:32px 0 14px}h3{font-size:16px;margin:8px 0}p{line-height:1.55}.muted,small{color:#546879}small{font-size:12px}.notice{background:#fff7e7;border-left:4px solid #c08624;padding:12px 16px}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:24px 0}.metric{background:#f1f7f8;padding:16px;border-radius:6px}.metric b{display:block;font-size:27px;margin-bottom:6px}.metric span{font-size:12px;color:#426270}
table{width:100%;border-collapse:collapse;font-size:12px;margin:12px 0 22px;table-layout:fixed}th{text-align:left;background:#eef4f6;color:#32505d}th,td{padding:9px 8px;border-bottom:1px solid #dce5e9;vertical-align:top;overflow-wrap:anywhere}.timeline{border-left:2px solid #b9d9db;padding-left:20px}.entry{padding:0 0 22px 14px;border-bottom:1px solid #e4ebee;margin:0 0 20px;position:relative}.entry:before{content:"";position:absolute;left:-27px;top:7px;width:11px;height:11px;border-radius:50%;background:#167b80}.stamp{font-size:12px;color:#386a75;font-weight:700}.tag{font-size:11px;background:#edf3f5;border-radius:3px;padding:3px 6px;margin-left:8px}.evidence{background:#f7f9fa;border:1px solid #e0e7ea;border-radius:4px;padding:12px;margin-top:10px}.evidence p{font-size:12px;margin:3px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:10px/1.45 ui-monospace,monospace;margin:6px 0}.quality{font-size:12px;color:#6b5531}.day{margin-top:44px}.method{font-size:12px;color:#506574;border-top:1px solid #dce5e9;margin-top:30px;padding-top:16px}footer{font-size:11px;color:#6b7b86;margin-top:32px}.printbar{max-width:1120px;margin:20px auto 0;display:flex;justify-content:space-between;align-items:center;padding:0 12px;font-size:13px}button{padding:10px 18px;background:#167b80;color:white;border:0;border-radius:5px;font:inherit;cursor:pointer}a{color:#126d75}
@media(max-width:650px){main{margin:12px;padding:24px 16px}.metrics{grid-template-columns:repeat(2,1fr)}h1{font-size:27px}.table-scroll{overflow:auto}table{min-width:580px}.printbar{padding:0 20px}.timeline{padding-left:12px}.entry:before{left:-19px}}
@page{size:A4;margin:16mm 14mm 18mm}
@media print{body,main{background:white}main{margin:0;padding:0;max-width:none;box-shadow:none;font-size:10pt}.printbar{display:none}header{padding-top:14px}h1{font-size:25pt}h2{font-size:17pt;break-after:avoid}h3{break-after:avoid}.day+.day{break-before:page}.metrics{grid-template-columns:repeat(4,1fr)}.metric b{font-size:20pt}.entry{break-inside:avoid}.evidence{break-inside:auto}table{font-size:8pt;min-width:0}thead{display:table-header-group}tr{break-inside:avoid}pre{font-size:7.2pt}.table-scroll{overflow:visible}a{color:inherit;text-decoration:none}.notice,.metric,th{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
'''


def e(value):
    return html.escape(str(value if value is not None else 'Unavailable'), quote=True)


def number(value, digits=1):
    return f'{value:,.{digits}f}' if isinstance(value, (int, float)) and math.isfinite(value) else 'Unavailable'


def timestamp(at, zone):
    return dt.datetime.fromtimestamp(at, zone).strftime('%d %b %Y · %H:%M:%S %Z')


def evidence_block(items, provenance=None):
    result = ''
    for item in items:
        if not isinstance(item, dict):
            result += '<p>'+e(item)+'</p>'
            continue
        values = []
        for key, label in (('status','Result'),('rtt_ms','Round-trip latency (ms)'),('latency_limit_ms','Review threshold (ms)'),
                           ('timing_quality','Timing quality'),('source','Observer'),('target','Target'),('line','Log excerpt'),('detail','Detail')):
            if item.get(key) is not None:
                values.append(label+': '+str(item[key]))
        if isinstance(item.get('at'), (int,float)):
            values.insert(0, dt.datetime.fromtimestamp(item['at'],dt.timezone.utc).isoformat(timespec='milliseconds'))
        if item.get('kind') == 'telemetry':
            m=item.get('metrics',{}); ow=m.get('openwrt',{})
            if m.get('cpu') is not None: values.append('Router CPU: '+number(m['cpu'])+'%')
            if m.get('memory',{}).get('percent') is not None: values.append('Memory used: '+number(m['memory']['percent'])+'%')
            for key, label in (('cpu_percent','Per-core CPU (%)'),('softirq_percent','Softirq (%)'),('conntrack','Connection tracking')):
                if ow.get(key): values.append(label+': '+json.dumps(ow[key],ensure_ascii=False))
            # Preserve the actual measurements behind queue/interface/radio/Wi-Fi findings.
            for key in ('interfaces','radio_stats','queue_stats','stations'):
                if ow.get(key): values.append(key.replace('_',' ').title()+': '+json.dumps(ow[key],ensure_ascii=False,separators=(',',':')))
        if values:
            result += ''.join('<p>'+e(v)+'</p>' for v in values)
            identity={k:item[k] for k in ('run_id','sample_key','provenance') if k in item}
            if identity: result += '<pre>'+e(json.dumps(identity,ensure_ascii=False,separators=(',',':')))+'</pre>'
        else:
            result += '<pre>'+e(json.dumps(item, ensure_ascii=False, indent=2, allow_nan=False))+'</pre>'
    if provenance:
        result += '<p>Source: '+e(json.dumps(provenance, separators=(',', ':')))+'</p>'
    return '<div class="evidence"><b>Supporting evidence</b>'+result+'</div>' if result else '<p class="quality">No retained supporting excerpt is available.</p>'


def entry(item, zone):
    at, end = item['at'], item.get('end', item['at'])
    when = timestamp(at, zone)+((' — '+timestamp(end, zone)) if end != at else '')
    return ('<article class="entry"><div class="stamp">'+e(when)+'<span class="tag">'+e(item['kind'])+'</span></div>'
        '<h3>'+e(item['title'])+'</h3><p>'+e(item.get('detail', ''))+'</p>'
        +''.join('<p class="quality">'+e(q)+'</p>' for q in item.get('quality', []))
        +evidence_block(item.get('evidence', []), item.get('provenance'))+'</article>')


def table(headers, rows):
    return '<div class="table-scroll"><table><thead><tr>'+''.join('<th>'+e(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+e(v)+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table></div>'


def path_table(paths):
    rows = []
    for p in paths:
        usable, replies = p['usable'], p['replies']
        rank, cumulative, p95 = math.ceil(replies*.95), 0, None
        for index, count in enumerate(p['histogram']):
            cumulative += count
            if replies and cumulative >= rank:
                p95 = ('≤ '+str(report_facts.BINS[index])+' ms') if index < len(report_facts.BINS) else '> 10000 ms'
                break
        rows.append([p['source']+' → '+p['target'], str(usable)+' / '+str(p['attempts']),
                     str(p['timeouts'])+' / '+(number(100*p['timeouts']/usable)+'%' if usable else 'unavailable'),
                     str(p['high'])+' > '+str(p['threshold_ms'])+' ms',
                     number(p['sum_ms']/replies) if replies else 'Unavailable', p95 or 'Unavailable',
                     number(p['max_ms']), str(p['uncertain'])])
    return table(['Path', 'Usable / attempts', 'Timeouts / rate', 'Slow replies', 'Mean ms', 'p95 bucket', 'Peak ms', 'Uncertain'], rows)


def merged_paths(days):
    totals = {}
    for day in days:
        for row in day['paths']:
            key = (row['source'], row['target'], row['threshold_ms'])
            if key not in totals:
                totals[key] = {**row, 'histogram': list(row['histogram'])}
            else:
                total = totals[key]
                for field in (*report_facts.COUNTS, 'sum_ms', 'observed_seconds'):
                    total[field] += row[field]
                values = [v for v in (total['max_ms'], row['max_ms']) if v is not None]
                total['max_ms'] = max(values) if values else None
                total['histogram'] = [a+b for a, b in zip(total['histogram'], row['histogram'])]
    return list(totals.values())


def render(server, period):
    zone = ZoneInfo(period['timezone']); days = period['days']
    calls = {c['source_id']: c for d in days for c in d['calls']}
    samples = sum(sum(d['kinds'].values()) for d in days)
    markers = sum(d['markers'] for d in days)
    label = period['start_day'] if period['start_day']==period['end_day'] else period['start_day']+' to '+period['end_day']
    content = '<header><div class="eyebrow">Harbour · Network evidence report</div><h1>'+e(server)+'</h1><p>'+e(label)+' · '+e(period['timezone'])+'</p><p class="muted">Chronological observations, retained evidence and measured statistics.</p></header>'
    content += '<div class="metrics">'+''.join('<div class="metric"><b>'+e(value)+'</b><span>'+e(title)+'</span></div>' for value, title in [(len(days),'Compiled days'),(f'{samples:,}','Recorded observations'),(markers,'Incident markers'),(len(calls),'Distinct call tests')])+'</div>'
    if period['missing_days']:
        content += '<p class="notice"><b>Missing daily reports:</b> '+e(', '.join(period['missing_days']))+'. Missing days are excluded from totals; they do not represent healthy coverage.</p>'
    if len(days)>1:
        content += '<h2>Period statistics</h2><p class="muted">Counts and RTT distributions are combined from saved days. Percentages are recalculated from their denominators.</p>'+path_table(merged_paths(days))
    if calls:
        content += '<h2>Call-test register</h2>'+table(['Started', 'Profile / schedule', 'Status', 'Saved conclusion'],
            [[timestamp(c['start'], zone), c.get('context',{}).get('schedule_name') or c.get('context',{}).get('profile') or 'Synthetic test', c.get('status'), c.get('conclusion')] for c in sorted(calls.values(), key=lambda c:c['start'])])
        content += '<p class="muted">Tests crossing a day boundary appear in both daily timelines, but once in this register. Whole-test receiver summaries are not calendar-day loss rates.</p>'
    for day in days:
        content += '<section class="day"><h2>'+e(dt.date.fromisoformat(day['day']).strftime('%A, %d %B %Y'))+'</h2>'
        content += '<p class="muted">'+str(day['minutes_with_recordings'])+' of '+str(day['minutes_in_day'])+' minutes contain recordings. A minute with one sample is not a full minute of confirmed coverage.</p>'
        entries = list(day['timeline'])
        for call in day['calls']:
            entries.append({'at': max(call['start'],day['start']), 'end': min(call['end'],day['end']), 'kind': 'Synthetic call test',
                'title': call.get('conclusion','Test record'), 'detail': 'Status: '+str(call.get('status')),
                'evidence': [{'test_id':call['source_id'], 'directions':call.get('directions',[]), 'load_response':call.get('load_response',[])}],
                'quality': call.get('quality',[])})
            for episode in call.get('episodes', []):
                at = episode.get('start', episode.get('at', call['start']))
                end = episode.get('end',at)
                if not at < day['end'] or (end <= day['start'] and at < day['start']):
                    continue
                refs = episode.get('evidence_refs', [])
                entries.append({'at':max(at,day['start']), 'end':min(end,day['end']), 'kind':'Call-flow episode',
                    'title':episode.get('conclusion') or episode.get('label') or episode.get('classification','Impairment'),
                    'detail': f"{episode.get('observations', 'Unknown number of')} observations; sampled span {number(episode.get('sampled_span_seconds'))} s; timing uncertainty ±{number(episode.get('uncertainty_seconds'),3)} s. Test {call['source_id']}.",
                    'evidence': [call.get('evidence',{}).get(ref, {'unavailable_reference':ref}) for ref in refs],
                    'quality': ['Timing correlation is limited by the uncertainty stated in the evidence.']})
            if call.get('episodes_omitted'):
                content += '<p class="notice">'+e(call['episodes_omitted'])+' call episodes were omitted by the source analyzer; this report cannot reconstruct them.</p>'
        entries.sort(key=lambda item:(item['at'],item.get('id','')))
        content += '<h3>Reconciled timeline</h3><div class="timeline">'+(''.join(entry(item,zone) for item in entries) if entries else '<p>No incidents or review signals in the available evidence. Check the coverage before interpreting this.</p>')+'</div>'
        content += '<h3>Final daily statistics</h3>'+path_table(day['paths'])
        content += table(['Recording type','Observations'],sorted(day['kinds'].items()))
        if day.get('telemetry'):
            content += table(['Router measurement','Samples','Minimum','Mean','Maximum'],
                [[k.replace('_',' '),v['count'],number(v['min']),number(v['sum']/v['count']),number(v['max'])] for k,v in day['telemetry'].items()])
        content += '<div class="method">'+''.join('<p>'+e(q)+'</p>' for q in day['quality'])+'<p>p95 is the upper edge of the retained histogram bucket. Means use reply counts. Timeout rates exclude uncertain attempts and late-reply records. Coverage counts observed one-second bins, not inferred uptime.</p><p>Compiled '+e(timestamp(day['generated'],zone))+'. Raw source archives: '+e(', '.join(m['archive_id'] for m in day['manifests']) or 'none')+'.</p></div></section>'
    content += '<footer>Harbour · Deterministic local analysis · Root cause is not established by temporal association. This document contains selected retained evidence, not full raw recordings.</footer>'
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+e(server+' · '+label+' · Harbour')+'</title><style>'+CSS+'</style></head><body><div class="printbar"><span>Use your browser’s Print command to save this report as PDF.</span></div><main>'+content+'</main></body></html>'
