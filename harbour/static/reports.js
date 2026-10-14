'use strict';
const reportViews=new Map();
function reportView(s){if(!reportViews.has(s.id))reportViews.set(s.id,{data:null,loaded:0,busy:false,error:''});return reportViews.get(s.id);}
function reportStorage(v){
 if(!v)return '';
 const labels={database:'Database',recordings:'Raw recordings',recovery:'Recovery copies',temporary:'Temporary files',other:'Other data'};
 const used=v.filesystem.used/v.filesystem.total*100;
 return `<section class="report-storage"><div class="between"><h3>Storage in use</h3><strong>${footprintBytes(v.bytes)} Harbour data</strong></div><progress max="100" value="${used}" aria-label="Host filesystem usage"></progress><p>${footprintBytes(v.filesystem.free)} available on host · ${used.toFixed(1)}% used${v.pressure!=='normal'?` · <strong class="form-error">${e(v.pressure)} free-space level</strong>`:''}</p><div class="report-storage-parts">${Object.entries(v.categories).map(([k,c])=>`<span>${e(labels[k]||k)} <b>${footprintBytes(c.bytes)}</b></span>`).join('')}</div><p class="hint">${footprintBytes(v.reusable_database_bytes)} of database space is reusable. ${e(v.scope)}${v.complete?'':' Some files could not be measured.'}</p></section>`;
}
function reportContent(s,v){
 const d=v.data;if(!d)return `<p>${e(v.error||'Loading reports…')}</p>`;
 const p=d.policy,st=d.state||{},days=d.days,latest=days[0]?.day||new Date(Date.now()-86400000).toISOString().slice(0,10);
 return `<div class="between"><div><h2>Daily evidence reports</h2><p class="hint">A reconciled timeline, supporting evidence and final statistics. Open a report and use your browser’s Print command to save it as PDF.</p></div><div class="flex">${button('reports-refresh','Refresh','refresh','small')}${admin()?button('reports-settings','Processing & retention','settings','small')+button('reports-run','Process now','','small',''+(!p.enabled?'disabled':'')):''}</div></div>
 ${reportStorage(d.storage)}
 <p class="hint">Saved reports, call summaries and compact facts: ${footprintBytes(Object.values(d.stored_bytes||{}).reduce((a,b)=>a+b,0))} / ${p.max_mib} MiB.</p>${d.budget_exceeded?'<p class="form-error">Summary budget exceeded by the newest report or pending facts. Older eligible days have been trimmed; review the budget and processing status.</p>':''}
 <p class="hint">${p.enabled?`Scheduled ${e(p.process_at)} · ${e(p.timezone)}. Next run ${e(new Date(d.next_run*1000).toLocaleString())}.`:'Processing paused; raw archives remain protected.'} ${st.last_success?'Last success '+e(new Date(st.last_success*1000).toLocaleString()):'No completed processing run yet.'}</p>
 ${st.progress?`<p role="status">${e(st.progress)}</p>`:''}${st.requested?'<p role="status">Processing requested; waiting for the recorder worker.</p>':''}${st.error?`<p class="form-error">${e(st.error)}</p>`:''}${v.error?`<p class="form-error">${e(v.error)}</p>`:''}
 <form id="report-range" data-preserve-children data-server="${e(s.id)}" class="report-range" action="/api/servers/${encodeURIComponent(s.id)}/network/reports/period" method="get" target="_blank"><label>Period<select id="report-preset"><option value="day">One day</option><option value="week">Calendar week (Monday–Sunday)</option><option value="custom">Custom dates</option></select></label><label>From<input type="date" name="start" value="${e(latest)}" required></label><label>Through<input type="date" name="end" value="${e(latest)}" required></label><button type="submit" class="primary">Open printable report</button></form>
 <div class="network-table"><table data-table="daily-reports"><thead><tr><th>Date</th><th>Compiled</th><th>Compressed report</th><th>Review</th></tr></thead><tbody>${days.map(day=>`<tr data-key="report:${e(day.day)}"><td>${e(day.day)}</td><td>${e(new Date(day.generated*1000).toLocaleString())}</td><td>${footprintBytes(day.bytes)}</td><td><a target="_blank" rel="noopener" href="/api/servers/${encodeURIComponent(s.id)}/network/reports/period?start=${encodeURIComponent(day.day)}&end=${encodeURIComponent(day.day)}">Open HTML report</a></td></tr>`).join('')||'<tr><td colspan="4">Reports appear after the first scheduled processing run. Existing recordings are imported once; missing days stay explicitly unavailable.</td></tr>'}</tbody></table></div>`;
}
function renderReports(s){return `<section class="network-view report-view" data-server="${e(s.id)}" data-key="reports:${e(s.id)}" data-preserve-children>${reportContent(s,reportView(s))}</section>`;}
function paintReports(s){const root=$('.report-view');if(root?.dataset.server===s.id&&state.tab==='reports')updateHTML(root,reportContent(s,reportView(s)));}
async function loadReports(s,force=false){
 const v=reportView(s);if(v.busy||(!force&&Date.now()-v.loaded<15000))return;v.busy=true;
 try{v.data=await api(`/servers/${encodeURIComponent(s.id)}/network/reports`);v.loaded=Date.now();v.error='';}catch(error){v.error=error.message;}finally{v.busy=false;paintReports(s);}
}
async function reportSettings(s){
 const d=await api(`/servers/${encodeURIComponent(s.id)}/network/reports`),p=d.policy;
 const field=(key,label,min,max)=>`<label>${label}<input type="number" name="${key}" min="${min}" max="${max}" value="${p[key]}" required></label>`;
 modal('Processing & retention',`<form id="report-policy" data-server="${e(s.id)}"><label class="check-line"><input type="checkbox" name="enabled" ${p.enabled?'checked':''}>Enable scheduled processing</label><div class="form-grid"><label>Processing time<input type="time" name="process_at" value="${e(p.process_at)}" required></label><label>Timezone<input name="timezone" value="${e(p.timezone)}" placeholder="Europe/London" required></label>${field('retention_days','Keep daily reports (days)',7,3650)}${field('max_mib','Reports and compact facts budget (MiB)',16,16384)}${field('call_retention_days','Keep full call evidence (days)',1,365)}${field('call_max_mib','Full call evidence budget (MiB)',16,16384)}</div><p class="hint">The previous calendar day is compiled at this time. Raw archive rotation follows the same schedule. Late readings revise earlier reports. Weekly and custom reports combine saved days.</p><p class="hint">Cleanup runs after successful report publication. The first age or size limit reached applies. Raw recordings keep the limits in Recording settings; the rolling database cache is still maintained throughout the day. Pausing processing also pauses archive cleanup.</p><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary" type="submit">Save processing settings</button></div></form>`,true);
}
document.addEventListener('click',async event=>{
 const action=event.target.closest('[data-action]')?.dataset.action,s=current();if(!s||!action?.startsWith('reports-'))return;
 try{if(action==='reports-settings')await reportSettings(s);if(action==='reports-refresh')await loadReports(s,true);if(action==='reports-run'){await api(`/servers/${s.id}/network/reports/run`,'POST',{});toast('Processing queued');await loadReports(s,true);}}catch(error){toast(error.message,true);}
});
document.addEventListener('submit',async event=>{
 const form=event.target;
 if(form.id==='report-range'){const start=form.elements.start.value,end=form.elements.end.value;if(end<start){event.preventDefault();toast('The end date must follow the start date',true);}return;}
 if(form.id!=='report-policy')return;event.preventDefault();const data=Object.fromEntries(new FormData(form));
 const body={enabled:data.enabled==='on',timezone:data.timezone,process_at:data.process_at};for(const key of ['retention_days','max_mib','call_retention_days','call_max_mib'])body[key]=Number(data[key]);
 try{await api(`/servers/${form.dataset.server}/network/reports/policy`,'PUT',body);closeOverlay(true);await loadReports(current(),true);toast('Processing schedule saved');}catch(error){formError(error.message);}
});
document.addEventListener('change',event=>{
 const form=event.target.closest('#report-range');if(!form||!['report-preset',''].includes(event.target.id)&&event.target.name!=='start')return;
 if(event.target.name==='end')return;const preset=$('#report-preset',form).value;if(preset==='custom')return;
 const start=new Date(form.elements.start.value+'T12:00:00Z');if(!Number.isFinite(start.getTime()))return;
 if(preset==='week')start.setUTCDate(start.getUTCDate()-(start.getUTCDay()+6)%7);
 const end=new Date(start);if(preset==='week')end.setUTCDate(end.getUTCDate()+6);
 form.elements.start.value=start.toISOString().slice(0,10);form.elements.end.value=end.toISOString().slice(0,10);
});
