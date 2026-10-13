'use strict';

function renderCallTests(s,d){
  const data=d.call_tests;if(!data)return '';
  const tests=data.tests||[],running=tests.find(t=>t.status==='running');
  const metric=(t,direction,key,digits=2)=>networkNumber(t.result?.directions?.[direction]?.summary?.[key],digits);
  return `<section class="call-tests"><div class="between"><h3>Remote call flow tests</h3>${admin()?button('call-test-configure','Remote call flow test','activity','small',`data-server="${e(s.id)}" ${running?'disabled':''}`):''}</div>
    <div class="endpoint-controls">${admin()?button('endpoint-setup','Deploy remote endpoint','','small'): ''}${button('endpoint-logs','Remote logs','download','small')}${admin()?button('call-schedule','Schedule','','small'):''}</div>${data.schedule?`<p class="hint"><b>Recurring:</b> ${e(data.schedule.config.profile_snapshot?.name||data.profiles.find(p=>p.id===data.schedule.config.profile)?.name||data.schedule.config.profile)} · every ${data.schedule.interval_seconds/60} minutes · ${data.schedule.config.duration/60} minute load · ${data.schedule.enabled?'next '+e(new Date(data.schedule.next_run*1000).toLocaleString()):'paused'} ${admin()?button('call-schedule-remove','Remove schedule','','small'):''}<br>${e(data.schedule.last_outcome||'Awaiting first run')}</p>`:''}<details class="network-notes"><summary>What this test measures</summary><p class="hint">Controlled UDP loads for FaceTime, Teams and Zoom. Runs from the on-site Harbour host to your test endpoint. A wired host tests its LAN/WAN path, not the caller’s Wi-Fi. These are traffic approximations, not real calls or video-quality scores.</p>
    </details>${!data.available?'<p class="hint">iperf3 is not installed on the Harbour host. Rebuild the container or install it for a native deployment.</p>':''}
    ${running?`<p role="status"><b>${e(running.config.profile_snapshot.name)}</b> · ${e(running.phase)} · ${Math.max(0,Math.floor(Date.now()/1000-running.created))} s elapsed ${admin()?button('call-test-stop','Stop test','','small',`data-server="${e(s.id)}" data-test="${e(running.id)}"`):''}</p>`:''}
    <p class="hint">Latest 20 tests · receiver loss and jitter · fixed-rate UDP approximation</p>
    <div class="network-table"><table><thead><tr><th>Test</th><th>Status</th><th>Upload loss / jitter</th><th>Download loss / jitter</th><th>Evidence</th></tr></thead><tbody>${tests.map(t=>`<tr><td>${e(t.config.profile_snapshot.name)}<br><small>${t.config.source==='schedule'?'Scheduled · ':''}${e(new Date(t.created*1000).toLocaleString())} · ${t.config.upload_kbps/1000} ↑ / ${t.config.download_kbps/1000} ↓ Mbps</small></td><td>${e(t.status==='running'?t.phase:t.status)}${t.error?`<br><small>${e(t.error)}</small>`:''}</td><td>${metric(t,'upload','lost_percent')}% / ${metric(t,'upload','jitter_ms')} ms</td><td>${metric(t,'download','lost_percent')}% / ${metric(t,'download','jitter_ms')} ms</td><td>${button('call-analysis','Analysis','','small',`data-server="${e(s.id)}" data-test="${e(t.id)}"`)} <a href="/api/servers/${encodeURIComponent(s.id)}/network/call-tests/${encodeURIComponent(t.id)}/export">Download evidence (.gz)</a>${t.event_id?` ${button('incident-open','Router evidence','','small',`data-server="${e(s.id)}" data-event="${e(t.event_id)}"`)}`:''}</td></tr>`).join('')||'<tr><td colspan="5">No traffic tests yet. Start one after preparing your remote endpoint.</td></tr>'}</tbody></table></div></section>`;
}

