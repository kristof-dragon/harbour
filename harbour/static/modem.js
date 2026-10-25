'use strict';
const modemViews=new Map();
function modemView(s){if(!modemViews.has(s.id))modemViews.set(s.id,{data:null,history:null,loaded:0,busy:false,testing:false,error:null,section:null});return modemViews.get(s.id);}
const modemTime=t=>Number.isFinite(t)?new Date(t*1000).toLocaleString():'—';
const modemState=s=>({ok:'Read complete',partial:'Partial reading',unavailable:'Collection gap'})[s]||'No readings';
const modemSectionName=key=>({system:'System',device:'Device',downstream:'Downstream',upstream:'Upstream',events:'Event log'})[key]||key;
function modemSections(s,v,sample){
  const entries=Object.entries(sample?.sections||{}),selected=entries.find(([key])=>key===v.section),panelId=`modem-values-${s.id}`;
  const overall=`${modemState(sample?.state)}${v.data?.stale?' · stale':''}`,complete=sample?.state==='ok'&&!v.data?.stale;
  const chips=entries.map(([key,part])=>{
    const name=modemSectionName(key),count=(part.fields||[]).length,status=modemState(part.state),shortStatus=({ok:'Read',partial:'Partial',unavailable:'Unavailable'})[part.state]||'No readings',tone=part.state==='ok'?'complete':'incomplete';
    return `<button type="button" class="modem-section-chip ${tone}" id="${e(panelId+'-'+key)}" data-key="modem-chip:${e(key)}" data-action="modem-section" data-section="${e(key)}" aria-expanded="${v.section===key}" aria-controls="${e(panelId)}" aria-label="${e(name+' · '+status+' · '+count+' values')}"><strong>${e(name)}</strong><span class="modem-chip-state">${icon(part.state==='ok'?'check':'warning')}${e(shortStatus)}</span><span class="modem-chip-count" aria-hidden="true">${count}</span></button>`;
  }).join('');
  const key=selected?.[0],part=selected?.[1];
  return `<div class="modem-sections"><div class="modem-section-chips" role="group" aria-label="Modem diagnostic sections"><span class="modem-overall-chip ${complete?'complete':''}">${icon(complete?'check':'clock')}${e(overall)}</span>${chips}</div><div id="${e(panelId)}" class="modem-section-values" ${part?`role="region" aria-labelledby="${e(panelId+'-'+key)}"`:'hidden'}>${part?`${part.error?`<p class="form-error">${e(part.error)}</p>`:''}<div class="network-table"><table><thead><tr><th>Reported field</th><th>Reported value</th></tr></thead><tbody>${(part.fields||[]).map(f=>`<tr><td>${e(f.field)}</td><td>${e(f.value)}</td></tr>`).join('')||'<tr><td colspan="2">No usable values returned.</td></tr>'}</tbody></table></div>${part.omitted_fields?`<p class="hint">${part.omitted_fields} fields or values shortened by collection limits.</p>`:''}`:''}</div></div>`;
}
const modemAddressHelp='Use an HTTP(S) address reachable from this Harbour host.\n\nCommon modem / gateway addresses:\n• 192.168.100.1 — many cable modems; some gateways in modem mode.\n• 192.168.0.1 — some cable gateways in router mode.\n• 192.168.1.1 — many DSL / modem routers.\n• 192.168.1.254 — some ISP gateways, including AT&T.\n• 10.0.0.1 — some ISP gateways, including Xfinity.\n\nThese are examples, not detected or automatically scanned addresses. Check the device label or ISP documentation. Bridge mode, firmware and custom settings can change the address; verify that it is the modem rather than your separate router. Reachability does not imply adapter support.\n\nUse a private IPv4 address; redirects and proxies are not used. HTTPS requires a valid certificate.';
function renderModem(s){
  const v=modemView(s),d=v.data,c=d?.settings,sample=d?.sample;
  const controls=admin()?button('modem-settings','Modem settings','settings','small'):'';
  if(!d)return `<section class="modem-view" data-key="modem:${e(s.id)}"><div class="between"><h2>Modem</h2>${controls}</div><p>${e(v.error||'Loading modem settings…')}</p></section>`;
  const mode=d.demo?'Demo · no live requests':!c.enabled?'Polling off':!d.server_monitoring_enabled?'Host monitoring paused':d.blocked?'Login needs attention':`Polling every ${c.interval_seconds} seconds`;
  return `<section class="modem-view" data-key="modem:${e(s.id)}"><div class="between network-heading modem-heading"><div class="compact-heading modem-title"><h2>Modem</h2>${help('Direct modem collection','Harbour reads the modem directly from its own host. No SSH commands or collector run on the router. Collection failures alone do not identify an ISP fault. This Compal ONE adapter still needs validation against authenticated readings from your firmware.')}<span class="tag">${e(mode)}</span></div>
    <div class="network-status-strip modem-meta"><span>${e(c.url)}</span><span>Observer: ${e(d.observer)}</span><span>${d.password_saved?'Password saved':'Credentials needed'}</span><span>${sample?'Received '+e(modemTime(sample.received_at)):''}</span>${help('Evidence limits','These are modem-reported values with their original field names; scaling and units are not guessed. A complete read describes collection success, not line health. Event times use the modem clock; request and receipt times use the Harbour clock. No automatic ISP/router fault verdict is assigned.')}</div><div class="flex modem-actions">${controls}</div></div>
    ${sample?.error?`<p class="form-error">${e(sample.error)}</p>`:''}${d.stale?'<p class="form-error">This reading is old. Current line status is unknown.</p>':''}${sample?.truncated?'<p class="hint">Snapshot size limit reached; some fields were omitted.</p>':''}
    ${modemSections(s,v,sample)}
    <div class="between"><div class="compact-heading"><h3>Recent collections</h3>${help('Modem history',`Retained for up to ${c.retention_days} days or 64 MiB, whichever fills first. This view and JSON export include at most the latest 256 collections from the last 24 hours. Configuration revisions identify settings changes; older readings are not current evidence. Missing collection periods are not counted as downtime.`)}</div><a href="/api/servers/${encodeURIComponent(s.id)}/modem/export?hours=24">Export recent JSON</a></div>
    <div class="network-table"><table data-table="modem-history"><thead><tr><th data-type="date">Received</th><th>Collection</th><th>Duration</th><th>Settings revision</th></tr></thead><tbody>${[...(v.history?.samples||[])].reverse().map(row=>`<tr><td data-value="${Number(row.received_at)}">${e(modemTime(row.received_at))}</td><td>${e(modemState(row.state))}</td><td>${Number.isFinite(row.collection_ms)?e(row.collection_ms)+' ms':'—'}</td><td>${e((row.configuration_revision||'').slice(0,8))}</td></tr>`).join('')||'<tr><td colspan="4">No retained collections.</td></tr>'}</tbody></table></div>
    ${v.history?.truncated?'<p class="hint">Showing the latest 256 collections; older records remain within retention limits.</p>':''}${v.error?`<p class="form-error">${e(v.error)}</p>`:''}</section>`;
}
function paintModem(s){if(current()?.id===s.id&&state.tab==='modem')updateHTML($('#server-view'),renderModem(s));}
async function loadModem(s,force=false){
  const v=modemView(s);if(v.busy||(!force&&Date.now()-v.loaded<5000))return;
  v.busy=true;
  try{[v.data,v.history]=await Promise.all([api(`/servers/${s.id}/modem`),api(`/servers/${s.id}/modem/history`)]);v.error=null;}
  catch(error){v.error=error.message;}
  finally{v.loaded=Date.now();v.busy=false;paintModem(s);}
}
async function modemSettings(s){
  const d=await api(`/servers/${s.id}/modem`);modemView(s).data=d;
  const c=d.settings;
  modal('Modem settings',`<form id="modem-settings-form" data-id="${e(s.id)}">
    <label class="check-line"><input type="checkbox" name="enabled" ${c.enabled?'checked':''}> Enable direct polling</label>
    <div class="form-grid"><label>${fieldCaption('Modem address',modemAddressHelp)}<input name="url" type="url" value="${e(c.url)}" maxlength="120" required></label><label>${fieldCaption('Username (optional)','Leave blank for the default supplied by the modem login form, or for a supported password-only form. If the modem requires a username without providing a default, the test will ask for one. Unrecognised login protocols are not tried.')}<input name="username" value="${e(c.username)}" maxlength="80" autocomplete="off" placeholder="Automatic / password only"></label></div>
    <label>${fieldCaption('Modem password',`Encrypted in Harbour’s database using its application secret. Never returned to the browser. ${d.password_saved?'Leave blank to keep the saved password.':'Enter the modem administration password.'} Use HTTPS to access Harbour. The modem’s HTTP interface sends a password hash and session cookie without transport encryption; these remain sensitive credentials.`)}<input type="password" name="password" value="" maxlength="256" autocomplete="new-password" placeholder="${d.password_saved?'Saved · leave blank to keep':'Modem administration password'}"></label>
    ${d.password_saved?'<label class="check-line"><input type="checkbox" name="clear_password"> Remove saved password (disable polling first)</label>':''}
    <div class="form-grid"><label>${fieldCaption('Poll interval (seconds)','One sequential status read cycle, default once per minute. Failures back off to at most 15 minutes; rejected logins pause until you save settings. Testing does not change polling state. Sessions are reused.')}<input name="interval_seconds" type="number" min="30" max="900" step="1" value="${c.interval_seconds}" required></label><label>History retention (days)<input name="retention_days" type="number" min="1" max="30" step="1" value="${c.retention_days}" required></label></div>
    <div class="form-error" role="alert"></div><div class="modem-test-result hint" role="status" aria-live="polite"></div><div class="form-actions"><button type="submit" name="action" value="save" class="primary">Save</button><button type="submit" name="action" value="test" formnovalidate ${d.demo?'disabled':''}>Test connection</button></div></form>`);
}
function modemTestResult(d){
  const sample=d.sample;
  if(d.demo)return 'Demo mode: no connection was attempted.';
  if(!sample)return 'No test reading was returned.';
  if(sample.error)return sample.error+(sample.cleanup_error?' '+sample.cleanup_error:'');
  const mode=({modem_default:'using the modem’s default username',configured_username:'using the configured username',password_only:'using password-only authentication'})[sample.authentication?.mode];
  const login=sample.authentication?.state==='authenticated'?`Login succeeded${mode?' '+mode:''}. `:'';
  const sections=Object.entries(sample.sections||{}),read=sections.filter(([,v])=>v.state==='ok').map(([k])=>k);
  return login+(read.length?`Diagnostic sections read: ${read.join(', ')} (${read.length}/${sections.length}).`:'No usable diagnostic sections were returned.')+(read.length<sections.length?' Some diagnostics are unavailable.':'')+(sample.cleanup_error?' '+sample.cleanup_error:'');
}
function modemFormBusy(form,busy){
  form.dataset.busy=busy?'true':'';
  form.querySelectorAll('input,button[type=submit]').forEach(el=>{el.disabled=busy;});
}
document.addEventListener('input',event=>{
  const form=event.target.closest('#modem-settings-form');
  if(form)form.querySelector('.modem-test-result').textContent='';
});
document.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action]'),s=current();if(!s)return;
  if(el?.dataset.action==='modem-section'){
    const v=modemView(s);v.section=v.section===el.dataset.section?null:el.dataset.section;paintModem(s);return;
  }
  if(!admin())return;
  try{
    if(el?.dataset.action==='modem-settings')await modemSettings(s);
  }catch(error){toast(error.message,true);}
});
document.addEventListener('submit',async event=>{
  const form=event.target;if(form.id!=='modem-settings-form')return;event.preventDefault();
  if(form.dataset.busy)return;
  const s={id:form.dataset.id},v=modemView(s),data=Object.fromEntries(new FormData(form)),test=event.submitter?.value==='test';
  if(v.testing){formError('A modem check is already running.',form);return;}
  modemFormBusy(form,true);form.querySelector('.form-error').textContent='';form.querySelector('.modem-test-result').textContent='';
  try{
    const connection={url:data.url,username:data.username,password:data.password,clear_password:data.clear_password==='on'};
    if(test){
      v.testing=true;form.querySelector('.modem-test-result').textContent='Checking connection…';
      const result=await api(`/servers/${s.id}/modem/test-settings`,'POST',connection);
      if(form.isConnected)form.querySelector('.modem-test-result').textContent=modemTestResult(result);
    }else{
      v.data=await api(`/servers/${s.id}/modem`,'PUT',{...connection,enabled:data.enabled==='on',interval_seconds:Number(data.interval_seconds),retention_days:Number(data.retention_days)});
      form.elements.password.value='';markFormSaved(form);
      if(form.isConnected)closeOverlay(true);
      await loadModem(s,true);
    }
    connection.password='';
  }catch(error){if(form.isConnected){form.querySelector('.modem-test-result').textContent='';formError(error.message,form);}else toast(error.message,true);}
  finally{data.password='';v.testing=false;if(form.isConnected){modemFormBusy(form,false);if(v.data?.demo)form.querySelector('[value=test]').disabled=true;}paintModem(s);}
});
