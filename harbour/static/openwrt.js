'use strict';
const networkViews=new Map();
function onboardOpenWrt(){return (document.querySelector('#onboard-form [name=server_type]')?.value||state.connection?.server_type)==='openwrt';}
function updateOpenWrtOnboarding(){
  const form=$('#onboard-form');if(!form)return;
  const router=onboardOpenWrt();
  form.querySelectorAll('[data-action=install-key]').forEach(b=>b.disabled=router||state.demo);
  if(state.key&&$('#key-output'))$('#key-output').innerHTML=keyOutput();
}
document.addEventListener('change',event=>{if(event.target.matches('#onboard-form [name=server_type]'))updateOpenWrtOnboarding();});
function networkView(s){if(!networkViews.has(s.id))networkViews.set(s.id,{loaded:0,busy:false,data:null,readings:null,error:null});return networkViews.get(s.id);}
function networkNumber(n,digits=1){return Number.isFinite(n)?n.toFixed(digits):'—';}
function networkRetention(c){
  const raw=[`${c.retention_hours??168} hours`];
  if(c.max_rows)raw.push(`${Number(c.max_rows).toLocaleString()} rows`);
  if(c.max_payload_mib)raw.push(`${c.max_payload_mib} MiB of payload`);
  return raw.join(' / ')+(raw.length>1?', whichever fills first':'');
}
function networkArchives(s,d){
  const a=d.archives||{files:[]},c=d.settings,files=a.files||[];
  return `<h3>Daily compressed archives</h3>
    <p class="hint">${c.archive_enabled?'Daily archiving enabled':'Daily archiving disabled'} · ${c.archive_retention_days??90} days${c.archive_max_mib?` / ${c.archive_max_mib} MiB compressed, whichever fills first`:''}. Each file covers an archive day in UTC; original measurement times are preserved. Files finish after midnight, even when recording is paused.</p>
    <p class="hint">Saved on this Harbour server in <code>${e(a.directory||'network-archives')}</code>. ${networkNumber((a.bytes||0)/1048576)} MiB in ${a.file_count||0} files. Today's file is built throughout the day before raw readings are trimmed.</p>
    ${a.error?`<p class="form-error">${e(a.error)}</p>`:''}
    <div class="network-table"><table><thead><tr><th>Archive day (UTC)</th><th>Readings</th><th>Compressed size</th><th>File</th></tr></thead><tbody>${files.map(file=>`<tr><td>${e(new Date(file.day*1000).toISOString().slice(0,10))}</td><td>${Number(file.sample_count).toLocaleString()}</td><td>${networkNumber(file.size/1048576)} MiB</td><td>${file.complete?`<a href="/api/servers/${encodeURIComponent(s.id)}/network/archives/${encodeURIComponent(file.id)}">Download .jsonl.gz</a>`:'Collecting · available after midnight'}</td></tr>`).join('')||'<tr><td colspan="4">No daily archives yet. Enable recording to collect evidence.</td></tr>'}</tbody></table></div>
    ${a.file_count>files.length?'<p class="hint">Showing the latest 100 files. Older retained files remain in the archive directory.</p>':''}`;
}
function networkChart(label,unit,series,start,end){
  const colors=['#4e9cf5','#4cc9a6','#e3a858','#c793ee','#eb7e9d','#66c9d9'];
  const visible=series.map(s=>({...s,points:s.points.filter(p=>Number.isFinite(p[0])&&p[0]>=start&&p[0]<=end).sort((a,b)=>a[0]-b[0])}));
  const values=visible.flatMap(s=>s.points.map(p=>p[1])).filter(Number.isFinite);
  if(!values.length)return `<article class="network-chart"><h3>${e(label)}</h3><p class="network-chart-empty hint">No usable readings in this window. Collection gaps and missing counter baselines are not shown as zero.</p></article>`;
  let low=Math.min(0,...values),high=Math.max(...values);if(high===low)high=low+1;
  const x=t=>8+(t-start)/Math.max(1,end-start)*984,y=v=>170-(v-low)/(high-low)*160;
  let paths=[10,90,170].map(y=>`<line x1="0" x2="1000" y1="${y}" y2="${y}" stroke="currentColor" opacity=".15" vector-effect="non-scaling-stroke"/>`).join('');
  const markers=networkView(current()).data?.events||[];
  for(const event of markers)if(event.measured>=start&&event.measured<=end)paths+=`<line x1="${x(event.measured)}" x2="${x(event.measured)}" y1="10" y2="170" stroke="#e3a858" stroke-dasharray="3 3" vector-effect="non-scaling-stroke"><title>${e(event.label)}</title></line>`;
  visible.forEach((s,i)=>{
    let segment=[];const flush=()=>{
      if(segment.length===1)paths+=`<path class="network-point" d="M${segment[0]} l0.01,0" fill="none" stroke="${colors[i%colors.length]}" stroke-width="5" stroke-linecap="round" vector-effect="non-scaling-stroke"/>`;
      else if(segment.length>1)paths+=`<polyline points="${segment.join(' ')}" fill="none" stroke="${colors[i%colors.length]}" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
      segment=[];
    };
    let previous=null;
    for(const [time,value] of s.points){if(!Number.isFinite(value)||(previous!==null&&time-previous>(s.maxGap||3)))flush();if(Number.isFinite(value))segment.push(`${x(time).toFixed(1)},${y(value).toFixed(1)}`);previous=time;}flush();
  });
  const tick=value=>new Intl.NumberFormat(undefined,{notation:'compact',maximumFractionDigits:1}).format(value);
  return `<article class="network-chart"><div class="between"><h3>${e(label)}</h3><span class="hint">${e(unit)}</span></div><div class="network-plot"><div class="network-axis"><span>${e(tick(high))}</span><span>${e(tick((high+low)/2))}</span><span>${e(tick(low))}</span></div><svg viewBox="0 0 1000 180" preserveAspectRatio="none" role="img" aria-label="${e(label)} over the last two minutes">${paths}</svg></div><div class="network-times"><span>${e(new Date(start*1000).toLocaleTimeString())}</span><span>${e(new Date(end*1000).toLocaleTimeString())}</span></div><div class="network-legend">${visible.map((s,i)=>`<span><i style="background:${colors[i%colors.length]}"></i>${e(s.name)}</span>`).join('')}</div></article>`;
}
function renderNetwork(s){
  const v=networkView(s),d=v.data,r=v.readings,settings=d?.settings;
  const controls=admin()?`${button('network-settings','Recording settings','settings','small')}${button('network-degrade','Mark degradation','warning','small','data-label="Call degradation"')}${button('network-recover','Mark recovery','check','small','data-label="Call recovery"')}`:'';
  if(!d)return `<section class="network-view" data-key="network:${e(s.id)}"><div class="between"><h2>Network diagnostics</h2>${controls}</div><p>${e(v.error||'Loading read-only diagnostics…')}</p></section>`;
  const rows=r?.samples||[],telemetry=rows.filter(x=>x.kind==='telemetry'),latest=telemetry.at(-1)?.metrics,device=latest?.openwrt||d.device;
  const status=d.status,recording=settings.enabled&&s.monitoring_enabled;
  const board=device?.board||status.device||{},capabilities=device?.capabilities||status.capabilities||{};
  const targets=[...new Set(rows.filter(x=>x.kind==='probe').map(x=>`${x.source}|${x.target}`))];
  const probeRows=targets.map(key=>{
    const [source,target]=key.split('|'),attempts=rows.filter(x=>x.kind==='probe'&&x.source===source&&x.target===target&&x.status!=='late_reply');
    const replies=attempts.filter(x=>x.status==='reply'),timeouts=attempts.filter(x=>x.status==='timeout');
    const ms=replies.map(x=>x.rtt_ms).filter(Number.isFinite).sort((a,b)=>a-b);
    return {name:`${source==='router'?'Router':'Recorder'} → ${target}`,points:attempts.map(x=>[x.at,x.status==='reply'?x.rtt_ms:null]),maxGap:source==='router'?3:settings.interval*3,
      sent:attempts.length,misses:timeouts.length,p95:ms.length?ms[Math.ceil(ms.length*.95)-1]:null,
      peak:ms.length?ms.at(-1):null};
  });
  const start=r?.start||Date.now()/1000-120,end=r?.end||Date.now()/1000;
  const cpuNames=[...new Set(telemetry.flatMap(x=>Object.keys(x.metrics.openwrt.cpu_percent||{})))];
  const cpuSeries=cpuNames.map(name=>({name:name==='cpu'?'Total':name,points:telemetry.map(x=>[x.at,x.metrics.openwrt.cpu_percent?.[name]])}));
  let wan=settings.wan_device;
  if(!wan)wan=(device?.network||[]).find(n=>n.interface==='wan')?.l3_device||'';
  const wanSeries=['rx_mbps','tx_mbps'].map(key=>({name:key==='rx_mbps'?'Download':'Upload',points:telemetry.map(x=>[x.at,x.metrics.openwrt.interfaces?.[wan]?.[key]])}));
  const mac=settings.client_mac.toLowerCase();
  const stations=(device?.stations||[]).filter(st=>!mac||st.mac===mac);
  const wifiSeries=stations.map(st=>({name:`${st.mac} · ${st.interface}`,points:telemetry.map(x=>[x.at,x.metrics.openwrt.stations.find(a=>a.mac===st.mac&&a.interface===st.interface)?.tx_retries_delta])}));
  const gaps=rows.filter(x=>x.kind==='gap');
  const queueNames=[...new Set(telemetry.flatMap(x=>(x.metrics.openwrt.queue_stats||[]).filter(q=>!['noqueue','ingress','clsact'].includes(q.discipline)&&Number.isFinite(q.backlog_bytes)).map(q=>q.device+' / '+q.discipline)))];
  const queues=queueNames.map(name=>({name,points:telemetry.map(x=>[x.at,x.metrics.openwrt.queue_stats?.find(q=>q.device+' / '+q.discipline===name)?.backlog_bytes])}));
  const softirq=cpuNames.filter(n=>n!=='cpu').map(name=>({name,points:telemetry.map(x=>[x.at,x.metrics.openwrt.softirq_percent?.[name]])}));
  return `<section class="network-view" data-key="network:${e(s.id)}">
    <div class="between"><h2>Network diagnostics</h2><div class="flex">${controls}</div></div>
    <p><span class="tag ${recording&&status.recorder_online?'green':''}">${!recording?'Recording stopped':status.recorder_online?'Local recorder active':'Recorder unavailable'}</span> ${e(board.model||s.metrics?.model||'Device discovery pending')} · ${e(board.release?.description||s.metrics?.os||'OpenWRT')}</p>
    <p class="hint">Read-only inspection · ${settings.interval*1000} ms recorder probes · approximately 1 s router samples · ${e(status.observer||'On-site recorder')}<br>Times are aligned to the recorder clock. Measurements describe the recorder’s network path, including container networking when deployed with Compose. IP probes do not perform DNS lookups.</p>
    ${recording&&!status.recorder_online?'<p class="form-error">The separate network-recorder service has not reported recently. Samples are unavailable; this is not evidence of a router outage.</p>':''}
    ${status.telemetry_error?`<p class="form-error">${e(status.telemetry_error)}</p>`:''}
    ${Object.entries(status.probe_errors||{}).map(([t,error])=>`<p class="form-error">${e(t)}: ${e(error)}</p>`).join('')}
    ${status.lan_probe?`<p class="hint">${e(status.lan_probe)}</p>`:''}
    <div class="network-capabilities">${Object.entries(capabilities).map(([name,yes])=>`<span class="tag">${e(name.replaceAll('_',' '))}: ${yes?'available':'unavailable'}</span>`).join('')}</div>
    <p class="hint">Last two minutes · ${gaps.length} collection gaps · ${telemetry.length} router readings · ${status.dropped_before_storage||0} samples lost before storage${r?.truncated?' · view truncated':''}. Probe timeouts are missed reply deadlines, not proof of an ISP fault. Dots show isolated readings; breaks show missing samples.</p>
    <div class="network-table"><table><thead><tr><th>Probe path</th><th>Attempts</th><th>Timeouts</th><th>p95 RTT</th><th>Peak RTT</th></tr></thead><tbody>${probeRows.map(p=>`<tr><td>${e(p.name)}</td><td>${p.sent}</td><td>${p.misses}</td><td>${networkNumber(p.p95)} ms</td><td>${networkNumber(p.peak)} ms</td></tr>`).join('')||'<tr><td colspan="5">Enable recording to collect measurements.</td></tr>'}</tbody></table></div>
    <div class="network-charts">${networkChart('Round-trip latency','ms',probeRows,start,end)}${networkChart('Router CPU','%',cpuSeries,start,end)}${networkChart(`WAN traffic${wan?' · '+wan:''}`,'Mbps',wanSeries,start,end)}${networkChart('Wi-Fi transmit retries','counter change per sample',wifiSeries,start,end)}${networkChart('Queue backlog','bytes',queues,start,end)}${networkChart('Router softirq time','%',softirq,start,end)}</div>
    <p class="hint">Wi-Fi retries describe router-to-client transmissions. Missing readings remain gaps. Router probes use ${e(settings.wan_device||'the router’s routing policy — select a WAN device to bind them')}; confirm the route before interpreting them as direct WAN tests.</p>
    <h3>Wireless clients</h3><div class="network-table"><table><thead><tr><th>Client</th><th>Interface</th><th>Signal</th><th>TX / RX rate</th><th>Retries / failures Δ</th></tr></thead><tbody>${stations.map(st=>`<tr><td>${e(st.mac)}</td><td>${e(st.interface)}</td><td>${networkNumber(st.signal_dbm,0)} dBm</td><td>${networkNumber(st.tx_mbps)} / ${networkNumber(st.rx_mbps)} Mbps</td><td>${networkNumber(st.tx_retries_delta,0)} / ${networkNumber(st.tx_failed_delta,0)}</td></tr>`).join('')||'<tr><td colspan="5">No matching client readings available.</td></tr>'}</tbody></table></div>
    <details><summary>Queues, routing and radio evidence</summary><p class="hint">Hardware offload: ${e(device?.hardware_offload??'not explicitly configured')} · Software offload: ${e(device?.software_offload??'not explicitly configured')}. Offloaded flows can bypass software queue accounting.</p><pre>${e(device?.queues||'Queue statistics unavailable')}</pre><pre>${e(device?.routes||'Routes unavailable')}</pre><pre>${e(device?.rules||'Policy rules unavailable')}</pre><pre>${e(device?.wireless||'Radio details unavailable')}</pre>${Object.entries(device?.surveys||{}).map(([key,value])=>`<pre>${e(key+'\n'+value)}</pre>`).join('')}</details>
    <details><summary>Recent router events and collection gaps</summary><pre>${e(rows.filter(row=>row.kind==='log'||row.kind==='gap').slice(-30).map(row=>new Date(row.at*1000).toLocaleTimeString()+' '+(row.line||row.detail)).join('\n')||'No events in this window')}</pre></details>
    <h3>Incident markers</h3><p class="hint">Markers preserve 2 minutes before and 5 minutes after the event. Incident limits: ${settings.incident_max_count?`${settings.incident_max_count} incidents`:'no count cap'} / ${settings.incident_max_mib?`${settings.incident_max_mib} MiB of payload`:'no size cap'}. Raw history retains up to ${e(networkRetention(settings))}.</p>
    <div class="network-table"><table><thead><tr><th>Time</th><th>Observation</th><th>Evidence</th></tr></thead><tbody>${d.events.map(event=>`<tr><td>${e(new Date(event.measured*1000).toLocaleString())}</td><td>${e(event.label)}${event.automatic?' · automatic':''}</td><td><a href="/api/servers/${encodeURIComponent(s.id)}/network/export/${encodeURIComponent(event.id)}">${event.complete?'Export incident':'Export available samples'}</a></td></tr>`).join('')||'<tr><td colspan="3">No incidents recorded.</td></tr>'}</tbody></table></div>
    ${networkArchives(s,d)}
    ${v.error?`<p class="form-error">${e(v.error)}</p>`:''}
  </section>`;
}
async function loadNetwork(s,force=false){
  const v=networkView(s);if(v.busy||(!force&&Date.now()-v.loaded<2000))return;
  v.busy=true;
  try{const [data,readings]=await Promise.all([api(`/servers/${s.id}/network`),api(`/servers/${s.id}/network/samples`)]);v.data=data;v.readings=readings;v.loaded=Date.now();v.error=null;}
  catch(error){v.error=error.message;v.loaded=Date.now();}
  finally{v.busy=false;}
  if(current()?.id===s.id&&$('.network-view')?.dataset.key===`network:${s.id}`)updateHTML($('#server-view'),renderNetwork(s));
}
async function networkSettings(s){
  await loadNetwork(s,true);const d=networkView(s).data;if(!d)return;
  const c=d.settings,device=d.device||{},interfaces=Object.keys(device.interfaces||{});
  const field=(name,label,fallback,min,max)=>`<label>${label}<input name="${name}" type="number" min="${min}" max="${max}" step="1" value="${c[name]??fallback}" required></label>`;
  modal('OpenWRT recording settings',`<form id="network-settings-form" data-id="${e(s.id)}"><p>Records on the on-site network-recorder service. Saving these settings changes Harbour only; the router is inspected read-only.</p><label class="check-line"><input name="enabled" type="checkbox" ${c.enabled?'checked':''}>Enable continuous recording</label><div class="form-grid"><label>Recorder probe interval<select name="interval">${[.25,.5,1,2].map(v=>`<option value="${v}" ${v===c.interval?'selected':''}>${v*1000} ms</option>`).join('')}</select></label><label>Router WAN device<select name="wan_device"><option value="">Router routing policy</option>${[...new Set([...interfaces,...(c.wan_device?[c.wan_device]:[])])].map(name=>`<option value="${e(name)}" ${name===c.wan_device?'selected':''}>${e(name)}</option>`).join('')}</select></label><label class="full">IP targets (one per line, up to four)<textarea name="targets" rows="4" required>${e(c.targets.join('\n'))}</textarea></label><label>Wi-Fi client MAC (optional)<input name="client_mac" value="${e(c.client_mac)}" placeholder="aa:bb:cc:dd:ee:ff"></label><label>Incident RTT threshold (ms)<input name="latency_limit_ms" type="number" min="10" max="10000" value="${c.latency_limit_ms}" required></label></div><label class="check-line"><input name="router_probes" type="checkbox" ${c.router_probes?'checked':''}>Also probe from the router (approximately 1 second)</label><p class="hint">Use the router’s LAN IP for its SSH address to include the LAN reference probe. Router/WAN interfaces stay unchanged. The selected WAN device applies to router-originated probes; recorder traffic follows the on-site server’s routes. A Wi-Fi client selection filters the view; raw evidence retains all reported clients.</p>
  <h3>Raw history</h3><div class="form-grid">${field('retention_hours','Keep readings (hours)',168,1,8760)}${field('max_rows','Maximum readings (0 = no cap)',0,0,100000000)}${field('max_payload_mib','Maximum payload (MiB, 0 = no cap)',1024,0,1048576)}</div><p class="hint">The first limit reached applies. Database indexes and allocated space add overhead. Increasing retention cannot recover previously deleted readings.</p>
  <h3>Daily archives</h3><label class="check-line"><input name="archive_enabled" type="checkbox" ${c.archive_enabled!==false?'checked':''}>Automatically save compressed daily files</label><div class="form-grid">${field('archive_retention_days','Keep daily files (days)',90,1,3650)}${field('archive_max_mib','Maximum compressed files (MiB, 0 = no cap)',4096,0,1048576)}</div><p class="hint">Files are built throughout the day and finish after midnight UTC. Raw readings are archived before cleanup, even with a shorter raw-history limit. If archiving fails, unarchived readings remain in the database and can exceed its limits until the problem is resolved. Turning archiving off removes this protection. Daily file limits apply to finished files; today's growing file is additional.</p>
  <h3>Incident exports</h3><div class="form-grid">${field('incident_max_count','Maximum incidents (0 = no cap)',200,0,10000)}${field('incident_max_mib','Maximum incident payload (MiB, 0 = no cap)',128,0,1048576)}</div>
  <div class="form-error" role="alert"></div><div class="form-actions"><button class="primary" type="submit">Save recording settings</button></div></form>`);
}
document.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action]');if(!el||current()?.server_type!=='openwrt')return;
  try{
    if(el.dataset.action==='network-settings')await networkSettings(current());
    if(['network-degrade','network-recover'].includes(el.dataset.action)){await api(`/servers/${current().id}/network/markers`,'POST',{label:el.dataset.label});await loadNetwork(current(),true);}
  }catch(error){toast(error.message,true);}
});
document.addEventListener('submit',async event=>{
  const form=event.target;if(form.id!=='network-settings-form')return;event.preventDefault();
  const data=Object.fromEntries(new FormData(form));
  const limits=Object.fromEntries(['retention_hours','max_rows','max_payload_mib','archive_retention_days','archive_max_mib','incident_max_count','incident_max_mib'].map(key=>[key,Number(data[key])]));
  try{await api(`/servers/${form.dataset.id}/network`,'PUT',{enabled:data.enabled==='on',interval:Number(data.interval),targets:data.targets.split(/\s+/).filter(Boolean),router_probes:data.router_probes==='on',wan_device:data.wan_device,client_mac:data.client_mac.trim(),latency_limit_ms:Number(data.latency_limit_ms),archive_enabled:data.archive_enabled==='on',...limits});closeOverlay(true);await loadNetwork(current(),true);}
  catch(error){formError(error.message);}
});
