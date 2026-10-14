'use strict';

function callTestState(s){const v=networkView(s);v.callFilter??='';return v;}
function callTestContent(s,data){
  const v=callTestState(s),tests=data?.tests||[],schedules=data?.schedules||[],running=data?.running||tests.find(t=>t.status==='running');
  const when=value=>value?new Date(value*1000).toLocaleString():'—';
  const controls=(row)=>admin()?`${button('call-schedule-edit','Edit','','small',`data-id="${e(row.id)}"`)}${button('call-schedule-start','Start now','','small',`data-id="${e(row.id)}" ${running?'disabled':''}`)}${button('call-schedule-toggle',row.enabled?'Pause':'Resume','','small',`data-id="${e(row.id)}"`)}${button('call-schedule-remove','Remove','','ghost small',`data-id="${e(row.id)}"`)}`:'';
  const liveRow=direction=>{
    const final=running?.result?.directions?.[direction]?.summary;
    const sample=running?.live?.[direction==='upload'?'upload_receiver':'download'];
    const sender=direction==='upload'?running?.live?.upload:null;
    const stale=sample?.stale??(sample?.observed_at&&Date.now()/1000-sample.observed_at>10);
    const available=Boolean(sample)&&!stale&&sample.sender===false;
    const senderFresh=sender&&!(sender.stale??(!sender.observed_at||Date.now()/1000-sender.observed_at>10));
    const values=final||(available?sample:null),rate=values?.bits_per_second??(senderFresh?sender.bits_per_second:null);
    const note=final?'Final receiver summary':stale?'Readings stale':available?'Latest receiver interval':direction==='upload'&&running&&!running.config.remote_live?'Redeploy endpoint for live upload loss/jitter':running?.phase==='loading'?'Waiting for receiver readings':'Waiting for load';
    return `<article class="probe-card" data-key="live-${direction}"><div class="compact-heading"><h4>${direction==='upload'?'Upload':'Download'}</h4><small data-key="live-note">${e(note)}${!values&&senderFresh?' · sender rate':''}</small></div><div class="call-live-values"><span>Throughput <b data-key="bitrate">${networkNumber(rate==null?null:rate/1e6,2)} Mbps</b></span><span>Loss <b data-key="loss">${networkNumber(values?.lost_percent,2)}%</b></span><span>Jitter <b data-key="jitter">${networkNumber(values?.jitter_ms,2)} ms</b></span></div></article>`;
  };
  return `<div class="between compact-section-heading" data-key="call-actions"><div class="compact-heading"><h3>Call tests</h3>${help('Call tests','Fixed-rate UDP approximations for FaceTime, Teams and Zoom, from Harbour to your endpoint. These are not real calls or video-quality scores. One test runs at a time; due schedules wait their turn without replaying missed intervals.')}</div><div class="endpoint-controls" data-key="endpoint-actions">${admin()?button('endpoint-setup','Deploy endpoint','','small')+button('install-artifacts','Saved installs','','small'):''}${button('endpoint-logs','Remote logs','download','small')}${admin()?button('call-schedule','Add recurring test','','small')+button('call-test-configure','Run test','activity','small',`data-server="${e(s.id)}" ${running?'disabled':''}`):''}</div></div>
    <p class="form-error" data-key="call-error" role="status">${e(v.callError||'')}</p>
    <section class="call-live" data-key="call-live" ${running?'':'hidden'}><div class="between"><div class="compact-heading"><h3>Running · <span data-key="live-profile">${e(running?.config.profile_snapshot?.name||'')}</span></h3><span class="hint" data-key="live-phase">${e(running?.phase||'')} · ${running?Math.floor((Date.now()/1000-running.created)/60):0}m elapsed</span>${help('Live test readings','Updates every 5 seconds. Receiver loss and jitter are provisional until the test finishes. Throughput may come from the sender while receiver readings are unavailable.')}</div>${admin()?button('call-test-stop','Stop test','','small',`data-server="${e(s.id)}" data-test="${e(running?.id||'')}"`):''}</div><div class="probe-cards">${liveRow('upload')}${liveRow('download')}</div>${running?.live?.remote_error?`<p class="form-error" data-key="live-warning">${e(running.live.remote_error)}</p>`:''}</section>
    <section data-key="schedules"><h3>Recurring tests</h3><div class="network-table"><table data-table="call-schedules" class="call-schedules"><thead><tr><th>Name / profile</th><th>Timing</th><th data-type="date">Last run</th><th>Controls</th></tr></thead><tbody>${schedules.map(row=>`<tr data-key="schedule:${e(row.id)}"><td><b>${e(row.name)}</b><br>${e(data.profiles.find(p=>p.id===row.config.profile)?.name||row.config.profile)}</td><td data-value="${row.interval_seconds}">${row.config.duration/60} minute load · every ${row.interval_seconds/60} minutes<br>${row.enabled?'Next: '+e(when(row.next_run)):'Paused'}</td><td data-value="${e(row.last_run??'')}">${e(row.last_status||'Not run yet')}<br><small>${e(row.last_outcome||'')} · ${e(when(row.last_run))}</small></td><td><div class="collector-actions">${controls(row)}${button('call-schedule-results','Results','','small',`data-id="${e(row.id)}"`)}</div></td></tr>`).join('')||'<tr data-key="no-schedules"><td colspan="4">No recurring tests.</td></tr>'}</tbody></table></div></section>
    <section data-key="call-history"><div class="between"><div class="compact-heading"><h3>Test results</h3>${help('Test result retention','Latest 20 runs retained per recurring test, plus 20 one-off tests. Choose a recurring test to narrow the history.')}</div><label>Results for<select id="call-results-filter"><option value="">All tests</option>${schedules.map(row=>`<option value="${e(row.id)}" ${v.callFilter===row.id?'selected':''}>${e(row.name)}</option>`).join('')}${v.callFilter&&!schedules.some(r=>r.id===v.callFilter)?`<option value="${e(v.callFilter)}" selected>Removed recurring test</option>`:''}</select></label></div><div class="network-table"><table data-table="call-results" class="call-results"><thead><tr><th>Test</th><th data-type="date">Started</th><th>Status</th><th>Upload loss / jitter</th><th>Download loss / jitter</th><th>Results</th></tr></thead><tbody>${tests.map(t=>`<tr data-key="test:${e(t.id)}"><td>${e(t.config.profile_snapshot?.name||t.config.profile)}<br><small>${e(t.config.schedule_name||'One-off test')}</small></td><td data-value="${t.created}">${e(when(t.created))}</td><td>${e(t.status==='running'?t.phase:t.status)}${t.error?`<br><small>${e(t.error)}</small>`:''}</td>${['upload','download'].map(d=>`<td data-key="result:${d}" data-value="${e(t.result?.directions?.[d]?.summary?.lost_percent??'')}">${networkNumber(t.result?.directions?.[d]?.summary?.lost_percent,2)}% / ${networkNumber(t.result?.directions?.[d]?.summary?.jitter_ms,2)} ms</td>`).join('')}<td>${button('call-analysis','Analysis','','small',`data-server="${e(s.id)}" data-test="${e(t.id)}" ${t.status==='running'?'disabled':''}`)}${t.status!=='running'?` <a href="/api/servers/${encodeURIComponent(s.id)}/network/call-tests/${encodeURIComponent(t.id)}/export">Download evidence (.gz)</a>`:''}</td></tr>`).join('')||'<tr data-key="no-tests"><td colspan="6">No test results yet.</td></tr>'}</tbody></table></div></section>
${data&&!data.available?'<p class="hint" data-key="iperf-missing">Install iperf3 on Harbour or rebuild its container to run tests.</p>':''}`;
}
function renderCallTests(s,d){return `<section class="call-tests" data-server="${e(s.id)}" data-key="call-tests:${e(s.id)}" data-preserve-children>${callTestContent(s,callTestState(s).callData||d?.call_tests)}</section>`;}
async function refreshCallTests(s,force=false){
  const v=callTestState(s);
  if(v.callPending){await v.callPending;if(force)return refreshCallTests(s);return;}
  const filter=v.callFilter;
  v.callPending=(async()=>{
    try{
      const data=await api(`/servers/${encodeURIComponent(s.id)}/network/call-tests/live`+(filter?'?schedule_id='+encodeURIComponent(filter):''));
      if(v.callFilter!==filter)return;
      v.callData=data;v.callLoaded=Date.now();v.callError=null;
    }catch(error){if(v.callFilter===filter){v.callError='Live updates unavailable: '+error.message;v.callLoaded=Date.now();}}
    finally{
      v.callPending=null;
      const root=$('.call-tests');
      if(current()?.id===s.id&&root?.dataset.server===s.id)updateHTML(root,callTestContent(s,v.callData));
    }
  })();
  await v.callPending;
  if(v.callFilter!==filter&&current()?.id===s.id)await refreshCallTests(s,true);
}

