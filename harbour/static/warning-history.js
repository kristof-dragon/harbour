'use strict';
const warningHistory={user:null,key:null,data:null,error:'',pending:false,loaded:0,sequence:0,server:'',resource:'',status:'all',ack:'all',search:'',before:null,pages:[],tab:null};
const warningActions=new Set(['warning-ack','warning-page','warning-refresh','warning-occurrences','warning-groups','warning-visibility','warning-hide-all','current-warning']);
const warningTime=value=>value==null?'—':new Date(value*1000).toLocaleString();
const warningNumber=value=>value==null?'—':Number(value).toLocaleString(undefined,{maximumFractionDigits:2});
function warningRequestKey(){
  const params=new URLSearchParams({acknowledgement:state.notices==='history'?warningHistory.ack:'all',visibility:state.notices==='history'?'all':'visible'});
  if(state.notices==='history'){
    if(warningHistory.server)params.set('server_id',warningHistory.server);
    if(warningHistory.resource){params.set('warning_key',warningHistory.resource);params.set('view','occurrences');}
    params.set('status',warningHistory.status);
    if(warningHistory.search)params.set('search',warningHistory.search);
  }
  if(warningHistory.before)params.set('before',warningHistory.before);
  return params.toString();
}
function invalidateWarningHistory(){++warningHistory.sequence;warningHistory.pending=false;warningHistory.loaded=0;}
async function fetchWarningHistory(key){
  const sequence=++warningHistory.sequence,user=state.user;
  warningHistory.pending=true;warningHistory.key=key;
  try{
    const data=await api('/warning-events?'+key);
    if(sequence!==warningHistory.sequence||state.user!==user)return;
    warningHistory.data=data;warningHistory.error='';
  }catch(error){if(sequence!==warningHistory.sequence||state.user!==user)return;warningHistory.error=error.message;}
  finally{
    if(sequence===warningHistory.sequence&&state.user===user){warningHistory.pending=false;warningHistory.loaded=Date.now();if(state.notices)renderNotices();}
  }
}
function warningEventMarkup(event){
  const definition=event.definition,unit=definition.unit||'',extreme=event.extreme_values;
  const repeated=event.occurrence_count>1,occurrences=state.notices==='history'&&warningHistory.resource;
  const status=event.status==='active'?(event.acknowledged_at?'Acknowledged · still present':'Active at last observation'):event.status==='resolved'?'Resolved':'Ended';
  const reason={settings_changed:'Warning settings changed',condition_changed:'Warning condition changed',monitoring_paused:'Monitoring paused',resource_removed:'Resource no longer reported'}[event.close_reason];
  const limits=[definition.low!=null?`Low: ${warningNumber(definition.low)} ${unit}`:'',definition.high!=null?`High: ${warningNumber(definition.high)} ${unit}`:'',definition.free_low!=null?`Free space: ≤ ${warningNumber(definition.free_low)} GB`:''].filter(Boolean).join(' · ');
  const measurements=[extreme.value!=null?`${definition.side==='low'?'Lowest':definition.free_low!=null?'Peak used':'Peak'}: ${warningNumber(extreme.value)} ${unit}`:'',extreme.free_gb!=null?`Lowest free: ${warningNumber(extreme.free_gb)} GB`:''].filter(Boolean).join(' · ');
  return `<article class="notice warning-event" data-key="warning-event:${event.id}"><div class="between"><div class="notice-title">${icon('warning')}${e(event.title)}</div><span class="tag ${event.status==='resolved'?'green':event.status==='active'&&!event.acknowledged_at?'red':''}">${e(status)}</span></div>${repeated?`<p class="warning-recurrence">${button('warning-occurrences',`${count(event.occurrence_count,'occurrence')} · view history`,'','text-button',`data-server="${e(event.server_id)}" data-resource="${e(event.warning_key)}" title="One warning for this server and resource. View the retained occurrences and their original readings and limits."`)}</p>`:''}<p>${e(event.detail)}</p>${measurements?`<p><b>${e(measurements)}</b>${repeated?' <span class="muted">· latest occurrence</span>':''}</p>`:''}<div class="warning-event-times"><span>First detected <time>${e(warningTime(event.first_seen??event.started_at))}</time></span><span>${event.closed_at?'Ended':'Last observed'} <time>${e(warningTime(event.closed_at||event.last_seen))}</time></span></div><details data-key="warning-details:${event.id}"><summary>Details</summary><dl class="warning-event-details">${[
    ...(repeated?[['Latest occurrence began',warningTime(event.started_at)],['Retained recurrences',String(event.occurrence_count-1)]]:[]),
    ['Limits at detection',limits||'Status warning'],['Last abnormal reading',warningTime(event.last_seen)],['Recorded by Harbour',warningTime(event.detected_at)],
    ...(reason?[['End reason',reason]]:[]),...(event.observation_gap?[['Coverage','Readings were interrupted; this is an observed span, not a confirmed continuous duration.']]:[]),
    ...(event.status==='resolved'?[['Recovery','The end time is the first confirmed healthy reading.']]:[]),
    ...(event.hidden_at?[['List visibility','Removed from your list']]:[]),
    ['Acknowledged by you',event.acknowledged_at?warningTime(event.acknowledged_at):'Not yet']
  ].map(([label,value])=>`<dt>${e(label)}</dt><dd>${e(value)}</dd>`).join('')}</dl></details><div class="notice-actions">${button('notice-server',e(event.server_name),'server','text-button',`data-server="${e(event.server_id)}"`)}${!occurrences?button('warning-visibility',event.hidden_at?'Bring back':'Remove from list','','text-button',`data-id="${event.id}" data-hidden="${event.hidden_at?'false':'true'}" title="Changes list visibility only; acknowledgement stays unchanged."`):''}${!occurrences?button('warning-ack',event.acknowledged_at?'Acknowledged':'Acknowledge',event.acknowledged_at?'':'check','small',`data-id="${event.id}" data-scope="${occurrences?'occurrence':'warning'}" data-ack="true" ${event.acknowledged_at?'disabled':''}`):''}</div></article>`;
}
function notificationListActions(kind){
  const warnings=(state.data.warning_events?.unacknowledged||0)+state.data.servers.reduce((sum,s)=>sum+s.warnings.filter(w=>!w.event_id&&!w.dismissed).length,0);
  const updates=state.data.servers.reduce((sum,s)=>sum+s.updates,0);
  return (kind==='all'||kind==='warnings'?`<div class="notification-action-group">${button('dismiss-all','Acknowledge all warnings','check','small',`data-kind="warnings" ${warnings?'':'disabled'}`)}${button('warning-hide-all','Remove warnings from list','','small','title="Hide warning entries without acknowledging them."')}${button('restore-dismissals','Bring back warnings','','text-button','data-kind="warnings" title="Bring removed warnings back to your list without changing their acknowledgement or reactivating indicators."')}</div>`:'')+
    (kind==='all'||kind==='updates'?`<div class="notification-action-group">${button('dismiss-all','Dismiss all updates','check','small',`data-kind="updates" ${updates?'':'disabled'}`)}${button('restore-dismissals','Bring back updates','','text-button','data-kind="updates" title="Bring dismissed updates that are still available back to your list."')}</div>`:'');
}
function renderWarningNotices(){
  if(warningHistory.user!==state.user.id){Object.assign(warningHistory,{user:state.user.id,key:null,data:null,error:'',loaded:0,pending:false,server:'',resource:'',status:'all',ack:'all',search:'',before:null,pages:[],tab:null});++warningHistory.sequence;}
  if(warningHistory.tab!==state.notices){warningHistory.tab=state.notices;warningHistory.before=null;warningHistory.pages=[];}
  const kind=state.notices,history=kind==='history',key=warningRequestKey(),changed=key!==warningHistory.key;
  if(changed){warningHistory.data=null;warningHistory.error='';}
  if(kind!=='updates'&&(changed||!warningHistory.pending&&Date.now()-warningHistory.loaded>5000))fetchWarningHistory(key);
  const data=warningHistory.data,events=kind==='updates'?[]:data?.events||[],fallback=[],updates=[];
  for(const s of state.data.servers){
    if(!history&&kind!=='updates')for(const w of s.warnings)if(!w.event_id&&!w.hidden)fallback.push(`<article class="notice" data-key="current-warning:${e(s.id)}:${e(w.id)}"><div class="notice-title">${icon('warning')}${e(w.title)}</div><p>${e(w.detail)}</p><div class="notice-actions">${button('notice-server',e(s.name),'server','text-button',`data-server="${e(s.id)}"`)}${button('current-warning','Remove from list','','text-button',`data-server="${e(s.id)}" data-warning="${e(w.id)}" data-operation="hide"`)}${button('current-warning',w.dismissed?'Acknowledged':'Acknowledge','check','small',`data-server="${e(s.id)}" data-warning="${e(w.id)}" data-operation="acknowledge" ${w.dismissed?'disabled':''}`)}</div></article>`);
    if(kind==='all'||kind==='updates')for(const service of s.services||[])if(service.update?.status==='available'&&!service.dismissed)updates.push(`<article class="notice update" data-key="update:${e(s.id)}:${e(service.id)}"><div class="notice-title">${icon('download')}${e(service.name)} · image update</div><p>${e(service.image)}</p><div class="notice-actions">${button('notice-server',e(s.name),'server','text-button',`data-server="${e(s.id)}"`)}${button('dismiss','Dismiss','','small',`data-server="${e(s.id)}" data-service="${e(service.id)}"`)}</div></article>`);
  }
  const filters=history?`<form id="warning-history-filter" data-key="warning-filter:${e(warningHistory.resource)}" data-preserve-children class="warning-history-filters"><label>Server<select name="server"><option value="">All servers</option>${state.data.servers.map(s=>`<option value="${e(s.id)}" ${s.id===warningHistory.server?'selected':''}>${e(s.name)}</option>`).join('')}</select></label><label>Status<select name="status">${[['all','All statuses'],['active','Active'],['resolved','Resolved'],['ended','Ended by a change']].map(([v,label])=>`<option value="${v}" ${warningHistory.status===v?'selected':''}>${label}</option>`).join('')}</select></label><label>Acknowledgement<select name="ack">${[['all','All'],['unacknowledged','Unacknowledged'],['acknowledged','Acknowledged']].map(([v,label])=>`<option value="${v}" ${warningHistory.ack===v?'selected':''}>${label}</option>`).join('')}</select></label><label>Search<input name="search" value="${e(warningHistory.search)}" maxlength="200" placeholder="Server, sensor or message"></label><button type="submit" class="small">Apply filters</button></form>`:'';
  updateHTML($('#overlay'),`<div class="drawer-backdrop"><section class="drawer warning-history-drawer" role="dialog" aria-modal="true" aria-labelledby="notice-title"><header class="modal-header"><h2 id="notice-title">Notifications</h2>${iconButton('close','Close notifications','close')}</header><div class="tabs">${[['all','All'],['warnings','Warnings'],['updates','Updates'],['history','History']].map(([v,label])=>button('notice-tab',label,'',kind===v?'active':'',`data-kind="${v}"`)).join('')}</div>${filters}${history&&warningHistory.resource?`<div class="notification-actions">${button('warning-groups','Back to all warnings','','small')}<span>Occurrences of this warning</span></div>`:''}<div class="notification-actions">${notificationListActions(kind)}${button('warning-refresh','Refresh','refresh','small',warningHistory.pending?'disabled':'')}${fieldCaption('','Each server/resource has one warning; repeated episodes appear as occurrences. Readings, limits and status describe the latest occurrence. Occurrence counts cover retained history. Warnings are saved while you are signed out, independently of Telegram. Acknowledgement silences your warning indicators until a new occurrence and keeps the entry in the list. Remove from list only hides the entry; Bring back preserves acknowledgement. Closed events follow monitoring history retention ('+(data?.retention_days||'configured')+' days). History starts when this version is installed; old threshold breaches cannot be reconstructed reliably.')}</div>${warningHistory.error&&kind!=='updates'?`<p class="form-error" role="alert">${e(warningHistory.error)}</p>`:''}<div class="warning-event-list" aria-busy="${warningHistory.pending&&kind!=='updates'}">${events.map(warningEventMarkup).join('')+fallback.join('')+updates.join('')||`<div class="empty"><h3>${warningHistory.pending?'Loading warnings…':history?'No matching warning events':'Nothing new'}</h3></div>`}</div>${kind!=='updates'?`<div class="warning-history-pages">${button('warning-page','Newer','','small',`data-key="warning-page-newer" data-direction="newer" ${warningHistory.pages.length?'':'disabled'}`)}${button('warning-page','Older','','small',`data-key="warning-page-older" data-direction="older" ${!data?.next_before?'disabled':''}`)}</div>`:''}</section></div>`);
}
async function handleWarningAction(action,el){
  if(action==='warning-visibility'||action==='warning-hide-all'||action==='current-warning'){
    if(action==='warning-visibility')await api(`/warning-events/${el.dataset.id}/visibility`,'PUT',{hidden:el.dataset.hidden==='true'});
    else if(action==='warning-hide-all')await api('/hide-warnings','POST');
    else await api('/current-warning','POST',{server_id:el.dataset.server,warning_id:el.dataset.warning,action:el.dataset.operation});
    invalidateWarningHistory();await load();if(state.notices)renderNotices();return;
  }
  if(action==='warning-ack'){
    await api(`/warning-events/${el.dataset.id}/acknowledgement?scope=${el.dataset.scope||'warning'}`,'PUT',{acknowledged:el.dataset.ack==='true'});
    invalidateWarningHistory();await load();if(state.notices)renderNotices();return;
  }
  if(action==='warning-refresh'){invalidateWarningHistory();await load();if(state.notices)renderNotices();return;}
  if(action==='warning-page'){
    if(el.dataset.direction==='older'&&warningHistory.data?.next_before){warningHistory.pages.push(warningHistory.before);warningHistory.before=warningHistory.data.next_before;}
    else if(el.dataset.direction==='newer')warningHistory.before=warningHistory.pages.pop()??null;
  }
  if(action==='warning-occurrences'){
    state.notices='history';Object.assign(warningHistory,{server:el.dataset.server,resource:el.dataset.resource,status:'all',ack:'all',search:'',before:null,pages:[]});
  }
  if(action==='warning-groups')Object.assign(warningHistory,{server:'',resource:'',status:'all',ack:'all',search:'',before:null,pages:[]});
  if(action==='warning-occurrences'||action==='warning-groups')$('#warning-history-filter')?.remove();
  invalidateWarningHistory();renderNotices();
}
document.addEventListener('submit',event=>{
  if(event.target.id!=='warning-history-filter')return;
  event.preventDefault();const data=new FormData(event.target);
  Object.assign(warningHistory,{server:data.get('server'),status:data.get('status'),ack:data.get('ack'),search:data.get('search').trim(),before:null,pages:[]});
  invalidateWarningHistory();renderNotices();
});
