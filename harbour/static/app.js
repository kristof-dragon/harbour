'use strict';
const $ = (s, root = document) => root.querySelector(s);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const e = escapeHTML;
const paths = {
 temperature:'M9 14.8V5a3 3 0 0 1 6 0v9.8a5 5 0 1 1-6 0M12 8v10',
 server:'M4 3h16v7H4zM4 14h16v7H4zM7 6.5h.01M7 17.5h.01M11 6.5h6M11 17.5h6',
 cpu:'M7 7h10v10H7zM9 1v3m6-3v3M9 20v3m6-3v3M1 9h3m-3 6h3m16-6h3m-3 6h3',
 memory:'M3 6h18v12H3zM6 9v5m4-5v5m4-5v5m4-5v5M6 18v3m4-3v3m4-3v3m4-3v3',
 disk:'M5 4h14l3 10v6H2v-6zM2 14h20M6 17h.01M10 17h.01',
 plus:'M12 5v14M5 12h14', chevron:'m9 5 7 7-7 7', down:'m5 9 7 7 7-7',
 box:'m12 2 10 5v10l-10 5-10-5V7zM2 7l10 5 10-5M12 12v10M7 4.5l10 5',
 layers:'m12 3 10 5-10 5L2 8zM2 12l10 5 10-5M2 16l10 5 10-5',
 refresh:'M20 7V3l-3 3a8 8 0 1 0 3 9M20 3h-5',
 warning:'m12 3 10 18H2zM12 9v5M12 17h.01',
 download:'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5',
 play:'m7 4 14 8-14 8z', bell:'M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4',
 sun:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M12 1v2m0 18v2M1 12h2m18 0h2M4 4l2 2m12 12 2 2M4 20l2-2M18 6l2-2',
 moon:'M20 15.4A9 9 0 0 1 8.6 4 9 9 0 1 0 20 15.4z',
 settings:'M4 6h16M4 12h16M4 18h16M8 3v6m8 0v6m-6 0v6',
 users:'M16 21v-3a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v3M9 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8M16 3a4 4 0 0 1 0 8M22 21v-3a4 4 0 0 0-3-3.87',
 logout:'M9 21H3V3h6M10 12h12m-5-5 5 5-5 5',
 search:'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14M15 15l6 6',
 close:'m6 6 12 12M6 18 18 6', check:'m5 12 4 4L19 6', clock:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M12 7v5l3 2',
 shield:'m12 2 9 4v6c0 5-9 10-9 10S3 17 3 12V6zM8 11l3 3 5-5',
 key:'M8 3a5 5 0 1 0 0 10 5 5 0 0 0 0-10M12 12l9 9m-3-3 3-3m-6 0 3-3',
 copy:'M9 9h12v12H9zM15 9V3H3v12h6', menu:'M3 6h18M3 12h18M3 18h18',
 activity:'M2 12h4l3-8 6 16 3-8h4', info:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M12 11v6M12 7h.01',
 trash:'M3 6h18M8 6V3h8v3M5 6l1 15h12l1-15M10 10v7m4-7v7',
 network:'M9 2h6v6H9zM2 16h6v6H2zM16 16h6v6h-6zM12 8v4M5 16v-4h14v4',
 terminal:'m4 5 6 6-6 6M12 19h8', globe:'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20M2 12h20M12 2a20 20 0 0 1 0 20 20 20 0 0 1 0-20'
};
const icon = (name, cls='') => `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.box}"/></svg>`;
const state = {user:null, data:null, server:localStorage.getItem('harbour-server'), tab:'containers', open:new Set(), selected:new Set(), search:'', notices:null, modal:null, mobile:false, lastJobs:new Map(), key:null, keyMode:'generate', demo:false, version:'', menuOpen:localStorage.getItem('harbour-menu-open')==='true', filters:{warnings:'all',status:'all',updates:'all'}, authMessage:'', securityTab:'policy', logOffset:0, totpSetup:null};
document.documentElement.dataset.density = ['compact','normal','comfortable'].includes(localStorage.getItem('harbour-density')) ? localStorage.getItem('harbour-density') : 'normal';
document.documentElement.dataset.resourceDensity = ['compact','normal','comfortable'].includes(localStorage.getItem('harbour-resource-density')) ? localStorage.getItem('harbour-resource-density') : 'normal';
const storedTheme = localStorage.getItem('harbour-theme');
const systemTheme=matchMedia('(prefers-color-scheme:dark)');
let followSystem=localStorage.getItem('harbour-theme-mode')==='system'||(!localStorage.getItem('harbour-theme-mode')&&!storedTheme);
function applyTheme(){
  document.documentElement.dataset.theme=followSystem?(systemTheme.matches?'dark':'light'):(localStorage.getItem('harbour-theme')==='light'?'light':'dark');
  if($('#topbar')&&state.data)renderTop();
  updateSlidingControl($('#visual-theme'),followSystem?'system':document.documentElement.dataset.theme);
}
applyTheme();
systemTheme.addEventListener('change',()=>{if(followSystem)applyTheme();});
const width = Number(localStorage.getItem('harbour-width'));
if (width >= 18 && width <= 40) document.documentElement.style.setProperty('--sidebar', width+'%');
const admin = () => state.user?.role === 'admin';
const current = () => state.data?.servers.find(s => s.id === state.server);
const gb = n => (Number(n || 0)/1e9).toLocaleString(undefined,{maximumFractionDigits:1});
const percent = n => Number(n || 0).toFixed(1);
const age = n => {if (!n) return 'Not yet checked'; const s=Math.max(0,Date.now()/1000-n); return s<60?'Just now':s<3600?Math.floor(s/60)+'m ago':s<86400?Math.floor(s/3600)+'h ago':Math.floor(s/86400)+'d ago';};
const count = (n, singular, plural=singular+'s') => `${n} ${n === 1 ? singular : plural}`;
const actionLabel = a => ({pull:'Pull images',up:'Apply with Compose',restart:'Restart',refresh:'Refresh readings',check:'Check updates'}[a] || a);
const button = (action,label,ico='',cls='',attrs='') => `<button type="button" data-action="${action}" class="${cls}" ${attrs}>${ico?icon(ico):''}${label}</button>`;
const iconButton = (action,label,ico,attrs='') => button(action,'',ico,'ghost icon-button',`aria-label="${e(label)}" title="${e(label)}" ${attrs}`);
function help(label,text){return button('help','','info','help-button',`aria-label="${e(label)} help" data-help="${e(text)}"`);}
function fieldCaption(label,text){return `<span class="field-caption">${e(label)}${help(label,text)}</span>`;}
function rememberFocus(){
  const el=document.activeElement;
  if(!el||el===document.body)return null;
  if(el.id)return '#'+CSS.escape(el.id);
  if(el.getAttribute('aria-label'))return `${el.tagName.toLowerCase()}[aria-label="${CSS.escape(el.getAttribute('aria-label'))}"]`;
  if(el.dataset.action)return '[data-action="'+CSS.escape(el.dataset.action)+'"]'+Object.entries(el.dataset).filter(([k])=>k!=='action').map(([k,v])=>'[data-'+k.replace(/[A-Z]/g,c=>'-'+c.toLowerCase())+'="'+CSS.escape(v)+'"]').join('');
  if(el.tagName==='SUMMARY')return 'details[data-open="'+CSS.escape(el.parentElement.dataset.open)+'"] > summary';
  return null;
}

