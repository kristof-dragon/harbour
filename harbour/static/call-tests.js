'use strict';

function renderCallTests(s,d){
  const data=d.call_tests;if(!data)return '';
  const tests=data.tests||[],running=tests.find(t=>t.status==='running');
  const metric=(t,direction,key,digits=2)=>networkNumber(t.result?.directions?.[direction]?.summary?.[key],digits);
  return `<section class="call-tests"><div class="between"><h3>Remote call flow tests</h3>${admin()?button('call-test-configure','Remote call flow test','activity','small',`data-server="${e(s.id)}" ${running?'disabled':''}`):''}</div>
    <div class="endpoint-controls">${admin()?button('endpoint-setup','Deploy remote endpoint','','small'): ''}${button('endpoint-logs','Remote logs','download','small')}</div><details class="network-notes"><summary>What this test measures</summary><p class="hint">Controlled UDP loads for FaceTime, Teams and Zoom. Runs from the on-site Harbour host to your test endpoint. A wired host tests its LAN/WAN path, not the caller’s Wi-Fi. These are traffic approximations, not real calls or video-quality scores.</p>
    </details>${!data.available?'<p class="hint">iperf3 is not installed on the Harbour host. Rebuild the container or install it for a native deployment.</p>':''}
    ${running?`<p role="status"><b>${e(running.config.profile_snapshot.name)}</b> · ${e(running.phase)} · ${Math.max(0,Math.floor(Date.now()/1000-running.created))} s elapsed ${admin()?button('call-test-stop','Stop test','','small',`data-server="${e(s.id)}" data-test="${e(running.id)}"`):''}</p>`:''}
    <p class="hint">Latest 20 tests · receiver loss and jitter · fixed-rate UDP approximation</p>
    <div class="network-table"><table><thead><tr><th>Test</th><th>Status</th><th>Upload loss / jitter</th><th>Download loss / jitter</th><th>Evidence</th></tr></thead><tbody>${tests.map(t=>`<tr><td>${e(t.config.profile_snapshot.name)}<br><small>${e(new Date(t.created*1000).toLocaleString())} · ${t.config.upload_kbps/1000} ↑ / ${t.config.download_kbps/1000} ↓ Mbps</small></td><td>${e(t.status==='running'?t.phase:t.status)}${t.error?`<br><small>${e(t.error)}</small>`:''}</td><td>${metric(t,'upload','lost_percent')}% / ${metric(t,'upload','jitter_ms')} ms</td><td>${metric(t,'download','lost_percent')}% / ${metric(t,'download','jitter_ms')} ms</td><td><a href="/api/servers/${encodeURIComponent(s.id)}/network/call-tests/${encodeURIComponent(t.id)}">Export test JSON</a>${t.event_id?` ${button('incident-open','Router evidence','','small',`data-server="${e(s.id)}" data-event="${e(t.event_id)}"`)}`:''}</td></tr>`).join('')||'<tr><td colspan="5">No traffic tests yet. Start one after preparing your remote endpoint.</td></tr>'}</tbody></table></div></section>`;
}