async function callTestForm(s,mode='manual'){
  await loadNetwork(s,true);
  const data=networkView(s).data?.call_tests;if(!data)return;
  const endpoints=(await api('/call-endpoints')).endpoints.filter(x=>x.deployed);
  if(!endpoints.length){await endpointSetup(s);return;}
  const previous=mode==='schedule'?data.schedule?.config:data.tests?.[0]?.config;
  const profiles=data.profiles,last={...previous,...(endpoints.find(x=>x.id===previous?.endpoint_id)||endpoints[0])},selected=profiles.find(p=>p.id===last.profile)||profiles[0];
  const options=(values,value)=>values.map(v=>`<option value="${v}" ${v===value?'selected':''}>${v} seconds</option>`).join('');
  modal('Remote call flow test',`<form id="call-test-form" data-server="${e(s.id)}">
    <p>Generate a controlled load from this Harbour installation to a remote endpoint you operate. Probes and router monitoring continue throughout the test.</p>
    ${endpoints.length?`<label>Remote endpoint<select name="saved_endpoint">${endpoints.map(x=>`<option value="${e(x.id)}" ${x.endpoint===last.endpoint?'selected':''}>${e(state.data.servers.find(s=>s.id===x.id)?.name||x.endpoint)} · ${e(x.endpoint)}</option>`).join('')}</select></label>`:''}
    <label>Call profile<select name="profile">${profiles.map(p=>`<option value="${e(p.id)}" ${p.id===selected.id?'selected':''}>${e(p.name)}</option>`).join('')}</select></label>
    <p class="hint" id="call-profile-note"></p>
    <div class="form-grid"><label>Remote endpoint IP<input name="endpoint" readonly value="${e(last.endpoint||'')}" placeholder="IPv4 or IPv6 address" required></label>
    <label>First endpoint port<input name="port" readonly type="number" min="1024" max="65534" value="${last.port||5201}" required></label>
    <label>Upload load (kbps)<input name="upload_kbps" type="number" min="64" max="8000" value="${last.upload_kbps??selected.upload_kbps}" required></label>
    <label>Download load (kbps)<input name="download_kbps" type="number" min="64" max="8000" value="${last.download_kbps??selected.download_kbps}" required></label>
    <label>Load duration (seconds, up to 60 minutes)<input name="duration" type="number" min="10" max="3600" value="${last.duration||180}" list="call-durations" required><datalist id="call-durations"><option value="300">5 minutes</option><option value="900">15 minutes</option><option value="1800">30 minutes</option><option value="3600">60 minutes</option></datalist></label>
    <label>Quiet time before and after<select name="quiet">${options([0,30,60,120],last.baseline??60)}</select></label></div>
    <details><summary>Packet size</summary><label>UDP payload bytes<input name="packet_bytes" type="number" min="128" max="1400" value="${last.packet_bytes||1200}" required></label><p class="hint">1200 bytes is Harbour’s common test setting, not a published packet size for these apps. Traffic is paced at a fixed rate; video frame bursts, separate audio, codec adaptation and QoS markings are not reproduced.</p></details>
    <div class="form-grid"><label>Run mode<select name="run_mode"><option value="manual" ${mode==='manual'?'selected':''}>Run once now</option><option value="schedule" ${mode==='schedule'?'selected':''}>Repeat on schedule</option></select></label><label id="call-repeat-interval" ${mode==='schedule'?'':'hidden'}>Start every (minutes)<input name="interval_minutes" type="number" min="1" max="10080" value="${data.schedule?.interval_seconds/60||120}"></label></div>
    <p id="call-test-estimate" class="hint"></p>
    <p class="hint">The endpoint needs two iperf3 servers on consecutive ports (default 5201 and 5202), with TCP and UDP available. They support simultaneous, independent upload and download rates. Add this endpoint to the IP probe targets to measure its RTT during the load.</p>
    <p class="hint">Tests stop automatically. Closing this dialog or browser leaves the test running; Stop test, pausing monitoring, changing the router connection or restarting Harbour ends it. One traffic test can run at a time.</p>
    <div class="form-error" role="alert"></div><div class="form-actions"><button type="submit" class="primary" ${!data.available?'disabled':''}>${mode==='schedule'?'Save schedule':'Start traffic test'}</button></div></form>`);
  $('#call-test-form').endpointChoices=endpoints;
  callProfileNote(profiles);
}