async function api(path, method='GET', body) {
  const res = await fetch('/api'+path,{method,headers:{'Content-Type':'application/json',...(state.user?{'X-CSRF-Token':state.user.csrf}:{})},...(body!==undefined?{body:JSON.stringify(body)}:{})});
  let value; try {value=await res.json();} catch {throw new Error('The server returned an unreadable response.');}
  if (!res.ok) {if(res.status===401 && path!='/login'){state.authMessage=typeof value.detail==='string'?value.detail:'Please sign in again.';state.user=null;renderLogin();} throw new Error(typeof value.detail==='string'?value.detail:Array.isArray(value.detail)?value.detail.map(x=>x.loc.slice(1).join('.')+': '+x.msg).join('\n'):'Request failed');}
  return value;
}
function toast(message,error=false){const el=document.createElement('div');el.className='toast'+(error?' error':'');el.textContent=message;$('#toasts').append(el);setTimeout(()=>el.remove(),6500);}
async function load(initial=false){
  if(!state.user)return;
  try {
    const next=await api('/dashboard');
    if(!initial&&JSON.stringify(next)===JSON.stringify(state.data))return;
    const focus=rememberFocus();
    state.data=next;
    if(!current()){state.server=state.data.servers[0]?.id;state.selected.clear();}
    for(const job of state.data.jobs){
      const old=state.lastJobs.get(job.id);
      if(old && ['queued','running'].includes(old) && !['queued','running'].includes(job.status))toast(`${actionLabel(job.action)} · ${job.server_name}: ${job.status}`,job.status==='failed');
      state.lastJobs.set(job.id,job.status);
    }
    if(initial || !$('#shell'))renderShell(); else {renderSidebar();renderTop();renderMain();}
    if(state.notices)renderNotices();
    if(focus)$(focus)?.focus({preventScroll:true});
  } catch(err){if(initial)toast(err.message,true);else if($('#connection-status'))$('#connection-status').textContent='Dashboard connection lost · retrying';}
}

