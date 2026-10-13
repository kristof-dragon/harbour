'use strict';

function renderCallTests(s,d){
  const data=d.call_tests;if(!data)return '';
  const tests=data.tests||[],running=tests.find(t=>t.status==='running');
  const metric=(t,direction,key,digits=2)=>networkNumber(t.result?.directions?.[direction]?.summary?.[key],digits);
  return `<section class="call-tests"><div class="between"><h3>Call traffic tests</h3>${admin()?button('call-test-configure','New traffic test','activity','small',`data-server="${e(s.id)}" ${running?'disabled':''}`):''}</div>
    <p class="hint">Controlled UDP loads for FaceTime, Teams and Zoom. Runs from the on-site Harbour host to your test endpoint. A wired host tests its LAN/WAN path, not the caller’s Wi-Fi. These are traffic approximations, not real calls or video-quality scores.</p>
    ${!data.available?'<p class="hint">iperf3 is not installed on the Harbour host. Rebuild the container or install it for a native deployment.</p>':''}
    ${running?`<p role="status"><b>${e(running.config.profile_snapshot.name)}</b> · ${e(running.phase)} · ${Math.max(0,Math.floor(Date.now()/1000-running.created))} s elapsed ${admin()?button('call-test-stop','Stop test','','small',`data-server="${e(s.id)}" data-test="${e(running.id)}"`):''}</p>`:''}
    <p class="hint">Upload/download receiver statistics are saved when the load finishes. Jitter is iperf3’s receiver estimate; latency remains in the ICMP charts. The latest 20 tests per router are retained.</p>
    <div class="network-table"><table><thead><tr><th>Test</th><th>Status</th><th>Upload loss / jitter</th><th>Download loss / jitter</th><th>Evidence</th></tr></thead><tbody>${tests.map(t=>`<tr><td>${e(t.config.profile_snapshot.name)}<br><small>${e(new Date(t.created*1000).toLocaleString())} · ${t.config.upload_kbps/1000} ↑ / ${t.config.download_kbps/1000} ↓ Mbps</small></td><td>${e(t.status==='running'?t.phase:t.status)}${t.error?`<br><small>${e(t.error)}</small>`:''}</td><td>${metric(t,'upload','lost_percent')}% / ${metric(t,'upload','jitter_ms')} ms</td><td>${metric(t,'download','lost_percent')}% / ${metric(t,'download','jitter_ms')} ms</td><td><a href="/api/servers/${encodeURIComponent(s.id)}/network/call-tests/${encodeURIComponent(t.id)}">Export test JSON</a>${t.event_id?` ${button('incident-open','Router evidence','','small',`data-server="${e(s.id)}" data-event="${e(t.event_id)}"`)}`:''}</td></tr>`).join('')||'<tr><td colspan="5">No traffic tests yet. Start one after preparing your remote endpoint.</td></tr>'}</tbody></table></div></section>`;
}

async function callTestForm(s){
  await loadNetwork(s,true);
  const data=networkView(s).data?.call_tests;if(!data)return;
  const profiles=data.profiles,last=data.tests?.[0]?.config||{},selected=profiles.find(p=>p.id===last.profile)||profiles[0];
  const options=(values,value)=>values.map(v=>`<option value="${v}" ${v===value?'selected':''}>${v} seconds</option>`).join('');
  modal('Call traffic test',`<form id="call-test-form" data-server="${e(s.id)}">
    <p>Generate a controlled load from this Harbour installation to a remote endpoint you operate. Probes and router monitoring continue throughout the test.</p>
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
  if(event.target.matches('#call-test-form [name=profile]'))callProfileNote(networkView(current()).data.call_tests.profiles,true);
});
document.addEventListener('input',event=>{if(event.target.closest('#call-test-form'))callTestEstimate();});
document.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action]');if(!el)return;
  try{
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