async function callTestForm(s){
  await loadNetwork(s,true);
  const data=networkView(s).data?.call_tests;if(!data)return;
  const endpoints=(await api('/call-endpoints')).endpoints;
  const profiles=data.profiles,last=data.tests?.[0]?.config||endpoints.find(x=>x.deployed)||{},selected=profiles.find(p=>p.id===last.profile)||profiles[0];
  const options=(values,value)=>values.map(v=>`<option value="${v}" ${v===value?'selected':''}>${v} seconds</option>`).join('');
  modal('Remote call flow test',`<form id="call-test-form" data-server="${e(s.id)}">
    <p>Generate a controlled load from this Harbour installation to a remote endpoint you operate. Probes and router monitoring continue throughout the test.</p>
    ${endpoints.length?`<label>Saved remote endpoint<select name="saved_endpoint"><option value="">Custom address</option>${endpoints.map(x=>`<option value="${e(x.server_id)}" ${x.endpoint===last.endpoint?'selected':''}>${e(state.data.servers.find(s=>s.id===x.server_id)?.name||x.endpoint)} · ${e(x.endpoint)}</option>`).join('')}</select></label>`:''}
    <label>Call profile<select name="profile">${profiles.map(p=>`<option value="${e(p.id)}" ${p.id===selected.id?'selected':''}>${e(p.name)}</option>`).join('')}</select></label>
    <p class="hint" id="call-profile-note"></p>
    <div class="form-grid"><label>Remote endpoint IP<input name="endpoint" value="${e(last.endpoint||'')}" placeholder="IPv4 or IPv6 address" required></label>
    <label>First endpoint port<input name="port" type="number" min="1024" max="65534" value="${last.port||5201}" required></label>
    <label>Upload load (kbps)<input name="upload_kbps" type="number" min="64" max="8000" value="${selected.upload_kbps}" required></label>
    <label>Download load (kbps)<input name="download_kbps" type="number" min="64" max="8000" value="${selected.download_kbps}" required></label>
    <label>Load duration<select name="duration">${options([10,30,60,180,300],180)}</select></label>
    <label>Quiet time before and after<select name="quiet">${options([0,30,60,120],60)}</select></label></div>
    <details><summary>Packet size</summary><label>UDP payload bytes<input name="packet_bytes" type="number" min="128" max="1400" value="1200" required></label><p class="hint">1200 bytes is Harbour’s common test setting, not a published packet size for these apps. Traffic is paced at a fixed rate; video frame bursts, separate audio, codec adaptation and QoS markings are not reproduced.</p></details>
    <p id="call-test-estimate" class="hint"></p>
    <p class="hint">The endpoint needs two iperf3 servers on consecutive ports (default 5201 and 5202), with TCP and UDP available. They support simultaneous, independent upload and download rates. Add this endpoint to the IP probe targets to measure its RTT during the load.</p>
    <p class="hint">Tests stop automatically. Closing this dialog or browser leaves the test running; Stop test, pausing monitoring, changing the router connection or restarting Harbour ends it. One traffic test can run at a time.</p>
    <div class="form-error" role="alert"></div><div class="form-actions"><button type="submit" class="primary" ${!data.available?'disabled':''}>Start traffic test</button></div></form>`);
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
  $('#call-test-estimate').textContent=`${duration+quiet*2} seconds total · approximately ${((up+down)*duration/8000).toFixed(1)} MB of UDP payload, plus network overhead. Your chosen rates are recorded with the profile.`;
}
document.addEventListener('change',event=>{
  if(event.target.matches('#call-test-form [name=saved_endpoint]')){const f=event.target.form,c=f.endpointChoices.find(x=>x.server_id===event.target.value);if(c){f.elements.endpoint.value=c.endpoint;f.elements.port.value=c.port;}}
  if(event.target.matches('#call-test-form [name=profile]'))callProfileNote(networkView(current()).data.call_tests.profiles,true);
});
document.addEventListener('input',event=>{if(event.target.closest('#call-test-form'))callTestEstimate();});
document.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action]');if(!el)return;
  try{
    if(el.dataset.action==='endpoint-setup')await endpointSetup();
    if(el.dataset.action==='endpoint-logs')await endpointLogs();
    if(el.dataset.action==='call-test-configure')await callTestForm(current());
    if(el.dataset.action==='call-test-stop'){
      await api(`/servers/${el.dataset.server}/network/call-tests/${el.dataset.test}/stop`,'POST',{});
      toast('Stopping traffic test…');if(current()?.id===el.dataset.server)await loadNetwork(current(),true);
    }
  }catch(error){toast(error.message,true);}
});
document.addEventListener('submit',async event=>{
  const form=event.target;if(form.id!=='call-test-form')return;event.preventDefault();
  const values=Object.fromEntries(new FormData(form)),body={endpoint:values.endpoint.trim(),profile:values.profile};
  for(const key of ['port','upload_kbps','download_kbps','duration','packet_bytes'])body[key]=Number(values[key]);
  body.baseline=body.recovery=Number(values.quiet);
  const submit=form.querySelector('[type=submit]');submit.disabled=true;
  try{
    await api(`/servers/${form.dataset.server}/network/call-tests`,'POST',body);
    closeOverlay(true);toast('Traffic test started');if(current()?.id===form.dataset.server)await loadNetwork(current(),true);
  }catch(error){if(form.isConnected){formError(error.message);submit.disabled=false;}}
});