function callProfileNote(profiles,reset=false){
  const form=$('#call-test-form');if(!form)return;
  const profile=profiles.find(p=>p.id===form.elements.profile.value);if(!profile)return;
  if(reset)for(const key of ['upload_kbps','download_kbps'])form.elements[key].value=profile[key];
  $('#call-profile-note').innerHTML=`${e(profile.basis)} · ${e(profile.note)} <a href="${e(profile.source)}" target="_blank" rel="noopener noreferrer">Source</a>`;
  callTestEstimate();
}
function callTestEstimate(){
  const form=$('#call-test-form');if(!form)return;
  const up=Number(form.elements.upload_kbps.value),down=Number(form.elements.download_kbps.value),duration=Number(form.elements.duration.value),quiet=Number(form.elements.quiet.value);
  const scheduled=form.elements.run_mode.value==='schedule';
  $('#call-repeat-interval').hidden=!scheduled;
  form.querySelector('[type=submit]').textContent=scheduled?'Save schedule':'Start traffic test';
  $('#call-test-estimate').textContent=(scheduled?'First run in '+form.elements.interval_minutes.value+' minutes. Overlapping runs are skipped. ':'')+`${duration+quiet*2} seconds total · approximately ${((up+down)*duration/8000).toFixed(1)} MB of UDP payload, plus network overhead. Your chosen rates are recorded with the profile.`;
}
document.addEventListener('change',event=>{
  if(event.target.matches('#call-test-form [name=saved_endpoint]')){const f=event.target.form,c=f.endpointChoices.find(x=>x.id===event.target.value);if(c){f.elements.endpoint.value=c.endpoint;f.elements.port.value=c.port;}}
  if(event.target.matches('#call-test-form [name=profile]'))callProfileNote(networkView(current()).data.call_tests.profiles,true);
});
document.addEventListener('input',event=>{if(event.target.closest('#call-test-form'))callTestEstimate();});
document.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action]');if(!el)return;
  try{
    if(el.dataset.action==='endpoint-setup')await endpointSetup(current());
    if(el.dataset.action==='call-analysis')await callAnalysis(el.dataset.server,el.dataset.test);
    if(el.dataset.action==='endpoint-logs')await endpointLogs();
    if(el.dataset.action==='call-test-configure')await callTestForm(current());
    if(el.dataset.action==='call-schedule')await callTestForm(current(),'schedule');
    if(el.dataset.action==='call-schedule-remove'){await api(`/servers/${current().id}/network/call-tests/schedule`,'DELETE');await loadNetwork(current(),true);}
    if(el.dataset.action==='call-test-stop'){
      await api(`/servers/${el.dataset.server}/network/call-tests/${el.dataset.test}/stop`,'POST',{});
      toast('Stopping traffic test…');if(current()?.id===el.dataset.server)await loadNetwork(current(),true);
    }
  }catch(error){toast(error.message,true);}
});
document.addEventListener('submit',async event=>{
  const form=event.target;if(form.id!=='call-test-form')return;event.preventDefault();
  const values=Object.fromEntries(new FormData(form)),body={endpoint:values.endpoint.trim(),endpoint_id:values.saved_endpoint,profile:values.profile};
  for(const key of ['port','upload_kbps','download_kbps','duration','packet_bytes'])body[key]=Number(values[key]);
  body.baseline=body.recovery=Number(values.quiet);
  const submit=form.querySelector('[type=submit]');submit.disabled=true;
  try{
    const scheduled=values.run_mode==='schedule';
    await api(`/servers/${form.dataset.server}/network/call-tests`+(scheduled?'/schedule':''),scheduled?'PUT':'POST',scheduled?{enabled:true,interval_minutes:Number(values.interval_minutes),test:body}:body);
    closeOverlay(true);toast(scheduled?'Recurring test saved':'Traffic test started');if(current()?.id===form.dataset.server)await loadNetwork(current(),true);
  }catch(error){if(form.isConnected){formError(error.message);submit.disabled=false;}}
});