function renderLogin(){
  state.mobile=false;document.body.classList.remove('mobile-panel-open');
  state.modal=null;state.notices=null;$('#overlay').innerHTML='';
  $('#app').innerHTML=`<main class="login-page"><section class="login-identity"><div class="brand"><img src="/static/favicon.svg" alt="">Harbour</div><div class="login-copy"><h1>A home for<br>your <span>servers.</span></h1><p>Keep an eye on your infrastructure.<br>Give every container a little attention.</p><div class="login-features"><span>${icon('activity')}Live monitoring</span><span>${icon('layers')}Compose aware</span><span>${icon('shield')}Secure access</span></div></div><div class="login-bottom">Your infrastructure. Your control.</div></section><section class="login-form-wrap"><form class="login-form" id="login-form"><h2>Welcome aboard</h2><p>Sign in to your server workspace.</p>${state.authMessage?`<p class="auth-message" role="status">${e(state.authMessage)}</p>`:''}<label>Username<input name="name" autocomplete="username" required autofocus></label><label>Password<input name="password" type="password" autocomplete="current-password" required></label><label>Authenticator or recovery code <small class="muted">if enabled</small><input name="code" autocomplete="one-time-code" maxlength="80" placeholder="6-digit code or recovery code"></label><div class="form-error" role="alert"></div><button class="primary" type="submit">Sign in ${icon('shield')}</button>${state.demo?`<div class="demo-entry"><p class="muted">Take a look around with sample servers. All actions are simulated.</p>${button('demo-login','Explore the demo','play')}</div>`:''}</form></section></main>`;
}
function renderShell(){
  $('#app').innerHTML=`<div class="app-shell" id="shell"><aside class="sidebar ${state.mobile?'mobile-open':''}" id="sidebar"></aside><button type="button" class="mobile-sidebar-backdrop" id="mobile-sidebar-backdrop" data-action="close-mobile" aria-label="Dismiss server list" ${state.mobile?'':'hidden'}></button><section class="workspace"><header class="topbar" id="topbar"></header><main class="main" id="main"></main></section></div>`;
  renderSidebar();renderTop();renderMain();syncMobilePanel();
}
function slidingControl(id,label,options,value,action,attrs=''){
  const index=options.findIndex(([v])=>v===value);
  return `<div id="${id}" class="sliding-control" role="group" aria-label="${e(label)}" data-selected="${index>=0}" style="--segments:${options.length};--selected:${Math.max(0,index)}"><span class="sliding-thumb" aria-hidden="true"></span>${options.map(([v,n])=>button(action,e(n),'','',`data-value="${v}" aria-pressed="${v===value}" ${attrs}`)).join('')}</div>`;
}
function updateSlidingControl(group,value){
  if(!group)return;
  const buttons=[...group.querySelectorAll('button')],index=buttons.findIndex(b=>b.dataset.value===value);
  group.dataset.selected=String(index>=0);
  if(index>=0)group.style.setProperty('--selected',index);
  buttons.forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.value===value)));
}
function filterControl(key,label,options){
  return `<div class="server-filter"><span class="filter-label">${label}</span>${slidingControl('server-filter-'+key,'Filter servers by '+label.toLowerCase(),options,state.filters[key],'server-filter',`data-filter="${key}" title="Click a selected filter again to show all."`)}</div>`;
}
function filteredServers(){
  const f=state.filters;
  return state.data.servers.filter(s=>(f.warnings==='all'||Boolean(s.warnings.length)===(f.warnings==='with'))&&(f.updates==='all'||Boolean(s.updates)===(f.updates==='with'))&&(f.status==='all'||connectionState(s)===f.status));
}
function serverCards(servers){
  return servers.map(s=>`<button type="button" class="server-item ${s.id===state.server?'selected':''} ${s.warnings.length?'has-warning':''}" data-action="select-server" data-id="${e(s.id)}" aria-current="${s.id===state.server?'page':'false'}"><div class="flex server-title">${icon('server')}<span class="server-name">${e(s.name)}</span><span class="server-badges">${s.warnings.length?`<span class="count-badge warn" data-warning-server="${e(s.id)}" aria-label="${s.warnings.length} warnings">${icon('warning')}${s.warnings.length}</span>`:''}${s.updates?`<span class="count-badge update" title="${s.updates} updates">${icon('download')}${s.updates}</span>`:''}</span></div>${serverConnection(s,true)}<div class="server-mini">${s.metrics?sidebarMetrics(s):`<span>${s.error?'Connection failed':'Connecting…'}</span>`}</div></button>`).join('')||`<div class="server-filter-empty">${state.data.servers.length?'No servers match these filters.':'No servers yet.'}</div>`;
}
function updateServerFilters(){
  hideWarningTooltip();
  const servers=filteredServers(),active=Object.values(state.filters).some(v=>v!=='all');
  $('.server-list').innerHTML=serverCards(servers);
  $('#server-count').textContent=(active?servers.length+' / ':'')+state.data.servers.length;
  for(const [key,value] of Object.entries(state.filters)){
    updateSlidingControl($('#server-filter-'+key),value);
  }
}
function renderSidebar(){
  hideWarningTooltip();
  const active=Object.values(state.filters).some(v=>v!=='all'),servers=filteredServers();
  $('#sidebar').innerHTML=`<div class="brand"><img src="/static/favicon.svg" alt="">Harbour <span>SELF HOSTED</span>${button('close-mobile','','close','ghost icon-button mobile-sidebar-close','aria-label="Close server list"')}</div>
    <div class="sidebar-heading between"><span class="eyebrow">Your servers</span><span class="count" id="server-count" aria-live="polite">${active?servers.length+' / ':''}${state.data.servers.length}</span></div>
    <div class="server-filters">${filterControl('warnings','Warnings',[['with','With warnings'],['without','No warnings']])}${filterControl('status','Status',[['up','Up'],['down','Down'],['paused','Paused']])}${filterControl('updates','Updates',[['with','Available'],['without','None']])}</div>
    <nav class="server-list" aria-label="Servers">${serverCards(servers)}</nav>
    <div class="sidebar-bottom"><details class="sidebar-menu" id="sidebar-menu" ${state.menuOpen?'open':''}><summary>${icon('menu')}<span>Menu</span>${icon('down','menu-chevron')}</summary><nav aria-label="Workspace menu">${admin()?button('onboard','Add server','plus','ghost sidebar-link'):''}${button('visuals','Visuals','sun','ghost sidebar-link')}${admin()?button('global-settings','Global settings','settings','ghost sidebar-link')+button('users','Manage users','users','ghost sidebar-link')+button('security','Security & sign-ins','shield','ghost sidebar-link'):''}${button('account','My account','users','ghost sidebar-link')}<div class="connection-note"><span class="dot"></span><span id="connection-status">${state.demo?'Demo workspace':'Background monitoring active'}</span></div></nav></details>
    <div class="sidebar-footer"><span class="app-version">Harbour v${e(state.version)}</span><span class="footer-account" title="${e(state.user.name)} · ${admin()?'Administrator':'Read-only user'}">${e(state.user.name)}</span>${iconButton('logout','Sign out','logout')}</div></div><div class="resize-handle" role="separator" aria-label="Resize server pane" aria-orientation="vertical" aria-valuemin="18" aria-valuemax="40" aria-valuenow="${Math.round(parseFloat(document.documentElement.style.getPropertyValue('--sidebar'))||23)}" tabindex="0"></div>`;
}
function visuals(){
  const densities=[['compact','Compact'],['normal','Normal'],['comfortable','Comfortable']];
  modal('Visuals',`<div class="stack visuals-settings"><div class="visual-choice"><h3>${fieldCaption('Colour theme','System follows your device’s light / dark setting. The toolbar theme button selects a manual theme.')}</h3>${slidingControl('visual-theme','Colour theme',[['system','System'],['light','Light'],['dark','Dark']],followSystem?'system':document.documentElement.dataset.theme,'visual-choice','data-visual="theme"')}</div><div class="visual-choice"><h3>${fieldCaption('Server card density','Adjust spacing in the server list.')}</h3>${slidingControl('card-density','Server card density',densities,document.documentElement.dataset.density,'visual-choice','data-visual="density"')}</div><div class="visual-choice"><h3>${fieldCaption('Resource card density','Compact uses smaller cards and charts. Comfortable gives each resource more space.')}</h3>${slidingControl('resource-density','Resource card density',densities,document.documentElement.dataset.resourceDensity,'visual-choice','data-visual="resourceDensity"')}</div><p class="hint">Appearance preferences are saved in this browser.</p></div>`);
}
function renderTop(){
  const warns=state.data.servers.reduce((a,s)=>a+s.warnings.length,0), updates=state.data.servers.reduce((a,s)=>a+s.updates,0);
  $('#topbar').innerHTML=`<div class="breadcrumbs">${button('mobile','','menu','ghost icon-button mobile-menu',`aria-label="Toggle server list" aria-controls="sidebar" aria-expanded="${state.mobile}"`)}<span>Workspace</span><span class="subtle-divider">/</span><span class="current">${e(current()?.name||'Servers')}</span></div><div class="top-actions">${button('warnings',`<span class="chip-label">Warnings</span><b>${warns}</b>`,'warning','notification-chip warn')}${button('updates',`<span class="chip-label">Updates</span><b>${updates}</b>`,'download','notification-chip update')}<span class="divider"></span>${iconButton('theme','Switch colour theme',document.documentElement.dataset.theme==='dark'?'sun':'moon')}${iconButton('notifications','Open notifications','bell')}</div>`;
}
function metricCard(label,ico,value,unit,detail,aside,pct,warning=false,extra=''){
  return `<article class="metric ${warning?'warning':''}"><div class="metric-label">${icon(ico)}${label}<span class="tag ${warning?'red':''}">${warning?'Attention':ico==='cpu'?'Live':ico==='memory'?'RAM':'Storage'}</span></div><div class="metric-number-row"><div class="metric-value">${value}<span>${unit}</span></div>${extra}</div><div class="meter ${warning?'warn':ico==='memory'?'memory':''}"><span style="width:${Math.max(0,Math.min(100,pct))}%"></span></div><div class="metric-bottom"><span>${detail}</span><span>${aside}</span></div></article>`;
}
function renderMain(){
  if(!$('#main'))return;
  const s=current();
  if(!s){$('#main').innerHTML=`<div class="no-servers empty">${icon('server')}<h1>Your fleet starts here</h1><p>Add your first Linux server to see its resources and Docker services.</p>${admin()?button('onboard','Add your first server','plus','primary'):''}</div>`;return;}
  const m=s.metrics;
  const focused=$('#service-search')===document.activeElement;const cursor=focused?$('#service-search').selectionStart:null;
  $('#main').innerHTML=`${serverWarnings(s)}${state.demo?`<div class="demo-banner">${icon('info')}<span>Demo workspace · sample servers, simulated actions.</span><span>No live connections</span></div>`:''}
    <section class="server-header"><div class="server-heading"><div class="server-emblem">${icon('server')}</div><div><h1>${e(s.name)}</h1><div class="server-meta">${serverConnection(s)}<span class="subtle-divider">·</span><span class="mono">${e(s.host)}</span></div></div></div><div class="header-actions">${admin()?button('refresh','<span class="action-label">Refresh</span>','refresh','',`aria-label="Refresh server"`)+button('server-settings','<span class="action-label">Settings</span>','settings','',`aria-label="Server settings"`):''}</div></section>
    ${m?`<div class="host-facts"><span><small>Operating system</small><b>${e(m.os||'Unavailable')}</b></span><span><small>Kernel</small><b class="mono">${e(m.kernel||'Pending next check')}</b></span><span><small>Uptime at last check</small><b>${uptime(m.uptime)}</b></span><span><small>Server timezone</small><b>${timezoneLabel(m.timezone)}</b></span></div>
    <div class="resource-heading"><h2>Resources</h2><div class="flex"><label>Recent history <select id="card-hours" aria-label="Resource card history hours">${hourOptions(cardHours)}</select></label>${button('history','Explore history','activity','small')}</div></div><section class="metrics" aria-label="Server resource usage">${resourceCards(s)}</section>`:''}
    <nav class="tabs" aria-label="Server views">${['containers','storage','activity'].map(tab=>button('tab',tab[0].toUpperCase()+tab.slice(1)+(tab==='containers'?` <span class="tag">${s.services?.length||0}</span>`:''),'',state.tab===tab?'active':'',`data-tab="${tab}"`)).join('')}<span class="tab-meta">Updated ${age(s.checked)}</span></nav><div id="server-view">${state.tab==='containers'?renderContainers(s):state.tab==='storage'?renderStorage(s):renderActivity(s)}</div>
    ${m?`<footer class="server-footer"><span>${icon('box')}Docker ${e(m.docker)}</span><span>${icon('shield')}${state.demo?'Simulated SSH':'Pinned SSH'} · port ${s.port}</span><span>Poll every ${s.poll_seconds}s${!s.monitoring_enabled?' · paused':''}</span><span>${s.override?'Custom':'Global'} thresholds</span></footer>`:''}`;
  if(m)loadCardHistory(s);
  if(focused){$('#service-search')?.focus();$('#service-search')?.setSelectionRange(cursor,cursor);}
}
function updateTag(s){const u=s.update||{};return u.status==='available'?`<span class="tag ${s.dismissed?'':'blue'}">${s.dismissed?'Dismissed':icon('download')+' Update'}</span>`:u.status==='current'?'<span class="tag green">Current</span>':`<span class="tag" title="${e(u.error||'Checks the configured image tag, not newer version tags')}">${u.status==='pinned'?'Pinned':u.status==='unknown'?'Check failed':'Not checked'}</span>`;}
function renderContainers(s){
  const services=(s.services||[]), q=state.search.toLowerCase();
  const filtered=services.filter(c=>[c.name,c.project,c.image].some(v=>String(v||'').toLowerCase().includes(q)));
  const groups=new Map();const standalone=[];
  for(const c of filtered){if(c.project){if(!groups.has(c.project))groups.set(c.project,[]);groups.get(c.project).push(c);}else standalone.push(c);}
  return `<div class="toolbar"><label class="search-box">${icon('search')}<input id="service-search" type="search" placeholder="Filter containers…" aria-label="Filter containers" value="${e(state.search)}"></label><div class="toolbar-actions">${admin()?`<span class="selected-count">${state.selected.size?count(state.selected.size,'selected target'):''}</span>${button('bulk','Pull','download','small',`data-kind="pull" ${!state.selected.size?'disabled':''}`)}${button('bulk','Apply','play','small',`data-kind="up" ${!state.selected.size?'disabled':''}`)}${button('bulk','Restart','refresh','small',`data-kind="restart" ${!state.selected.size?'disabled':''}`)}${button('check-updates','Check updates','download','small')}`:''}</div></div><div class="service-table"><div class="table-heading"><span>${admin()?`<input type="checkbox" aria-label="Select all visible groups and standalone containers" id="select-all" ${filtered.length&&[...groups.keys()].every(g=>state.selected.has('group:'+g))&&standalone.every(c=>state.selected.has(c.id))?'checked':''}>`:''}</span><span>Stack / container</span><span>Services</span><span>Updates</span></div>${[...groups].map(([name,members])=>{const n=members.filter(c=>c.update?.status==='available'&&!c.dismissed).length;const key='group:'+name;return `<details class="group" data-open="${e(key)}" ${state.open.has(key)?'open':''}><summary class="group-summary"><span class="flex">${admin()?`<input type="checkbox" data-select="${e(key)}" aria-label="Select ${e(name)} stack" ${state.selected.has(key)?'checked':''}>`:''}${icon('chevron','chevron')}</span><div class="group-info"><div class="group-icon">${icon('layers')}</div><div><div class="group-name">${e(name)}<span class="tag">COMPOSE</span></div><div class="group-path mono">${e(members[0].working_dir||'Compose path unavailable')}</div></div></div><span class="muted">${count(members.length,'service')}</span><span class="tag ${n?'blue':''}">${n?icon('download')+' '+count(n,'update'):members.some(c=>['unknown','unchecked'].includes(c.update?.status))?'Not verified':'No updates'}</span></summary>${members.map(c=>renderService(c)).join('')}</details>`;}).join('')}${standalone.map(c=>renderService(c,true)).join('')}${!filtered.length?`<div class="empty">${icon('box')}<h3>${q?'No matching containers':'No containers discovered'}</h3><p>${q?'Try a different name, image or stack.':'Refresh the server to check Docker.'}</p></div>`:''}</div><div class="table-footer"><span>${count(services.filter(c=>c.state==='running').length,'running container')} · ${count([...new Set(services.filter(c=>c.project).map(c=>c.project))].length,'Compose stack')}</span><span>Image checks: ${age(s.update_checked)} · configured tags only</span></div>`;
}
function renderService(c,standalone=false){
  const active=c.state==='running';const key='service:'+c.id;
  return `<details class="service ${standalone?'standalone':''}" data-open="${e(key)}" ${state.open.has(key)?'open':''}><summary class="service-summary"><span class="flex">${admin()?`<input type="checkbox" data-select="${e(c.id)}" aria-label="Select ${e(c.name)} service" ${state.selected.has(c.id)?'checked':''}>`:''}${icon('chevron','chevron')}</span>${standalone?`<div class="group-icon">${icon('box')}</div>`:''}<div class="service-title"><b>${e(c.name)}</b><small>${e(c.image)}</small></div><span class="version">${e(c.version||'Version unlabelled')}</span><span class="state ${active?'':'stopped'}"><span class="dot"></span>${e(c.state)}</span>${updateTag(c)}</summary><div class="service-detail">${c.update?.status==='available'?`<div class="update-note">${icon('download')}<span>A new image is available for this tag.${c.dismissed?' Notification dismissed.':''}</span>${!c.dismissed?button('dismiss','Dismiss','','ghost small',`data-service="${e(c.id)}" data-server="${e(current().id)}"`):''}</div>`:''}${c.update?.error?`<div class="info-box"><p>Update check failed: ${e(c.update.error)}</p></div>`:''}<div class="details-grid"><div><div class="detail-label">Running version</div><div class="detail-value">${e(c.version||'Not declared by image')} <span class="muted">${c.version?'(image label)':''}</span></div></div><div><div class="detail-label">Container</div><div class="detail-value mono">${e(c.container)}</div></div><div><div class="detail-label">Running image ID</div><div class="detail-value mono">${e(c.image_id)}</div></div><div><div class="detail-label">Health / restart policy</div><div class="detail-value">${e(c.health||'No health check')} · ${e(c.restart_policy||'none')}</div></div><div><div class="detail-label">Published ports</div><div class="detail-value mono">${c.ports.length?c.ports.map(e).join('<br>'):'None'}</div></div><div><div class="detail-label">Mounts</div><div class="detail-value mono">${c.mounts.length?c.mounts.map(m=>`${e(m.destination)} · ${e(m.type)} · ${m.rw?'read/write':'read-only'}`).join('<br>'):'None'}</div></div></div>${admin()?`<div class="detail-actions">${button('service-action','Pull image','download','small',`data-kind="pull" data-service="${e(c.id)}"`)}${c.project?button('service-action','Apply','play','small',`data-kind="up" data-service="${e(c.id)}"`):''}${button('service-action','Restart','refresh','small',`data-kind="restart" data-service="${e(c.id)}"`)}</div>`:''}</div></details>`;
}
function renderStorage(s){return `<p class="muted">Each filesystem is checked against both the usage and free-space thresholds. Capacities use decimal GB.</p><div class="disk-list">${s.metrics?.disks.map(d=>{const warn=d.percent>=s.thresholds.disk||d.free/1e9<=s.thresholds.disk_free_gb;return `<article class="disk-row"><div class="between"><div class="flex">${icon('disk')}<b class="mono">${e(d.mount)}</b>${warn?'<span class="tag red">Low space</span>':''}</div><b>${percent(d.percent)}%</b></div><div class="meter ${warn?'warn':''}"><span style="width:${Math.min(100,d.percent)}%"></span></div><div class="metric-bottom"><span>${gb(d.used)} GB used of ${gb(d.total)} GB</span><span><b>${gb(d.free)} GB free</b></span></div></article>`;}).join('')||'<div class="empty">No filesystem data yet.</div>'}</div><p class="hint">Warn at ${s.thresholds.disk}% used or ${s.thresholds.disk_free_gb} GB free. ${s.override?'This server uses custom thresholds.':'This server inherits global thresholds.'}</p>`;}
function renderActivity(s){const jobs=state.data.jobs.filter(j=>j.server_id===s.id);return jobs.length?jobs.map(j=>`<article class="activity-row"><div class="activity-icon">${icon(j.action==='pull'?'download':'activity')}</div><div class="activity-text"><b>${e(actionLabel(j.action))}</b><small>${e(j.actor)} · ${age(j.created)}</small></div><span class="tag ${j.status==='succeeded'?'green':j.status==='failed'?'red':''}">${e(j.status)}</span>${admin()?button('job','Details','','ghost small',`data-id="${e(j.id)}"`):''}</article>`).join(''):`<div class="empty">${icon('activity')}<h3>Nothing to report yet</h3><p>Server refreshes and Docker actions will appear here.</p></div>`;}

