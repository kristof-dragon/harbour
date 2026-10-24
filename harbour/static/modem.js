'use strict';
const modemViews=new Map();
function modemView(s){if(!modemViews.has(s.id))modemViews.set(s.id,{data:null,history:null,loaded:0,busy:false,testing:false,error:null});return modemViews.get(s.id);}
const modemTime=t=>Number.isFinite(t)?new Date(t*1000).toLocaleString():'—';
const modemState=s=>({ok:'Read complete',partial:'Partial reading',unavailable:'Collection gap'})[s]||'No readings';
function renderModem(s){
  const v=modemView(s),d=v.data,c=d?.settings,sample=d?.sample;
  const controls=admin()?button('modem-settings','Modem settings','settings','small')+(d?button('modem-test',v.testing?'Checking…':'Test connection','refresh','small',v.testing||d.demo||!d.password_saved?'disabled':''):''):'';
  if(!d)return `<section class="modem-view" data-key="modem:${e(s.id)}"><div class="between"><h2>Modem</h2>${controls}</div><p>${e(v.error||'Loading modem settings…')}</p></section>`;
  const mode=d.demo?'Demo · no live requests':!c.enabled?'Polling off':!d.server_monitoring_enabled?'Host monitoring paused':d.blocked?'Login needs attention':`Polling every ${c.interval_seconds} seconds`;
  return `<section class="modem-view" data-key="modem:${e(s.id)}"><div class="between network-heading"><div class="compact-heading"><h2>Modem</h2>${help('Direct modem collection','Harbour reads the modem directly from its own host. No SSH commands or collector run on the router. Collection failures alone do not identify an ISP fault. This Compal ONE adapter still needs validation against authenticated readings from your firmware.')}</div><div class="flex">${controls}</div></div>
    <div class="network-status-strip"><span class="tag">${e(mode)}</span><span>${e(c.url)}</span><span>Observer: ${e(d.observer)}</span><span>${d.password_saved?'Password saved':'Credentials needed'}</span></div>
    <div class="compact-heading"><h3>${e(modemState(sample?.state))}${d.stale?' · stale':''}</h3><span class="hint">${sample?'Received '+e(modemTime(sample.received_at)):''}</span>${help('Evidence limits','These are modem-reported values with their original field names; scaling and units are not guessed. A complete read describes collection success, not line health. Event times use the modem clock; request and receipt times use the Harbour clock. No automatic ISP/router fault verdict is assigned.')}</div>
    ${sample?.error?`<p class="form-error">${e(sample.error)}</p>`:''}${d.stale?'<p class="form-error">This reading is old. Current line status is unknown.</p>':''}${sample?.truncated?'<p class="hint">Snapshot size limit reached; some fields were omitted.</p>':''}
    <div class="modem-sections">${Object.entries(sample?.sections||{}).map(([key,part])=>`<details data-key="modem-section:${e(key)}"><summary>${e(({system:'System',device:'Device',downstream:'Downstream',upstream:'Upstream',events:'Event log'})[key]||key)} · ${e(modemState(part.state))} · ${(part.fields||[]).length} values</summary>${part.error?`<p class="form-error">${e(part.error)}</p>`:''}<div class="network-table"><table><thead><tr><th>Reported field</th><th>Reported value</th></tr></thead><tbody>${(part.fields||[]).map(f=>`<tr><td>${e(f.field)}</td><td>${e(f.value)}</td></tr>`).join('')||'<tr><td colspan="2">No usable values returned.</td></tr>'}</tbody></table></div>${part.omitted_fields?`<p class="hint">${part.omitted_fields} fields or values shortened by collection limits.</p>`:''}</details>`).join('')}</div>
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
    <div class="form-grid"><label>${fieldCaption('Modem address','Private IPv4 HTTP(S) address, reachable from this Harbour instance. Redirects and proxies are not used. HTTPS requires a valid certificate.')}<input name="url" type="url" value="${e(c.url)}" maxlength="120" required></label><label>Username<input name="username" value="${e(c.username)}" maxlength="80" autocomplete="off"></label></div>
    <label>${fieldCaption('Modem password',`Encrypted in Harbour’s database using its application secret. Never returned to the browser. ${d.password_saved?'Leave blank to keep the saved password.':'Enter the modem administration password.'} Use HTTPS to access Harbour. The modem’s HTTP interface sends a password hash and session cookie without transport encryption; these remain sensitive credentials.`)}<input type="password" name="password" value="" maxlength="256" autocomplete="new-password" placeholder="${d.password_saved?'Saved · leave blank to keep':'Modem administration password'}"></label>
    ${d.password_saved?'<label class="check-line"><input type="checkbox" name="clear_password"> Remove saved password (disable polling first)</label>':''}
    <div class="form-grid"><label>${fieldCaption('Poll interval (seconds)','One sequential status read cycle, default once per minute. Failures back off to at most 15 minutes; rejected logins pause until you save settings or test again. Sessions are reused.')}<input name="interval_seconds" type="number" min="30" max="900" step="1" value="${c.interval_seconds}" required></label><label>History retention (days)<input name="retention_days" type="number" min="1" max="30" step="1" value="${c.retention_days}" required></label></div>
    <div class="form-error" role="alert"></div><div class="form-actions"><button type="submit" class="primary">Save modem settings</button></div></form>`);
}
document.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action]'),s=current();if(!s||!admin())return;
  try{
    if(el?.dataset.action==='modem-settings')await modemSettings(s);
    if(el?.dataset.action==='modem-test'){
      const v=modemView(s);if(v.testing)return;v.testing=true;paintModem(s);
      try{v.data=await api(`/servers/${s.id}/modem/test`,'POST');await loadModem(s,true);}
      finally{v.testing=false;paintModem(s);}
    }
  }catch(error){toast(error.message,true);}
});
document.addEventListener('submit',async event=>{
  const form=event.target;if(form.id!=='modem-settings-form')return;event.preventDefault();
  const s={id:form.dataset.id},data=Object.fromEntries(new FormData(form)),submit=form.querySelector('[type=submit]');submit.disabled=true;
  try{await api(`/servers/${s.id}/modem`,'PUT',{enabled:data.enabled==='on',url:data.url,username:data.username,password:data.password,clear_password:data.clear_password==='on',interval_seconds:Number(data.interval_seconds),retention_days:Number(data.retention_days)});form.reset();closeOverlay(true);await loadModem(s,true);}
  catch(error){formError(error.message,form);}
  finally{data.password='';submit.disabled=false;}
});