async function endpointSetup(server=current()){
  const endpoints=(await api('/call-endpoints')).endpoints;
  const randomPort=()=>20000+crypto.getRandomValues(new Uint32Array(1))[0]%40000;
  modal('Remote call flow test',`<form id="endpoint-form" autocomplete="off" data-server="${e(server?.id||'')}">
    <p>Deploy your remote endpoint, then choose a call profile.</p>
    <div class="form-grid"><label>Remote IP<input name="endpoint" placeholder="IPv4 or IPv6 address" required></label>
    <label>SSH username<input name="username" value="root" autocomplete="off" required></label>
    <label>SSH password<input name="password" type="password" autocomplete="new-password" maxlength="1024" required></label>
    <label>Harbour callback address<input name="callback_url" type="url" value="${e(location.origin)}" required></label></div>
    <p class="hint">Use a Harbour address the remote container can reach. The SSH password is used once and discarded. The account must already have Docker access; deployment does not use sudo.</p>
    <details><summary>Ports, retention &amp; host identity</summary><div class="form-grid">
      <label>SSH port<input name="ssh_port" type="number" min="1" max="65535" value="22" required></label>
      <label>First traffic port<input name="port" type="number" min="1024" max="65534" value="5201" required></label>
      <label>Log HTTP port<input name="http_port" type="number" min="1024" max="65535" value="${randomPort()}" required></label>
      <label>Keep logs (days)<input name="retention_days" type="number" min="1" max="365" value="30" required></label>
      <label>Verified SSH fingerprint (optional)<input name="fingerprint" placeholder="SHA256:…"></label></div>
      <p class="hint">Without a supplied fingerprint, Harbour discovers and pins the host key for this connection. HTTP uses a generated token; use a trusted network or VPN. Traffic uses this port and the following port, both TCP and UDP.</p>
    </details>
    <p class="hint">Installs in ~/harbour-c-remote. Remote logs stay in logs/YY-MM-DD (UTC).</p>
    <div class="form-error" role="alert"></div><div class="form-actions"><button type="submit" class="primary">Deploy</button></div>
    </form>
    ${endpoints.filter(x=>!x.deployed).map(x=>`<div class="between"><span>${e(x.endpoint)} · awaiting confirmation</span>${button('endpoint-check','Check connection','','small',`data-endpoint="${e(x.id)}" data-server="${e(server?.id||'')}"`)}</div>`).join('')}
    <section id="endpoint-progress" hidden class="collector-setup" data-kind="call-endpoint"><div class="collector-progress" hidden><p class="collector-status" role="status"></p><pre class="collector-output" tabindex="0" aria-label="Remote endpoint setup output"></pre><div class="collector-password"></div>${button('collector-cancel','Stop setup','','ghost small')}</div></section>`);
}
document.addEventListener('submit',async event=>{
  const form=event.target;if(form.id!=='endpoint-form')return;event.preventDefault();
  const submit=form.querySelector('[type=submit]');if(submit.disabled)return;submit.disabled=true;
  const body={};for(const key of ['endpoint','username','callback_url','fingerprint'])body[key]=form.elements[key].value.trim();
  for(const key of ['ssh_port','port','http_port','retention_days'])body[key]=Number(form.elements[key].value);
  body.password=form.elements.password.value;form.elements.password.value='';markFormSaved(form);
  form.querySelector('.form-error').textContent='';
  form.querySelectorAll('input').forEach(el=>el.disabled=true);
  const root=$('#endpoint-progress');
  try{
    const result=await pushCollector(root,'install',{url:'/api/call-endpoints/deploy',body});
    if(result?.ok&&form.isConnected){
      const server=state.data.servers.find(x=>x.id===form.dataset.server);
      if(server)await callTestForm(server);
    }
  }catch(error){if(form.isConnected)form.querySelector('.form-error').textContent=error.message;}
  finally{delete body.password;if(form.isConnected){submit.disabled=false;form.querySelectorAll('input').forEach(el=>el.disabled=false);}}
});
document.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action=endpoint-check]');if(!el)return;
  el.disabled=true;
  try{
    await api('/call-endpoints/'+encodeURIComponent(el.dataset.endpoint)+'/check','POST',{});
    const server=state.data.servers.find(x=>x.id===el.dataset.server);
    if(server&&el.isConnected)await callTestForm(server);
  }catch(error){toast(error.message,true);}finally{if(el.isConnected)el.disabled=false;}
});
async function endpointLogs(){
  const endpoints=(await api('/call-endpoints')).endpoints;
  modal('Remote endpoint logs',`<div id="remote-log-browser">${endpoints.length?`<div class="form-grid"><label>Endpoint<select id="remote-log-host">${endpoints.map(x=>`<option value="${e(x.id)}">${e(state.data.servers.find(s=>s.id===x.id)?.name||x.endpoint)} · ${e(x.endpoint)}:${x.http_port}</option>`).join('')}</select></label><label>Day (UTC)<select id="remote-log-day"></select></label></div>${button('remote-logs-refresh','Refresh','','small')}<p class="hint">Fetched over token-authenticated HTTP. SSH is not used.</p><p class="form-error" role="alert"></p><div class="remote-log-list" role="status"></div>`:'<p>No remote endpoints configured. Use Deploy remote endpoint to set one up.</p>'}</div>`);
  if(endpoints.length){const root=$('#remote-log-browser');root.querySelector('#remote-log-host').addEventListener('change',()=>loadEndpointLogs(root));root.querySelector('#remote-log-day').addEventListener('change',()=>loadEndpointLogs(root,true));root.querySelector('[data-action=remote-logs-refresh]').addEventListener('click',()=>loadEndpointLogs(root));await loadEndpointLogs(root);}
}
async function loadEndpointLogs(root,filesOnly=false){
  const request={};root.request=request;
  const host=root.querySelector('#remote-log-host').value,day=root.querySelector('#remote-log-day'),list=root.querySelector('.remote-log-list'),error=root.querySelector('.form-error');
  error.textContent='';list.textContent='Loading logs…';
  const active=()=>root.isConnected&&root.request===request;
  try{
    if(!filesOnly){const data=await api(`/call-endpoints/${encodeURIComponent(host)}/logs`);if(!active())return;const selected=day.value;day.innerHTML=data.days.map(x=>`<option value="${e(x)}" ${x===selected?'selected':''}>${e(x)}</option>`).join('');}
    if(!day.value){list.textContent='No remote logs yet.';return;}
    const data=await api(`/call-endpoints/${encodeURIComponent(host)}/logs?day=${encodeURIComponent(day.value)}`);if(!active())return;
    list.innerHTML=data.files.map(x=>`<a href="/api/call-endpoints/${encodeURIComponent(host)}/logs/${encodeURIComponent(day.value)}/${encodeURIComponent(x.name)}">${e(x.name)} <small>· ${capacity(x.bytes)}</small></a>`).join('')||'No logs for this day.';
  }catch(err){if(active()){error.textContent=err.message;list.textContent='';}}
}

