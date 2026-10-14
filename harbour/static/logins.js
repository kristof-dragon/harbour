'use strict';
const loginViews = new Map();
const loginColumns=[{key:'time',label:'Login time',type:'date'},{key:'duration',label:'Duration (s)',type:'number'},{key:'status',label:'Status'},{key:'username',label:'User'},{key:'service',label:'Service / method'},{key:'source',label:'Source'},{key:'key',label:'SSH key'}];
function loginTableId(s){const id='logins:'+s.id,t=tableState(id);if(!t.sort){t.sort='time';t.order='desc';}t.dates=['time'];return id;}

function loginView(id){
  if(!loginViews.has(id))loginViews.set(id,{view:'sessions',outcome:'all',search:'',hide:false,before:0,pages:[],data:null,error:'',loaded:0,pending:false,generation:0});
  return loginViews.get(id);
}
function loginTime(value){return value?new Date(value*1000).toLocaleString():'Unavailable';}
function loginStatus(s,v){
  const data=v.data||{},status=data.status||{},sources=Object.entries(status.sources||{});
  const stale=status.fetched_at&&Date.now()/1000-status.fetched_at>Math.max(90,s.poll_seconds*2);
  const incomplete=sources.some(([,v])=>v.state!=='listening')||status.state!=='connected'||stale||data.paused||data.host_error;
  const label=data.paused?'Collection transfer paused':stale||data.host_error?'Last known collector status':({pending:'Awaiting first collector check',not_installed:'Collector not installed',permission_denied:'Collector permission required',error:'Collector unavailable',connected:incomplete?'Partial coverage':'Collector connected'})[status.state]||'Awaiting collector check';
  return `<details class="login-status" data-key="collector-status:${e(s.id)}"><summary class="tag ${incomplete||status.dropped?'yellow':'green'}" title="Collector details">${icon('shield')}<b>${label}</b>${status.dropped?`<span>· ${e(status.dropped)} lost</span>`:''}${icon('chevron')}</summary><div class="login-status-detail"><b>${status.fetched_at?'Fetched '+age(status.fetched_at):'Not checked'}</b>${status.detail?`<p>${e(status.detail)}</p>`:''}${sources.length?`<div class="login-sources">${sources.map(([name,v])=>`<span title="${e(v.detail)}">${e(name)} · <b>${e(v.state)}</b>${v.detail?`<small>${e(v.detail)}</small>`:''}</span>`).join('')}</div>`:''}${status.pending?`<p>${count(status.pending,'event')} waiting on the host.</p>`:''}${status.dropped?`<p class="form-error">${count(status.dropped,'unacknowledged event')} lost at the host queue limit. Last loss: ${e(loginTime(status.last_drop_at))}.</p>`:''}${status.ack_warning?`<p class="hint">${e(status.ack_warning)}</p>`:''}${(status.limitations||[]).map(x=>`<p class="hint">${e(x)}</p>`).join('')}${data.paused?'<p class="hint">The host collector continues recording; Harbour will fetch the queue when monitoring resumes.</p>':''}</div></details>`;
}
function renderLoginEvent(event){
  const tone=['failure','invalid_user','rejected','gap'].includes(event.result)?'red':event.result==='success'?'green':'';
  const fields=[['Event',event.event_type],['Source',event.source],['Collector',event.collector_id],['Event time',loginTime(event.occurred_at)],['Captured on host',loginTime(event.captured_at)],['Collected / seen by Harbour',loginTime(event.collected_at)],['Acknowledged on host',event.acknowledged_at?loginTime(event.acknowledged_at):'Pending next probe'],['User ID',event.uid],['Process ID',event.pid],['Session',event.session_id],['Session started',event.session_started_at?loginTime(event.session_started_at):null],['Session duration',event.duration_seconds!=null?event.duration_seconds+' seconds':null],['Key algorithm',event.key_algorithm],['Credential proof',event.key_fingerprint?(event.credential_verified?'Authentication verified':'Offered / rejected; possession not established'):null],['Certificate ID',event.certificate_id],['Certificate serial',event.certificate_serial],['CA fingerprint',event.ca_fingerprint],['Token public-key hash',event.token_public_key_hash],['Token identifier',event.token_id],['Directory',event.directory],['Viewer Apple ID',event.viewer_appleid],['Desktop user',event.session_username]];
  return `<details class="login-event" data-key="login:${event.id}"><summary><time title="${e(loginTime(event.occurred_at||event.captured_at))}">${e(loginTime(event.occurred_at||event.captured_at))}${!event.occurred_at?'<small>capture time</small>':''}</time><span>${event.duration_seconds!=null?e(loginDuration(event.duration_seconds)):'—'}</span><span class="tag ${tone}">${e(event.result.replaceAll('_',' '))}</span><span><b>${e(event.username||'Unknown user')}</b>${state.data?.bridge_enabled&&typeof bridgeTags==='function'?bridgeTags(event):''}${event.harbour_key?'<small title="Matches the SSH key and user configured for Harbour monitoring.">Harbour key</small>':''}</span><span>${e(event.service)}<small>${e(event.method||'Method unavailable')}</small></span><span class="mono">${e(event.source_ip||'No source IP')}${event.source_port?`<small>port ${e(event.source_port)}</small>`:''}</span><span class="mono login-fingerprint" title="${e(event.key_fingerprint||'')}">${e(event.key_fingerprint||'No key recorded')}</span>${icon('chevron')}</summary><div class="login-event-detail">${typeof bridgeRecordButton==='function'?bridgeRecordButton(event):''}<dl>${fields.filter(([,value])=>value!==null&&value!==undefined&&value!=='').map(([name,value])=>`<div><dt>${e(name)}</dt><dd>${e(value)}</dd></div>`).join('')}</dl>${event.evidence?`<pre>${e(event.evidence)}</pre>`:''}</div></details>`;
}
function loginDuration(seconds){
  if(seconds==null)return 'Unknown';
  seconds=Math.max(0,Math.floor(seconds));
  const h=Math.floor(seconds/3600),m=Math.floor(seconds%3600/60),s=seconds%60;
  return [h?h+'h':'',m?m+'m':'',s+'s'].filter(Boolean).join(' ');
}
function sessionPresence(s,v){
  const p=v.data?.session_presence||{};
  return {...p,available:p.available&&!v.error&&p.fetched_at&&Date.now()/1000-p.fetched_at<=Math.max(90,(s.poll_seconds||60)*2)};
}
function renderLoginSession(session,prefix='session',available=true){
  const status=session.session_status==='active'&&!available?'unconfirmed':session.session_status;
  const label={active:'Connected',closed:'Closed',ended:'Ended',unconfirmed:'Unconfirmed'}[status]||'Unconfirmed';
  const started=session.opened_at||session.started_at;
  const duration=status==='closed'?session.duration_seconds:status==='active'&&started?Date.now()/1000-started:null;
  const note=status==='ended'?'No longer present on the host; logout time was not recorded.':status==='unconfirmed'?'Current connection state is not verified.':!session.event_count?'Detected on the host; its login event is not available.':'';
  return `<details class="login-event login-session${status==='active'?' is-connected':''}" data-key="${prefix}:${e(session.id)}"><summary><time>${e(loginTime(session.started_at))}</time><span>${duration!=null?e(loginDuration(duration)):'—'}</span><span class="tag ${status==='active'?'green':status==='unconfirmed'?'yellow':''}">${label}</span><span><b>${e(session.username||'Unknown user')}</b>${state.data?.bridge_enabled&&typeof bridgeTags==='function'?bridgeTags(session):''}${session.harbour_key?'<small title="Matches the SSH key and user configured for Harbour monitoring.">Harbour key</small>':''}</span><span>${e(session.service)}<small>${e(session.method||'Method unavailable')}</small></span><span class="mono">${e(session.source_ip||'Address not recorded')}${session.source_port?`<small>port ${e(session.source_port)}</small>`:''}</span><span class="mono login-fingerprint" title="${e(session.key_fingerprint||'')}">${e(session.key_fingerprint||'No key recorded')}</span>${icon('chevron')}</summary><div class="login-event-detail">${typeof bridgeRecordButton==='function'?bridgeRecordButton(session):''}${note?`<p class="hint">${note}</p>`:''}<dl>${[['Logged in',loginTime(session.started_at)],['Session opened',session.opened_at?loginTime(session.opened_at):null],['Logged out',session.ended_at?loginTime(session.ended_at):null],['Last confirmed connected',session.confirmed_at?loginTime(session.confirmed_at):null],['Duration',duration!=null?loginDuration(duration):null],['Process ID',session.pid],['Session ID',session.session_id],['Boot ID',session.boot_id]].filter(([,v])=>v!=null).map(([k,v])=>`<div><dt>${e(k)}</dt><dd>${e(v)}</dd></div>`).join('')}</dl>${session.source_ip?'<p class="hint">The address and any key details come from this session’s matching authentication record.</p>':''}${session.evidence_events?.length?`<div class="login-evidence"><h3>Session history</h3>${session.evidence_events.map(renderLoginEvent).join('')}</div>`:''}</div></details>`;
}
function renderCurrentLogins(s,v){
  const p=sessionPresence(s,v),sessions=v.data?.current_sessions||[];
  return `<section class="login-current" aria-label="Currently connected"><div class="login-current-heading"><h3>Currently connected</h3>${p.available?`<span class="login-current-checked" title="${e(loginTime(p.checked_at))}. Updates arrive via collector notifications when The Bridge is enabled and the collector supports them; otherwise they follow the monitoring interval.">Checked ${e(new Date(p.checked_at*1000).toLocaleTimeString())}</span>`:''}<span class="tag ${p.available?'green':'yellow'}">${p.available?count(sessions.length,'SSH connection'):'Not verified'}</span></div>${!p.available?'<p class="hint">Current SSH sessions are unavailable or the host check is stale. This does not mean nobody is logged in.</p>':''}${p.available?(sessions.map(x=>renderLoginSession(x,'current',true)).join('')||'<p class="hint">No active SSH sessions were reported at the last host check.</p>'):''}</section>`;
}
function renderLogins(s){
  const v=loginView(s.id),events=v.data?.events||[],id=loginTableId(s);
  tableRefreshers.set(id,()=>{v.generation++;v.pending=false;v.loaded=0;loadLogins(s,true);});
  return `<section class="logins-view" data-key="logins:${e(s.id)}"><div class="between login-header"><div><div class="login-title"><h2>Logins</h2>${loginStatus(s,v)}</div></div><div class="flex">${button('login-reload','Refresh','refresh','small')}${admin()?button('login-setup','Collector setup','settings','small'):''}</div></div>${renderCurrentLogins(s,v)}<form id="login-filter" class="login-filters"><label>View<select name="view">${[['sessions','Sessions'],['events','Raw events']].map(([key,label])=>`<option value="${key}" ${v.view===key?'selected':''}>${label}</option>`).join('')}</select></label><label>Result<select name="outcome">${[['all','All activity'],['success','Successful authentication'],['failure','Failed / rejected'],['sessions','Session activity'],['collector','Collection gaps']].map(([key,label])=>`<option value="${key}" ${v.outcome===key?'selected':''}>${label}</option>`).join('')}</select></label><label class="login-search">User, IP or key<input name="search" maxlength="128" value="${e(v.search)}" placeholder="Search login history"></label><label class="check"><input type="checkbox" name="hide" ${v.hide?'checked':''}>Hide Harbour key</label><button type="submit">Filter</button></form>${v.error?`<p role="alert" class="form-error">${e(v.error)} · previously loaded events remain below.</p>`:''}<div data-table-key="${e(id)}"><div class="login-table-heading data-table-head">${loginColumns.map(c=>tableColumn(id,c,'span')).join('')}<span></span></div><div class="login-events">${events.map(x=>x.record_type==='session'?renderLoginSession(x,'session',sessionPresence(s,v).available):renderLoginEvent(x)).join('')||`<div class="empty">${icon('users')}<h3>${v.pending?'Loading events…':'No collected events'}</h3><p>${v.data?.status?.state==='not_installed'?'Install the collector on this host to begin recording.':'No events match this view. Check collector coverage above.'}</p></div>`}</div>${tableControls(id,v.data?.pagination?.total||0)}</div></section>`;
}
async function loadLogins(s,force=false){
  const v=loginView(s.id);
  if(v.pending||(!force&&Date.now()-v.loaded<10000))return;
  const generation=++v.generation;
  v.pending=true;
  const query=new URLSearchParams({...tableQuery(loginTableId(s)),view:v.view,outcome:v.outcome,search:v.search,hide_harbour:v.hide});
  try{const data=await api(`/servers/${encodeURIComponent(s.id)}/logins/table?${query}`);if(v.generation===generation){v.data=data;v.error='';if(data.pagination)tableState(loginTableId(s)).page=data.pagination.page;}}
  catch(err){if(v.generation===generation)v.error=err.message;}
  finally{if(v.generation===generation){v.pending=false;v.loaded=Date.now();if(current()?.id===s.id&&state.tab==='logins')updateHTML($('#server-view'),renderLogins(current()));}}
}
function resetLoginView(v){v.generation++;v.pending=false;v.loaded=0;v.data=null;v.error='';}
document.addEventListener('submit',event=>{
  if(event.target.id!=='login-filter')return;
  event.preventDefault();const s=current(),v=loginView(s.id),form=new FormData(event.target);
  tableState(loginTableId(s)).page=1;Object.assign(v,{view:form.get('view'),outcome:form.get('outcome'),search:form.get('search'),hide:form.has('hide'),before:0,pages:[]});resetLoginView(v);renderMain();
});
document.addEventListener('click',event=>{
  document.querySelectorAll('.login-status[open]').forEach(node=>{if(!node.contains(event.target))node.open=false;});
  const action=event.target.closest('[data-action]')?.dataset.action;
  if(!action?.startsWith('login-'))return;
  const s=current(),v=loginView(s.id);
  if(action==='login-setup'){
    modal('Login collector setup',`<div class="stack"><p>Install once on <b>${e(s.name)}</b>, using its Harbour SSH account <b>${e(s.username)}</b>. The collector starts at boot and keeps events while Harbour is disconnected.</p>${collectorSetupControls(s,'logins')}<p><a class="button" href="/api/login-collector/download">Download collector bundle</a></p><ol><li>Copy and extract the bundle on the monitored host.</li><li>Preview: <code>python3 collector_install.py --reader YOUR_SSH_USER --dry-run</code></li><li>Install: <code>sudo python3 collector_install.py --reader YOUR_SSH_USER</code></li><li>For detailed SSH attempts, set <code>LogLevel VERBOSE</code> in the host's SSH configuration, validate with <code>sshd -t</code>, and apply it using the host's service manager.</li></ol><p>The local queue holds 20,000 events by default. Acknowledged records expire after 24 hours. Harbour retains its copy for 90 days.</p><p>macOS log collection provides limited coverage. Broader OS login events need the bundled native helper, an Apple Endpoint Security entitlement and Full Disk Access. Build and installation instructions are in the bundle.</p><p class="hint">The installer uses administrator privileges on the host. Normal Harbour probes use the existing SSH account and a private local socket. No additional network port is opened.</p></div>`);return;
  }
  if(action==='login-older'&&v.data?.next_before){v.pages.push(v.before);v.before=v.data.next_before;}
  else if(action==='login-newer'&&v.pages.length)v.before=v.pages.pop();
  else if(action==='login-reload'){v.before=0;v.pages=[];v.generation++;v.pending=false;v.loaded=0;renderMain();return;}
  else return;
  resetLoginView(v);renderMain();
});

document.addEventListener('keydown',event=>{if(event.key==='Escape')document.querySelectorAll('.login-status[open]').forEach(node=>{node.open=false;});});