async function endpointSetup(){
  const hosts=state.data.servers.filter(s=>s.server_type!=='openwrt'),saved=(await api('/call-endpoints')).endpoints;
  if(!hosts.length){modal('Deploy remote endpoint','<p>Add the remote Linux Docker host through “Add server” first, including its verified SSH connection. Then return to Call tests to deploy the endpoint.</p>');return;}
  const randomPort=()=>20000+crypto.getRandomValues(new Uint32Array(1))[0]%40000;
  modal('Deploy remote endpoint',`<form id="endpoint-form"><label>Remote Linux Docker host<select name="host">${hosts.map(s=>`<option value="${e(s.id)}">${e(s.name)} · ${e(s.host)}</option>`).join('')}</select></label>
    <p class="hint">Installs into the SSH user’s <code>~/harbour-c-remote</code>. Docker Engine and Compose must already be installed.</p>
    <div class="form-grid"><label>Reachable endpoint IP<input name="endpoint" required placeholder="IP reachable from Harbour"></label><label>First traffic port<input name="port" type="number" min="1024" max="65534" required></label><label>Log HTTP port<input name="http_port" type="number" min="1024" max="65535" required></label><label>Keep logs (days)<input name="retention_days" type="number" min="1" max="365" required></label></div>
    <label>Log access token (optional)<input name="token" type="password" autocomplete="new-password" minlength="32" maxlength="128" pattern="[A-Za-z0-9_-]{32,128}" placeholder="Generated securely; blank keeps the saved token"></label>
    <p class="hint">Logs stay in <code>logs/YY-MM-DD</code>, using UTC dates, up to 1 GiB. A random HTTP port is preselected. HTTP tokens need a trusted LAN or VPN; restrict this port and both traffic ports to the Harbour host. Deployment leaves firewall rules unchanged.</p>
    <div class="form-error" role="alert"></div><div class="form-actions"><button class="primary" type="submit" ${state.demo?'disabled':''}>Save & deploy endpoint</button></div></form>
    <section id="endpoint-progress" class="collector-setup" data-kind="call-endpoint"><div class="collector-progress" hidden><p class="collector-status" role="status"></p><pre class="collector-output" tabindex="0" aria-label="Remote endpoint setup output"></pre><div class="collector-password"></div>${button('collector-cancel','Stop setup','','ghost small')}</div></section>`);
  const form=$('#endpoint-form');
  const fill=()=>{const host=hosts.find(s=>s.id===form.elements.host.value),c=saved.find(s=>s.server_id===host.id)||{};for(const [k,v] of Object.entries({endpoint:c.endpoint||host.host,port:c.port||5201,http_port:c.http_port||randomPort(),retention_days:c.retention_days||30}))form.elements[k].value=v;form.elements.token.value='';};
  form.elements.host.addEventListener('change',fill);fill();
}
document.addEventListener('submit',async event=>{
  const form=event.target;if(form.id!=='endpoint-form')return;event.preventDefault();
  const submit=form.querySelector('[type=submit]');if(submit.disabled)return;submit.disabled=true;
  const values=Object.fromEntries(new FormData(form)),body={endpoint:values.endpoint.trim()};
  for(const k of ['port','http_port','retention_days'])body[k]=Number(values[k]);
  if(values.token)body.token=values.token;
  form.querySelector('.form-error').textContent='';
  try{
    await api(`/servers/${encodeURIComponent(values.host)}/call-endpoint`,'PUT',body);
    form.elements.token.value='';delete body.token;values.token='';markFormSaved(form);
    form.querySelectorAll('input,select').forEach(el=>el.disabled=true);
    const root=$('#endpoint-progress');root.dataset.server=values.host;
    await pushCollector(root,'install');
  }catch(error){if(form.isConnected)form.querySelector('.form-error').textContent=error.message;}
  finally{if(form.isConnected){submit.disabled=false;form.querySelectorAll('input,select').forEach(el=>el.disabled=false);}}
});
async function endpointLogs(){
  const endpoints=(await api('/call-endpoints')).endpoints;
  modal('Remote endpoint logs',`<div id="remote-log-browser">${endpoints.length?`<div class="form-grid"><label>Endpoint<select id="remote-log-host">${endpoints.map(x=>`<option value="${e(x.server_id)}">${e(state.data.servers.find(s=>s.id===x.server_id)?.name||x.endpoint)} · ${e(x.endpoint)}:${x.http_port}</option>`).join('')}</select></label><label>Day (UTC)<select id="remote-log-day"></select></label></div>${button('remote-logs-refresh','Refresh','','small')}<p class="hint">Fetched over token-authenticated HTTP. SSH is not used.</p><p class="form-error" role="alert"></p><div class="remote-log-list" role="status"></div>`:'<p>No remote endpoints configured. Use Deploy remote endpoint to set one up.</p>'}</div>`);
  if(endpoints.length){const root=$('#remote-log-browser');root.querySelector('#remote-log-host').addEventListener('change',()=>loadEndpointLogs(root));root.querySelector('#remote-log-day').addEventListener('change',()=>loadEndpointLogs(root,true));root.querySelector('[data-action=remote-logs-refresh]').addEventListener('click',()=>loadEndpointLogs(root));await loadEndpointLogs(root);}
}
async function loadEndpointLogs(root,filesOnly=false){
  const request={};root.request=request;
  const host=root.querySelector('#remote-log-host').value,day=root.querySelector('#remote-log-day'),list=root.querySelector('.remote-log-list'),error=root.querySelector('.form-error');
  error.textContent='';list.textContent='Loading logs…';
  const active=()=>root.isConnected&&root.request===request;
  try{
    if(!filesOnly){const data=await api(`/servers/${encodeURIComponent(host)}/call-endpoint/logs`);if(!active())return;const selected=day.value;day.innerHTML=data.days.map(x=>`<option value="${e(x)}" ${x===selected?'selected':''}>${e(x)}</option>`).join('');}
    if(!day.value){list.textContent='No remote logs yet.';return;}
    const data=await api(`/servers/${encodeURIComponent(host)}/call-endpoint/logs?day=${encodeURIComponent(day.value)}`);if(!active())return;
    list.innerHTML=data.files.map(x=>`<a href="/api/servers/${encodeURIComponent(host)}/call-endpoint/logs/${encodeURIComponent(day.value)}/${encodeURIComponent(x.name)}">${e(x.name)} <small>· ${capacity(x.bytes)}</small></a>`).join('')||'No logs for this day.';
  }catch(err){if(active()){error.textContent=err.message;list.textContent='';}}
}