async function callAnalysis(serverId,testId){
  const base=`/servers/${encodeURIComponent(serverId)}/network/call-tests/${encodeURIComponent(testId)}`;
  const data=await api(base+'/analysis'),a=data.analysis;
  const row=(direction)=>{const d=a?.directions?.find(x=>x.direction===direction);return `<article class="probe-card"><h4>${direction==='upload'?'Upload · remote receiver':'Download · local receiver'}</h4><p>${networkNumber(d?.summary?.lost_percent,2)}% loss · ${networkNumber(d?.summary?.jitter_ms,2)} ms jitter</p><span class="tag ${d?.remote_match?'green':''}">${d?.remote_match?'Remote session verified':'Remote session not verified'}</span></article>`;};
  modal('Call flow analysis',`<div id="call-analysis"><div class="between"><strong>${e(data.test.config.profile_snapshot?.name||'Call test')} · ${e(data.test.status)}</strong><a href="/api${base}/export">Download all evidence (.json.gz)</a></div>
    <p>${e(a?.summary||'Evidence has not been collected yet.')}</p>
    <div class="probe-cards">${row('upload')}${row('download')}</div>
    <p class="hint">${a?.clock_aligned?'Remote clock alignment verified within measured uncertainty.':'Remote clock alignment unavailable or changed.'} Root cause is not established.</p>
    ${(a?.findings||[]).map(f=>`<p><span class="tag">${e(f.kind)}</span> ${e(f.text)}</p>`).join('')}
    ${(a?.router_report?.findings||[]).map(f=>`<article class="incident-finding"><h4>${e(f.title)} · ${e(f.assessment)}</h4><p>${e(f.explanation)}</p><p>Next check: ${e(f.next_check)}</p></article>`).join('')}
    <details><summary>Recorder comparison · baseline / load / recovery</summary><div class="network-table"><table><thead><tr><th>Path</th><th>Phase</th><th>Attempts</th><th>Timeouts</th><th>p95 RTT</th><th>Uncertain</th></tr></thead><tbody>${(a?.paths||[]).map(p=>`<tr><td>${e(p.source)} → ${e(p.target)}</td><td>${e(p.phase)}</td><td>${p.attempts}</td><td>${p.timeouts}</td><td>${networkNumber(p.p95_ms,1)} ms</td><td>${p.uncertain}</td></tr>`).join('')}</tbody></table></div></details>
    <details><summary>Receiver timeline · ${a?.timeline?.length||0} intervals</summary><div class="network-table"><table><thead><tr><th>Harbour time</th><th>Direction</th><th>Loss</th><th>Jitter</th><th>Time uncertainty</th><th>Nearby probe issues</th></tr></thead><tbody>${(a?.timeline||[]).slice(0,120).map(t=>`<tr><td>${e(new Date(t.start*1000).toLocaleTimeString())}</td><td>${e(t.direction)}</td><td>${networkNumber(t.lost_percent,2)}%</td><td>${networkNumber(t.jitter_ms,2)} ms</td><td>±${networkNumber(t.uncertainty_seconds,2)} s</td><td>${t.recorder_degradation_count}</td></tr>`).join('')}</tbody></table></div><p class="hint">First 120 intervals shown; all are included in the export.</p></details>
    <details><summary>Evidence coverage &amp; method</summary>${(a?.quality||[]).map(q=>`<p>${e(q)}</p>`).join('')}<p>${e(a?.method||'')}</p></details>
    <div class="form-error" role="alert"></div>${admin()?'<button id="call-evidence-retry" class="small" '+(data.test.status==='running'?'disabled':'')+'>Collect / retry evidence</button>':''}</div>`,true);
  $('#call-evidence-retry')?.addEventListener('click',async event=>{
    const button=event.target,root=$('#call-analysis');button.disabled=true;
    try{await api(base+'/collect','POST',{});if(root.isConnected)await callAnalysis(serverId,testId);}
    catch(error){if(root.isConnected)root.querySelector('.form-error').textContent=error.message;}
    finally{if(button.isConnected)button.disabled=false;}
  });
}