let previousFocus=null;
function modal(title,content,wide=false){
  if(state.mobile)setMobilePanel(false);
  if(!state.modal)previousFocus=document.activeElement;
  state.modal=title;state.notices=null;
  $('#overlay').innerHTML=`<div class="modal-backdrop"><section class="modal ${wide?'wide':''}" role="dialog" aria-modal="true" aria-labelledby="modal-title"><header class="modal-header"><h2 id="modal-title">${title}</h2>${iconButton('close','Close dialog','close')}</header>${content}</section></div>`;
  $('input,button,select,textarea',$('.modal'))?.focus();
}
function closeOverlay(){hideHelpTooltip();state.modal=null;state.notices=null;$('#overlay').innerHTML='';previousFocus?.focus();}
const formError = message => {const el=$('.form-error',$('#overlay'))||$('.form-error');if(el)el.textContent=message;else toast(message,true);};
function thresholdFields(t){return `<div class="form-grid"><label>CPU warning (%)<input name="cpu" type="number" min="1" max="100" step="0.1" value="${t.cpu}" required></label><label>Memory warning (%)<input name="memory" type="number" min="1" max="100" step="0.1" value="${t.memory}" required></label><label>Disk warning (%)<input name="disk" type="number" min="1" max="100" step="0.1" value="${t.disk}" required></label><label>Disk free-space warning (GB)<input name="disk_free_gb" type="number" min="0" max="1000000" step="0.1" value="${t.disk_free_gb}" required></label><label>Temperature warning (°C)<input name="temperature" type="number" min="1" max="180" step="0.1" value="${t.temperature??80}" required></label></div>`;}
function settings(server=false){const s=current();modal(server?`${e(s.name)} settings`:'Global settings',`${server?`<form id="rename-form" data-id="${e(s.id)}"><label>Server name<div class="flex"><input name="name" value="${e(s.name)}" maxlength="80" required><button type="submit">Rename</button></div></label><div class="form-error" role="alert"></div></form><div class="connection-edit-action">${button('edit-connection','Edit SSH connection','key','small')}${help('SSH connection','Change the host, SSH account, authentication method or key without deleting this server or its history.')}</div><hr class="section-rule">`:''}<form id="threshold-form" data-server="${server?e(s.id):''}"><h3>${fieldCaption('Warning thresholds',server?'Set an override for this server, or inherit the global thresholds.':'Default warning thresholds for every server without an override.')}</h3>${thresholdFields(server?s.thresholds:state.data.thresholds)}<p class="hint">Disk warnings trigger when either limit is reached, on any monitored filesystem. CPU, memory and temperature warnings use the latest sample. CPU warnings use package sensors; other device sensors warn separately; choose a limit suitable for your hardware.</p><div class="form-error" role="alert"></div><div class="form-actions">${server?button('inherit','Use global thresholds','','ghost'):''}<button class="primary" type="submit">Save thresholds</button></div></form>${server?`${serverMonitoringForm(s)}<hr class="section-rule"><div class="between"><div><h3>${fieldCaption('Remove this server','Removes it from Harbour. Containers stay on the host.')}</h3></div>${button('remove-server','Remove','trash','danger small')}</div>`:`<hr class="section-rule"><h3>${fieldCaption('Monitoring & history','Polling intervals, staged resolution and data retention.')}</h3>${button('monitoring','Configure monitoring','activity','small')}<hr class="section-rule"><h3>${fieldCaption('Update notifications','Dismissals are personal and tied to a specific image digest. A different image will notify you again.')}</h3>${button('restore-dismissals','Restore my dismissed updates','bell','small')}`}`);}
function onboard(){
  state.connection=null;state.key=null;state.keyMode='generate';state.authMethod='key';state.hostProbe=null;state.acceptedHost=null;
  renderOnboard();
}
async function editConnection(){
  const connection=await api('/servers/'+current().id+'/connection');
  state.connection=connection;state.key=connection.key;state.keyMode='generate';state.authMethod=connection.auth_method;
  state.hostProbe=null;state.acceptedHost={host:connection.host,port:connection.port};renderOnboard();
}
function renderOnboard(){
  const connection=state.connection,passwordMode=state.authMethod==='password';
  modal(connection?'Edit SSH connection':'Add a server',`${state.demo?'<div class="info-box warning"><p>Live SSH connections and credentials are disabled in this demo.</p></div>':''}
    <form id="onboard-form" ${connection?`data-server="${e(connection.id)}"`:''}>
      <div class="form-grid">
        <label>Display name<input name="name" placeholder="Atlas" value="${e(connection?.name||'')}" required maxlength="80"></label>
        <label>${fieldCaption('Hostname or IP','Connect a Linux host with Docker, Compose v2 and Python 3 installed.')}<input name="host" aria-label="Hostname or IP" placeholder="192.0.2.10" value="${e(connection?.host||'')}" required></label>
        <label>SSH user<input name="username" placeholder="harbour" value="${e(connection?.username||'harbour')}" required pattern="[a-z_][a-z0-9_-]{0,63}"></label>
        <label>SSH port<input name="port" type="number" value="${connection?.port||22}" min="1" max="65535" required></label>
        <label class="full">${fieldCaption('Host fingerprint','Get fingerprint reads the server’s host key without logging in. Compare it through a trusted connection, or explicitly accept it on first connection. For an Ed25519 host key, the server-console command is: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256. Later fingerprint changes are rejected.')}<div class="fingerprint-input"><input name="fingerprint" aria-label="Host fingerprint" class="mono" value="${e(connection?.fingerprint||'')}" placeholder="SHA256:…" required pattern="SHA256:[A-Za-z0-9+/]{43}">${button('probe-fingerprint','Get fingerprint','search','',state.demo?'disabled':'')}</div></label>
      </div>
      <div id="fingerprint-result" aria-live="polite"></div>
      <hr class="section-rule">
      <label class="check-line"><input id="password-auth-toggle" type="checkbox" ${passwordMode?'checked':''}>Use password login</label>
      <div id="password-auth-warning" class="info-box warning" hidden><p>Password login requires storing your reusable server password; a dedicated SSH key is safer and easier to revoke.</p><div class="flex">${button('confirm-password-auth','Go ahead','','small')}${button('cancel-password-auth','Keep SSH key','','ghost small')}</div></div>
      <div id="password-auth-fields" ${passwordMode?'':'hidden'}><label>${fieldCaption('SSH password','Saved encrypted on Harbour for background monitoring. It is never shown again. Leave blank to keep an existing password; enter it again if the host or SSH user changes.')}<input name="ssh_password" type="password" aria-label="SSH password" autocomplete="off" maxlength="1024" placeholder="${connection?.has_password?'Leave blank to keep the saved password':'Server account password'}" ${passwordMode?'':'disabled'}></label></div>
      <div id="ssh-key-section" ${passwordMode?'hidden':''}>
        <h3>${fieldCaption('SSH key','Generate a dedicated key or import one. Private keys stay encrypted on Harbour. Generate a replacement to rotate this server’s key.')}</h3>
        <div class="segmented">${button('key-mode','Generate dedicated key','','active',`data-mode="generate"`)}${button('key-mode','Import existing key','','',`data-mode="import"`)}</div>
        <div id="key-fields">${keyFields()}</div><div id="key-output">${state.key?keyOutput():''}</div>
      </div>
      <p class="hint onboarding-footnote">Use a dedicated SSH account. Standard Docker access gives effective root control of the host. Keep Harbour private and use HTTPS.${connection?' Replacing a key here does not remove old public keys from the server.':''}</p>
      <div class="form-error" role="alert"></div><div class="form-actions">${button('close','Cancel','','ghost')}<button type="submit" class="primary" ${state.demo?'disabled':''}>${icon(connection?'check':'plus')}${connection?'Save connection':'Connect server'}</button></div>
    </form>`,true);
  updatePasswordRequirement();
}
async function preview(action,targets){
  const s=current();const plan=await api(`/servers/${s.id}/plan`,'POST',{action,targets});
  const description={pull:'Download images for the selected targets. Running containers keep their existing images until you apply them.',up:'Run docker compose up -d. Containers may be recreated and briefly unavailable. Compose dependencies are skipped for individual services.',restart:'Restart the selected containers. Services will be briefly unavailable. Restarting does not apply newly pulled images.'}[action];
  modal(`${actionLabel(action)} on ${e(s.name)}`,`<p class="operation-description">${description}</p>${state.demo?'<p>This operation is simulated in the demo.</p>':''}${plan.commands.map(c=>`<div class="info-box"><h3>${e(c.label)}</h3><code class="code-block">${e(c.command)}</code>${c.directory?`<p class="hint">Working directory: ${e(c.directory)}</p>`:''}</div>`).join('')}<div class="form-error" role="alert"></div><div class="form-actions">${button('close','Cancel','','ghost')}${button('execute',`Confirm ${action==='up'?'apply':action}`,'',action==='restart'?'danger':'primary',`data-server="${e(s.id)}"`)}</div>`,true);
  state.plan=plan;
}
async function userManagement(){const users=await api('/users');modal('Manage users',`<p>Administrators manage hosts and Docker. Users can view every server and dismiss their own update notices.</p><div>${users.map(u=>`<div class="user-row"><div class="avatar">${e(u.name.slice(0,2).toUpperCase())}</div><b>${e(u.name)}</b><span class="tag ${u.mfa_enabled?'green':''}">2FA ${u.mfa_enabled?'on':'off'}</span><span class="tag ${u.role==='admin'?'green':''}">${e(u.role)}</span>${u.id!==state.user.id?iconButton('remove-user','Remove '+u.name,'trash',`data-id="${e(u.id)}" data-name="${e(u.name)}"`):'<span class="tag">You</span>'}</div>`).join('')}</div><hr class="section-rule"><h3>Add a user</h3><form id="user-form"><div class="form-grid"><label>Username<input name="name" required maxlength="80" autocomplete="off"></label><label>Access level<select name="role"><option value="user">User · read only</option><option value="admin">Administrator</option></select></label><label class="full">Password<input name="password" type="password" minlength="12" required autocomplete="new-password" placeholder="At least 12 characters"></label></div><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary" type="submit">Add user</button></div></form>`);}
function renderNotices(){
  const kind=state.notices;const notices=[];
  for(const s of state.data.servers){if(kind==='warnings'||kind==='all')for(const w of s.warnings)notices.push({kind:'warn',server:s,title:w.title,detail:w.detail});if(kind==='updates'||kind==='all')for(const c of s.services||[])if(c.update?.status==='available'&&!c.dismissed)notices.push({kind:'update',server:s,service:c,title:c.name+' · image update',detail:c.image});}
  $('#overlay').innerHTML=`<div class="drawer-backdrop"><section class="drawer" role="dialog" aria-modal="true" aria-labelledby="notice-title"><header class="modal-header"><h2 id="notice-title">Notifications</h2>${iconButton('close','Close notifications','close')}</header><div class="tabs">${['all','warnings','updates'].map(k=>button('notice-tab',k[0].toUpperCase()+k.slice(1),'',kind===k?'active':'',`data-kind="${k}"`)).join('')}</div>${notices.map(n=>`<article class="notice ${n.kind}"><div class="notice-title">${icon(n.kind==='warn'?'warning':'download')}${e(n.title)}</div><p>${e(n.detail)}</p><div class="notice-actions">${button('notice-server',e(n.server.name),'server','text-button',`data-server="${e(n.server.id)}"`)}${n.service?button('dismiss','Dismiss','','ghost small',`data-server="${e(n.server.id)}" data-service="${e(n.service.id)}"`):'<span class="muted">Active warning</span>'}</div></article>`).join('')||`<div class="empty">${icon('check')}<h3>All clear</h3><p>No ${kind==='all'?'notifications':kind} to show.</p></div>`}<p class="hint" style="margin-top:20px">Dismissed updates stay hidden for you until a different image is available. Active warnings clear when the issue resolves.</p>${button('restore-dismissals','Restore dismissed updates','','text-button')}</section></div>`;
}