async function callTestForm(s,mode='manual',scheduleId=''){
  await refreshCallTests(s,true);
  const data=callTestState(s).callData;if(!data)return;
  const saved=data.schedules.find(row=>row.id===scheduleId);
  const endpoints=(await api('/call-endpoints')).endpoints.filter(x=>x.deployed);
  if(!endpoints.length){await endpointSetup(s);return;}
  const previous=saved?.config||(mode==='manual'?data.tests?.[0]?.config:null);
  const profiles=data.profiles,last={...previous,...(endpoints.find(x=>x.id===previous?.endpoint_id)||endpoints[0])},selected=profiles.find(p=>p.id===last.profile)||profiles[0];
  const localDate=value=>{const d=new Date(value*1000);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16);};
  const first=saved?.next_run?localDate(saved.next_run):'';
  modal(saved?'Edit recurring test':'Remote call flow test',`<form id="call-test-form" data-server="${e(s.id)}" data-schedule="${e(scheduleId)}" data-first-run="${e(first)}">
    <div class="form-grid"><label>Remote endpoint<select name="saved_endpoint">${endpoints.map(x=>`<option value="${e(x.id)}" ${x.endpoint===last.endpoint?'selected':''}>${e(x.endpoint)}</option>`).join('')}</select></label>
    <label>Call profile<select name="profile">${profiles.map(p=>`<option value="${e(p.id)}" ${p.id===selected.id?'selected':''}>${e(p.name)}</option>`).join('')}</select></label></div>
    <details><summary>Profile basis</summary><p class="hint" id="call-profile-note"></p></details>
    <div class="form-grid"><label>Remote endpoint IP<input name="endpoint" readonly value="${e(last.endpoint||'')}" required></label><label>First endpoint port<input name="port" readonly type="number" value="${last.port||5201}" required></label>
    <label>Upload load (kbps)<input name="upload_kbps" type="number" min="64" max="8000" value="${last.upload_kbps??selected.upload_kbps}" required></label><label>Download load (kbps)<input name="download_kbps" type="number" min="64" max="8000" value="${last.download_kbps??selected.download_kbps}" required></label>
    <label>Load duration (seconds, up to 60 minutes)<input name="duration" type="number" min="10" max="3600" value="${last.duration||180}" list="call-durations" required><datalist id="call-durations"><option value="300">5 minutes</option><option value="1800">30 minutes</option><option value="3600">60 minutes</option></datalist></label>
    ${[['quiet','Before',last.baseline??60],['recovery','After',last.recovery??60]].map(([name,label,value])=>`<label>Quiet time ${label.toLowerCase()}<select name="${name}">${[...new Set([0,30,60,120,value])].sort((a,b)=>a-b).map(n=>`<option value="${n}" ${n===value?'selected':''}>${n} seconds</option>`).join('')}</select></label>`).join('')}</div>
    <details><summary>Packet size</summary><label>UDP payload bytes<input name="packet_bytes" type="number" min="128" max="1400" value="${last.packet_bytes||1200}" required></label></details>
    <label ${saved?'hidden':''}>Run mode<select name="run_mode"><option value="manual" ${mode==='manual'?'selected':''}>Run once now</option><option value="schedule" ${mode==='schedule'?'selected':''}>Repeat on schedule</option></select></label>
    <div id="call-repeat-interval" ${mode==='schedule'?'':'hidden'}><div class="form-grid"><label>Recurring test name<input name="schedule_name" maxlength="80" value="${e(saved?.name||'')}"></label><label>Start every (minutes)<input name="interval_minutes" type="number" min="1" max="10080" value="${saved?.interval_seconds/60||120}"></label><label>${saved?'Next run':'First run'} (local time, optional)<input name="first_run" type="datetime-local" value="${e(first)}"></label><label class="check-line"><input name="enabled" type="checkbox" ${saved?.enabled===false?'':'checked'}>Enabled</label></div></div>
    <p id="call-test-estimate" class="hint"></p><p class="hint">One test runs at a time. Closing this dialog leaves an active test running.</p><div class="form-error" role="alert"></div><div class="form-actions"><button type="submit" class="primary" ${!data.available?'disabled':''}>${mode==='schedule'?'Save schedule':'Start traffic test'}</button></div></form>`);
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
  const up=Number(form.elements.upload_kbps.value),down=Number(form.elements.download_kbps.value),duration=Number(form.elements.duration.value),quiet=Number(form.elements.quiet.value)+Number(form.elements.recovery.value),scheduled=form.elements.run_mode.value==='schedule';
  $('#call-repeat-interval').hidden=!scheduled;
  form.querySelector('[type=submit]').textContent=scheduled?'Save schedule':'Start traffic test';
  const timing=form.dataset.schedule?'Existing timing is kept unless changed. ':form.elements.first_run.value?'First run at the chosen time. ':'First run in '+form.elements.interval_minutes.value+' minutes. ';
  $('#call-test-estimate').textContent=(scheduled?timing:'')+`${duration+quiet} seconds total · approximately ${((up+down)*duration/8000).toFixed(1)} MB of UDP payload, plus network overhead.`;
}
document.addEventListener('change',event=>{
  if(event.target.matches('#call-test-form [name=saved_endpoint]')){const f=event.target.form,c=f.endpointChoices.find(x=>x.id===event.target.value);if(c){f.elements.endpoint.value=c.endpoint;f.elements.port.value=c.port;}}
  if(event.target.matches('#call-test-form [name=profile]'))callProfileNote(callTestState(current()).callData.profiles,true);
  if(event.target.id==='call-results-filter'){const s=current();callTestState(s).callFilter=event.target.value;refreshCallTests(s,true);}
});
document.addEventListener('input',event=>{if(event.target.closest('#call-test-form'))callTestEstimate();});
document.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action]');if(!el)return;
  const s=current(),action=el.dataset.action;
  try{
    if(action==='endpoint-setup')await endpointSetup(s);
    if(action==='call-analysis')await callAnalysis(el.dataset.server,el.dataset.test);
    if(action==='endpoint-logs')await endpointLogs();
    if(action==='call-test-configure')await callTestForm(s);
    if(action==='call-schedule')await callTestForm(s,'schedule');
    if(action==='call-schedule-edit')await callTestForm(s,'schedule',el.dataset.id);
    if(['call-schedule-start','call-schedule-toggle','call-schedule-remove'].includes(action)){
      busyViewNodes.add(el);el.disabled=true;
      const row=callTestState(s).callData.schedules.find(r=>r.id===el.dataset.id),url=`/servers/${s.id}/network/call-tests/schedules/${encodeURIComponent(el.dataset.id)}`;
      if(action==='call-schedule-start')await api(url+'/start','POST',{});
      if(action==='call-schedule-remove')await api(url,'DELETE');
      if(action==='call-schedule-toggle')await api(url,'PUT',{name:row.name,enabled:!row.enabled,interval_minutes:row.interval_seconds/60,test:row.config});
      await refreshCallTests(s,true);
    }
    if(action==='call-schedule-results'){callTestState(s).callFilter=el.dataset.id;await refreshCallTests(s,true);$('.call-results')?.scrollIntoView({block:'nearest'});}
    if(action==='call-test-stop'){await api(`/servers/${el.dataset.server}/network/call-tests/${el.dataset.test}/stop`,'POST',{});toast('Stopping traffic test…');if(current()?.id===el.dataset.server)await refreshCallTests(s,true);}
  }catch(error){toast(error.message,true);}finally{busyViewNodes.delete(el);if(el.isConnected&&['call-schedule-start','call-schedule-toggle','call-schedule-remove'].includes(action))el.disabled=action==='call-schedule-start'&&Boolean(callTestState(s).callData?.running);}
});
document.addEventListener('submit',async event=>{
  const form=event.target;if(form.id!=='call-test-form')return;event.preventDefault();
  const submit=form.querySelector('[type=submit]');if(submit.disabled)return;
  const values=Object.fromEntries(new FormData(form)),body={endpoint:values.endpoint.trim(),endpoint_id:values.saved_endpoint,profile:values.profile};
  for(const key of ['port','upload_kbps','download_kbps','duration','packet_bytes'])body[key]=Number(values[key]);
  body.baseline=Number(values.quiet);body.recovery=Number(values.recovery);submit.disabled=true;
  try{
    const scheduled=values.run_mode==='schedule',id=form.dataset.schedule;
    const payload=scheduled?{name:values.schedule_name.trim()||callTestState(current()).callData.profiles.find(p=>p.id===body.profile).name,enabled:values.enabled==='on',interval_minutes:Number(values.interval_minutes),test:body}:body;
    if(scheduled&&values.first_run&&values.first_run!==form.dataset.firstRun)payload.first_run=new Date(values.first_run).getTime()/1000;
    await api(`/servers/${form.dataset.server}/network/call-tests`+(scheduled?'/schedules'+(id?'/'+encodeURIComponent(id):''):''),scheduled&&id?'PUT':'POST',payload);
    closeOverlay(true);toast(scheduled?'Recurring test saved':'Traffic test started');if(current()?.id===form.dataset.server)await refreshCallTests(current(),true);
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
    ${button('install-artifacts','Saved installs','','small')}
    <section id="endpoint-progress" hidden class="collector-setup" data-kind="call-endpoint">${collectorProgress('Remote endpoint setup output')}<div class="endpoint-continue" hidden>${button('endpoint-continue','Continue to call test','','primary',`data-server="${e(server?.id||'')}"`)}</div></section>`);
}
document.addEventListener('submit',async event=>{
  const form=event.target;if(form.id!=='endpoint-form')return;event.preventDefault();
  const submit=form.querySelector('[type=submit]');if(submit.disabled)return;submit.disabled=true;
  const body={};for(const key of ['endpoint','username','callback_url','fingerprint'])body[key]=form.elements[key].value.trim();
  for(const key of ['ssh_port','port','http_port','retention_days'])body[key]=Number(form.elements[key].value);
  body.password=form.elements.password.value;form.elements.password.value='';markFormSaved(form);
  form.querySelector('.form-error').textContent='';
  form.querySelectorAll('input').forEach(el=>el.disabled=true);
  form.querySelector('details').open=false;
  const root=$('#endpoint-progress');
  try{
    const result=await pushCollector(root,'install',{url:'/api/call-endpoints/deploy',body});
    if(result?.ok&&form.isConnected){
      form.dataset.deployed='true';
      root.querySelector('.endpoint-continue').hidden=false;
    }
  }catch(error){if(form.isConnected)form.querySelector('.form-error').textContent=error.message;}
  finally{delete body.password;if(form.isConnected){submit.disabled=form.dataset.deployed==='true';form.querySelectorAll('input').forEach(el=>el.disabled=submit.disabled);}}
});
document.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action=endpoint-continue]');if(!el)return;
  const server=state.data.servers.find(x=>x.id===el.dataset.server);
  try{if(server)await callTestForm(server);else closeOverlay();}catch(error){toast(error.message,true);}
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
    <details><summary>Recorder comparison · baseline / load / recovery</summary><div class="network-table"><table data-table="call-paths"><thead><tr><th>Path</th><th>Phase</th><th>Attempts</th><th>Timeouts</th><th>p95 RTT</th><th>Uncertain</th></tr></thead><tbody>${(a?.paths||[]).map(p=>`<tr><td>${e(p.source)} → ${e(p.target)}</td><td>${e(p.phase)}</td><td>${p.attempts}</td><td>${p.timeouts}</td><td>${networkNumber(p.p95_ms,1)} ms</td><td>${p.uncertain}</td></tr>`).join('')}</tbody></table></div></details>
    <details><summary>Receiver timeline · ${a?.timeline?.length||0} intervals</summary><div class="network-table"><table data-table="call-timeline"><thead><tr><th data-type="date">Harbour time</th><th>Direction</th><th>Loss</th><th>Jitter</th><th>Time uncertainty</th><th>Nearby probe issues</th></tr></thead><tbody>${(a?.timeline||[]).map(t=>`<tr><td data-value="${t.start}">${e(new Date(t.start*1000).toLocaleTimeString())}</td><td>${e(t.direction)}</td><td>${networkNumber(t.lost_percent,2)}%</td><td>${networkNumber(t.jitter_ms,2)} ms</td><td>±${networkNumber(t.uncertainty_seconds,2)} s</td><td>${t.recorder_degradation_count}</td></tr>`).join('')}</tbody></table></div><p class="hint">All retained intervals are searchable here and included in the export.</p></details>
    <details><summary>Evidence coverage &amp; method</summary>${(a?.quality||[]).map(q=>`<p>${e(q)}</p>`).join('')}<p>${e(a?.method||'')}</p></details>
    <div class="form-error" role="alert"></div>${admin()?'<button id="call-evidence-retry" class="small" '+(data.test.status==='running'?'disabled':'')+'>Collect / retry evidence</button>':''}</div>`,true);
  $('#call-evidence-retry')?.addEventListener('click',async event=>{
    const button=event.target,root=$('#call-analysis');button.disabled=true;
    try{await api(base+'/collect','POST',{});if(root.isConnected)await callAnalysis(serverId,testId);}
    catch(error){if(root.isConnected)root.querySelector('.form-error').textContent=error.message;}
    finally{if(button.isConnected)button.disabled=false;}
  });
}
