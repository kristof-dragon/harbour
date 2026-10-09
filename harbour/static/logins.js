'use strict';
const loginViews = new Map();
function loginView(id){
  if(!loginViews.has(id))loginViews.set(id,{outcome:'all',search:'',hide:false,before:0,pages:[],data:null,error:'',loaded:0,pending:false,generation:0});
  return loginViews.get(id);
}
function loginTime(value){return value?new Date(value*1000).toLocaleString():'Unavailable';}
function loginStatus(s,v){
  const data=v.data||{},status=data.status||{},sources=Object.entries(status.sources||{});
  const stale=status.fetched_at&&Date.now()/1000-status.fetched_at>Math.max(90,s.poll_seconds*2);
  const incomplete=sources.some(([,v])=>v.state!=='listening')||status.state!=='connected'||stale||data.paused||data.host_error;
  const label=data.paused?'Collection transfer paused':stale||data.host_error?'Last known collector status':({pending:'Awaiting first collector check',not_installed:'Collector not installed',permission_denied:'Collector permission required',error:'Collector unavailable',connected:incomplete?'Partial coverage':'Collector connected'})[status.state]||'Awaiting collector check';
  return `<section class="login-status" aria-label="Login collector status"><div class="between"><b>${icon('shield')}${label}</b><span class="tag ${incomplete?'yellow':'green'}">${status.fetched_at?'Fetched '+age(status.fetched_at):'Not checked'}</span></div>${status.detail?`<p>${e(status.detail)}</p>`:''}${sources.length?`<div class="login-sources">${sources.map(([name,v])=>`<span title="${e(v.detail)}">${e(name)} · <b>${e(v.state)}</b>${v.detail?`<small>${e(v.detail)}</small>`:''}</span>`).join('')}</div>`:''}${status.pending?`<p>${count(status.pending,'event')} waiting on the host.</p>`:''}${status.dropped?`<p class="form-error">${count(status.dropped,'unacknowledged event')} lost at the host queue limit. Last loss: ${e(loginTime(status.last_drop_at))}.</p>`:''}${status.ack_warning?`<p class="hint">${e(status.ack_warning)}</p>`:''}${(status.limitations||[]).map(x=>`<p class="hint">${e(x)}</p>`).join('')}${data.paused?'<p class="hint">The host collector continues recording; Harbour will fetch the queue when monitoring resumes.</p>':''}</section>`;
}
function renderLoginEvent(event){
  const tone=['failure','invalid_user','rejected','gap'].includes(event.result)?'red':event.result==='success'?'green':'';
  const fields=[['Event',event.event_type],['Source',event.source],['Collector',event.collector_id],['Event time',loginTime(event.occurred_at)],['Captured on host',loginTime(event.captured_at)],['Collected / seen by Harbour',loginTime(event.collected_at)],['Acknowledged on host',event.acknowledged_at?loginTime(event.acknowledged_at):'Pending next probe'],['User ID',event.uid],['Process ID',event.pid],['Session',event.session_id],['Session started',event.session_started_at?loginTime(event.session_started_at):null],['Session duration',event.duration_seconds!=null?event.duration_seconds+' seconds':null],['Key algorithm',event.key_algorithm],['Credential proof',event.key_fingerprint?(event.credential_verified?'Authentication verified':'Offered / rejected; possession not established'):null],['Certificate ID',event.certificate_id],['Certificate serial',event.certificate_serial],['CA fingerprint',event.ca_fingerprint],['Token public-key hash',event.token_public_key_hash],['Token identifier',event.token_id],['Directory',event.directory],['Viewer Apple ID',event.viewer_appleid],['Desktop user',event.session_username]];
  return `<details class="login-event" data-key="login:${event.id}"><summary><time title="${e(loginTime(event.occurred_at||event.captured_at))}">${e(loginTime(event.occurred_at||event.captured_at))}${!event.occurred_at?'<small>capture time</small>':''}</time><span class="tag ${tone}">${e(event.result.replaceAll('_',' '))}</span><span><b>${e(event.username||'Unknown user')}</b>${event.harbour_key?'<small>Harbour key</small>':''}</span><span>${e(event.service)}<small>${e(event.method||'Method unavailable')}</small></span><span class="mono">${e(event.source_ip||'No source IP')}${event.source_port?`<small>port ${e(event.source_port)}</small>`:''}</span><span class="mono login-fingerprint" title="${e(event.key_fingerprint||'')}">${e(event.key_fingerprint||'No key recorded')}</span>${icon('chevron')}</summary><div class="login-event-detail"><dl>${fields.filter(([,value])=>value!==null&&value!==undefined&&value!=='').map(([name,value])=>`<div><dt>${e(name)}</dt><dd>${e(value)}</dd></div>`).join('')}</dl>${event.evidence?`<pre>${e(event.evidence)}</pre>`:''}</div></details>`;
}
function renderLogins(s){
  const v=loginView(s.id),events=v.data?.events||[];
  return `<section class="logins-view" data-key="logins:${e(s.id)}"><div class="between"><div><h2>Logins</h2><p class="hint">Authentication and session events · retained for 90 days</p></div><div class="flex">${button('login-reload','Refresh','refresh','small')}${admin()?button('login-setup','Collector setup','settings','small'):''}</div></div>${loginStatus(s,v)}<form id="login-filter" class="login-filters"><label>Result<select name="outcome">${[['all','All events'],['success','Successful authentication'],['failure','Failed / rejected'],['sessions','Session activity'],['collector','Collection gaps']].map(([key,label])=>`<option value="${key}" ${v.outcome===key?'selected':''}>${label}</option>`).join('')}</select></label><label class="login-search">User, IP or key<input name="search" maxlength="128" value="${e(v.search)}" placeholder="Search collected events"></label><label class="check"><input type="checkbox" name="hide" ${v.hide?'checked':''}>Hide Harbour key</label><button type="submit">Filter</button></form>${v.error?`<p role="alert" class="form-error">${e(v.error)} · previously loaded events remain below.</p>`:''}<div class="login-table-heading" aria-hidden="true"><span>Time</span><span>Result</span><span>User</span><span>Service / method</span><span>Source</span><span>SSH key</span><span></span></div><div class="login-events">${events.map(renderLoginEvent).join('')||`<div class="empty">${icon('users')}<h3>${v.pending?'Loading events…':'No collected events'}</h3><p>${v.data?.status?.state==='not_installed'?'Install the collector on this host to begin recording.':'No events match this view. Check collector coverage above.'}</p></div>`}</div><div class="between login-pagination">${button('login-newer','Newer','','small',v.pages.length?'':'disabled')}<span class="hint">${events.length} events · ${v.before?'historical page':'latest page'}</span>${button('login-older','Older','','small',v.data?.next_before?'':'disabled')}</div></section>`;
}
async function loadLogins(s,force=false){
  const v=loginView(s.id);
  if(v.pending||(!force&&Date.now()-v.loaded<10000))return;
  const generation=++v.generation;
  v.pending=true;
  const query=new URLSearchParams({before:v.before,outcome:v.outcome,search:v.search,hide_harbour:v.hide});
  try{const data=await api(`/servers/${encodeURIComponent(s.id)}/logins?${query}`);if(v.generation===generation){v.data=data;v.error='';}}
  catch(err){if(v.generation===generation)v.error=err.message;}
  finally{if(v.generation===generation){v.pending=false;v.loaded=Date.now();if(current()?.id===s.id&&state.tab==='logins')updateHTML($('#server-view'),renderLogins(current()));}}
}
function resetLoginView(v){v.generation++;v.pending=false;v.loaded=0;v.data=null;v.error='';}
document.addEventListener('submit',event=>{
  if(event.target.id!=='login-filter')return;
  event.preventDefault();const s=current(),v=loginView(s.id),form=new FormData(event.target);
  Object.assign(v,{outcome:form.get('outcome'),search:form.get('search'),hide:form.has('hide'),before:0,pages:[]});resetLoginView(v);renderMain();
});
document.addEventListener('click',event=>{
  const action=event.target.closest('[data-action]')?.dataset.action;
  if(!action?.startsWith('login-'))return;
  const s=current(),v=loginView(s.id);
  if(action==='login-setup'){
    modal('Login collector setup',`<div class="stack"><p>Install once on <b>${e(s.name)}</b>, using its Harbour SSH account <b>${e(s.username)}</b>. The collector starts at boot and keeps events while Harbour is disconnected.</p>${collectorSetupControls(s,'logins')}<p><a class="button" href="/api/login-collector/download">Download collector bundle</a></p><ol><li>Copy and extract the bundle on the monitored host.</li><li>Preview: <code>python3 collector_install.py --reader YOUR_SSH_USER --dry-run</code></li><li>Install: <code>sudo python3 collector_install.py --reader YOUR_SSH_USER</code></li><li>For detailed SSH attempts, set <code>LogLevel VERBOSE</code> in the host's SSH configuration, validate with <code>sshd -t</code>, and apply it using the host's service manager.</li></ol><p>The local queue holds 20,000 events by default. Acknowledged records expire after 24 hours. Harbour retains its copy for 90 days.</p><p>macOS log collection provides limited coverage. Broader OS login events need the bundled native helper, an Apple Endpoint Security entitlement and Full Disk Access. Build and installation instructions are in the bundle.</p><p class="hint">The installer uses administrator privileges on the host. Normal Harbour probes use the existing SSH account and a private local socket. No additional network port is opened.</p></div>`);return;
  }
  if(action==='login-older'&&v.data?.next_before){v.pages.push(v.before);v.before=v.data.next_before;}
  else if(action==='login-newer'&&v.pages.length)v.before=v.pages.pop();
  else if(action==='login-reload'){v.before=0;v.pages=[];}
  else return;
  resetLoginView(v);renderMain();
});