document.addEventListener('click',async event=>{
  if(event.target.matches('input[type=checkbox]')){event.stopPropagation();return;}
  if(event.target.classList.contains('modal-backdrop')||event.target.classList.contains('drawer-backdrop')){closeOverlay();return;}
  const el=event.target.closest('[data-action]');if(!el)return;
  const a=el.dataset.action;if(el.disabled)return;
  try {
    if(a==='help')return showHelpTooltip(el);
    if(a==='close')return closeOverlay();
    if(a==='theme'){followSystem=false;localStorage.setItem('harbour-theme-mode','manual');localStorage.setItem('harbour-theme',document.documentElement.dataset.theme==='dark'?'light':'dark');return applyTheme();}
    if(a==='visuals')return visuals();
    if(a==='server-filter'){const key=el.dataset.filter;state.filters[key]=state.filters[key]===el.dataset.value?'all':el.dataset.value;updateServerFilters();return;}
    if(a==='visual-choice'){
      const key=el.dataset.visual,value=el.dataset.value;
      if(key==='theme'){followSystem=value==='system';localStorage.setItem('harbour-theme-mode',followSystem?'system':'manual');if(!followSystem)localStorage.setItem('harbour-theme',value);applyTheme();}
      else{document.documentElement.dataset[key]=value;localStorage.setItem(key==='density'?'harbour-density':'harbour-resource-density',value);updateSlidingControl(el.closest('.sliding-control'),value);}
      return;
    }
    if(a==='mobile')return setMobilePanel(!state.mobile);
    if(a==='close-mobile')return setMobilePanel(false);
    if(a==='select-server'||a==='notice-server'){state.server=el.dataset.id||el.dataset.server;localStorage.setItem('harbour-server',state.server);state.selected.clear();state.search='';state.mobile=false;closeOverlay();renderShell();return;}
    if(a==='tab'){state.tab=el.dataset.tab;renderMain();return;}
    if(['warnings','updates','notifications','notice-tab'].includes(a)){if(!state.notices)previousFocus=document.activeElement;state.modal=null;state.notices=a==='notifications'?'all':a==='notice-tab'?el.dataset.kind:a;renderNotices();$('.drawer button')?.focus();return;}
    if(a==='global-settings'||a==='server-settings')return settings(a==='server-settings');
    if(a==='onboard')return onboard();
    if(a==='edit-connection')return await editConnection();
    if(a==='confirm-password-auth')return confirmPasswordAuth();
    if(a==='cancel-password-auth')return setPasswordChoice(false);
    if(a==='accept-fingerprint')return acceptFingerprint();
    if(a==='account')return await account();
    if(a==='key-mode'){state.keyMode=el.dataset.mode;for(const b of document.querySelectorAll('[data-action=key-mode]'))b.classList.toggle('active',b.dataset.mode===state.keyMode);$('#key-fields').innerHTML=keyFields();return;}
    if(a==='copy-key'){await navigator.clipboard.writeText('restrict '+state.key.public_key);toast('Public key copied');return;}
    el.disabled=true;
    if(monitorActions.has(a)){await handleMonitorAction(a,el);return;}
    if(securityActions.has(a)){await handleSecurityAction(a,el);return;}
    if(a==='demo-login'){state.user=await api('/demo-login','POST');await load(true);}
    else if(a==='logout'){await api('/logout','POST');state.user=null;renderLogin();}
    else if(a==='refresh'||a==='check-updates'){const r=await api(`/servers/${current().id}/${a==='refresh'?'refresh':'check-updates'}`,'POST');state.lastJobs.set(r.id,'queued');toast(a==='refresh'?'Refreshing server readings…':'Checking configured image tags…');await load();}
    else if(a==='dismiss'){await api('/dismiss','POST',{server_id:el.dataset.server,service_id:el.dataset.service});await load();toast('Update dismissed for your account');}
    else if(a==='restore-dismissals'){await api('/dismissals','DELETE');await load();toast('Dismissed updates restored');}
    else if(a==='inherit'){await api(`/servers/${current().id}/thresholds`,'PUT',null);closeOverlay();await load();toast('Global thresholds restored');}
    else if(a==='probe-fingerprint')await probeFingerprint();
    else if(a==='create-key')await createKey();
    else if(a==='install-key')await installOnboardKey();
    else if(a==='bulk')await preview(el.dataset.kind,[...state.selected]);
    else if(a==='service-action')await preview(el.dataset.kind,[el.dataset.service]);
    else if(a==='execute'){const r=await api(`/servers/${el.dataset.server}/execute`,'POST',{token:state.plan.token});state.lastJobs.set(r.id,'queued');closeOverlay();state.tab='activity';await load();toast('Operation queued. Results will appear in Activity.');}
    else if(a==='users')await userManagement();
    else if(a==='remove-user'){modal('Remove user',`<p>Remove <b>${e(el.dataset.name)}</b> and revoke their active sessions?</p><div class="form-error" role="alert"></div><div class="form-actions">${button('close','Cancel','','ghost')}${button('confirm-remove-user','Remove user','trash','danger',`data-id="${e(el.dataset.id)}"`)}</div>`);}
    else if(a==='confirm-remove-user'){await api('/users/'+el.dataset.id,'DELETE');await userManagement();toast('User removed');}
    else if(a==='remove-server'){modal('Remove server',`<p>Remove <b>${e(current().name)}</b> from Harbour? This removes its monitoring and stored credential when unused. It does not stop containers or revoke the public key on the host.</p><div class="form-error" role="alert"></div><div class="form-actions">${button('close','Cancel','','ghost')}${button('confirm-remove-server','Remove server','trash','danger',`data-id="${e(current().id)}"`)}</div>`);}
    else if(a==='confirm-remove-server'){await api('/servers/'+el.dataset.id,'DELETE');closeOverlay();await load();toast('Server removed from Harbour');}
    else if(a==='job'){const j=await api('/jobs/'+el.dataset.id);modal('Operation details',`<div class="between"><h3>${e(actionLabel(j.action))} · ${e(j.server_name)}</h3><span class="tag">${e(j.status)}</span></div><p>${e(j.actor)} · ${new Date(j.created*1000).toLocaleString()}</p><code class="code-block">${e(j.output||'Operation is still running. Close and reopen to refresh the output.')}</code>`,true);}
  } catch(err){if(state.modal)formError(err.message);else toast(err.message,true);}
  finally{if(el.isConnected)el.disabled=false;}
});
document.addEventListener('submit',async event=>{
  event.preventDefault();const form=event.target;if(form.dataset.busy)return;const data=Object.fromEntries(new FormData(form));const submit=$('[type=submit]',form);submit.disabled=true;$('.form-error',form).textContent='';
  try{
    if(monitorForms.has(form.id)){await handleMonitorForm(form,data);return;}
    if(securityForms.has(form.id)){await handleSecurityForm(form,data);return;}
    if(form.id==='login-form'){state.user=await api('/login','POST',data);state.authMessage='';await load(true);}
    else if(form.id==='rename-form'){await api('/servers/'+form.dataset.id,'PATCH',data);await load();settings(true);toast('Server renamed');}
    else if(form.id==='threshold-form'){const values=Object.fromEntries(Object.entries(data).map(([k,v])=>[k,Number(v)]));await api(form.dataset.server?`/servers/${form.dataset.server}/thresholds`:'/thresholds','PUT',values);closeOverlay();await load();toast('Thresholds saved');}
    else if(form.id==='onboard-form'){
      if($('#password-auth-toggle').checked&&state.authMethod!=='password')throw new Error('Acknowledge the password-login warning, or keep SSH key authentication.');
      if(state.authMethod==='key'&&!state.key)throw new Error('Generate or import an SSH key first.');
      const body={name:data.name,host:data.host.trim(),port:Number(data.port),username:data.username.trim(),fingerprint:data.fingerprint.trim(),auth_method:state.authMethod};
      if(state.authMethod==='password'){body.password_auth_confirmed=true;if(data.ssh_password)body.password=data.ssh_password;}
      else body.key_id=state.key.id;
      const saved=await api(form.dataset.server?'/servers/'+form.dataset.server+'/connection':'/servers',form.dataset.server?'PUT':'POST',body);
      state.server=saved.id;if(saved.job)state.lastJobs.set(saved.job.id,'queued');closeOverlay();await load();toast('Connection saved. Checking SSH access…');
    }
    else if(form.id==='user-form'){await api('/users','POST',data);await userManagement();toast('User created');}
    else if(form.id==='password-form'){state.user=await api('/password','PUT',data);state.authMessage='';closeOverlay();toast('Password changed; other sessions signed out');}
  }catch(err){const output=$('.form-error',form);if(output)output.textContent=err.message;else formError(err.message);}
  finally{if(submit.isConnected)submit.disabled=false;}
});
document.addEventListener('change',event=>{
  const input=event.target;
  if(input.id==='password-auth-toggle')setPasswordChoice(input.checked);
  if(input.name==='key_algorithm')updateKeyTiers();
  if(input.dataset.select){input.checked?state.selected.add(input.dataset.select):state.selected.delete(input.dataset.select);renderMain();}
  if(input.id==='select-all'){const services=(current().services||[]).filter(c=>[c.name,c.project,c.image].some(v=>String(v||'').toLowerCase().includes(state.search.toLowerCase())));for(const c of services){const key=c.project?'group:'+c.project:c.id;input.checked?state.selected.add(key):state.selected.delete(key);}renderMain();}
});
document.addEventListener('input',event=>{if(event.target.closest('#onboard-form'))onboardIdentityChanged(event.target);if(event.target.id==='service-search'){state.search=event.target.value;renderMain();}});
document.addEventListener('toggle',event=>{if(event.target.id==='sidebar-menu'&&event.target.isConnected){state.menuOpen=event.target.open;localStorage.setItem('harbour-menu-open',String(state.menuOpen));}if(event.target.dataset.open){event.target.open?state.open.add(event.target.dataset.open):state.open.delete(event.target.dataset.open);}},true);
document.addEventListener('keydown',event=>{
  const slider=event.target.closest('.sliding-control');
  if(slider&&['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){
    event.preventDefault();
    const buttons=[...slider.querySelectorAll('button')],index=buttons.indexOf(event.target);
    const next=event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length;
    buttons[next].focus();if(buttons[next].getAttribute('aria-pressed')!=='true')buttons[next].click();return;
  }
  if(event.key==='Escape'&&$('#help-tooltip')){hideHelpTooltip();return;}
  if(event.key==='Escape'){closeOverlay();if(state.mobile)setMobilePanel(false);}
  const dialog=$('[role=dialog]')||(state.mobile?$('#sidebar'):null);
  if(event.key==='Tab'&&dialog){const items=[...dialog.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea,summary,[tabindex="0"]')].filter(el=>el.getClientRects().length&&!el.closest('details:not([open]) nav'));const first=items[0],last=items.at(-1);if(event.shiftKey&&(document.activeElement===first||!dialog.contains(document.activeElement))){event.preventDefault();last?.focus();}else if(!event.shiftKey&&(document.activeElement===last||!dialog.contains(document.activeElement))){event.preventDefault();first?.focus();}}
  if(event.target.classList.contains('resize-handle')&&['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();resize((parseFloat(document.documentElement.style.getPropertyValue('--sidebar'))||23)+(event.key==='ArrowRight'?1:-1));}
});
function resize(value){const w=Math.max(18,Math.min(40,Math.max(220/innerWidth*100,value)));document.documentElement.style.setProperty('--sidebar',w+'%');localStorage.setItem('harbour-width',w);$('.resize-handle')?.setAttribute('aria-valuenow',Math.round(w));}
document.addEventListener('pointerdown',event=>{if(!event.target.classList.contains('resize-handle'))return;event.preventDefault();const el=event.target;el.setPointerCapture(event.pointerId);const move=e=>resize(e.clientX/innerWidth*100);el.addEventListener('pointermove',move);el.addEventListener('pointerup',()=>el.removeEventListener('pointermove',move),{once:true});});
async function start(){try{const config=await api('/config');state.demo=config.demo;state.version=config.version;try{state.user=await api('/me');await load(true);}catch{renderLogin();}}catch(err){$('#app').innerHTML=`<div class="empty"><h1>Harbour is unavailable</h1><p>${e(err.message)}</p></div>`;}}


const mobileLayout=matchMedia('(max-width:800px)');
function syncMobilePanel(){
  const open=state.mobile&&mobileLayout.matches;
  document.body.classList.toggle('mobile-panel-open',open);
  $('#sidebar')?.classList.toggle('mobile-open',open);
  if($('#sidebar'))$('#sidebar').inert=mobileLayout.matches&&!open;
  if($('.workspace'))$('.workspace').inert=open;
  if($('#mobile-sidebar-backdrop'))$('#mobile-sidebar-backdrop').hidden=!open;
  $('[data-action=mobile]')?.setAttribute('aria-expanded',String(open));
}
function setMobilePanel(open){
  state.mobile=Boolean(open&&mobileLayout.matches);syncMobilePanel();hideWarningTooltip();
  (state.mobile?$('.mobile-sidebar-close'):$('[data-action=mobile]'))?.focus({preventScroll:true});
}
mobileLayout.addEventListener('change',()=>{state.mobile=false;syncMobilePanel();});
