'use strict';
let incidentRequest=0,incidentExplorer=null;
const incidentTime=at=>Number.isFinite(at)?new Date(at*1000).toLocaleString():'Unavailable';
const incidentBase=id=>`/servers/${encodeURIComponent(id)}/network/incidents`;
const incidentRaw=(id,event)=>`/api/servers/${encodeURIComponent(id)}/network/export/${encodeURIComponent(event)}`;
function incidentLinks(id,event){return `<a href="${incidentRaw(id,event)}">Download original evidence</a> · <a href="/api${incidentBase(id)}/${encodeURIComponent(event)}?download=true">Download analysis JSON</a>`;}
function incidentTrigger(report){
  const t=report.trigger,p=t.evidence;
  if(t.mode==='manual')return '<p>This incident was marked manually. It does not require a probe threshold crossing.</p>';
  return `<p>Automatic marker · ${e(t.verification)} trigger${p?` · ${e(p.source||'recorder')} → ${e(p.target||'unknown target')} · ${e(p.reason||p.status||'degradation')}${Number.isFinite(p.rtt_ms)?` (${networkNumber(p.rtt_ms)} ms)`:''}${Number.isFinite(p.latency_limit_ms)?` · threshold ${networkNumber(p.latency_limit_ms,0)} ms`:''}`:''}.</p>`;
}
function renderIncidentReport(id,r){
  const c=r.coverage;
  return `<div class="incident-report">
    <div class="between incident-heading"><div><h3>${e(r.incident.label||'Incident')}</h3><p class="hint">${e(r.server_name)} · ${e(incidentTime(r.incident.measured))}</p></div><span class="tag">${r.incident.complete?'Window sealed':'Still collecting'}</span></div>
    <p class="incident-summary">${e(r.summary)}</p>${incidentTrigger(r)}
    <p class="hint">A single recorder timeout or RTT above its run threshold can trigger a marker. Uncertain timing is excluded. Automatic markers have a five-minute cooldown; router probes, CPU and Wi-Fi counters do not trigger them.</p>
    <div class="incident-stats"><div><strong>${c.samples.toLocaleString()}</strong><span>Stored observations</span></div><div><strong>${c.kinds.telemetry||0}</strong><span>Router readings</span></div><div><strong>${c.kinds.gap||0}</strong><span>Collection gaps</span></div><div><strong>${c.duplicates_removed}</strong><span>Duplicate rows excluded</span></div></div>
    <p class="hint">Requested window: ${e(incidentTime(c.requested_start))} – ${e(incidentTime(c.requested_end))}.<br>Available sample range: ${e(incidentTime(c.first_at))} – ${e(incidentTime(c.last_at))}. A sample range can contain gaps.</p>
    ${r.quality.length?`<details class="incident-quality" open><summary>Evidence limitations (${r.quality.length})</summary><ul>${r.quality.map(q=>`<li>${e(q)}</li>`).join('')}</ul></details>`:''}
    ${renderIncidentBriefForm(id,r.incident.id)}
    <h3>Probe paths</h3><p class="hint">Timeouts are missed reply deadlines, not confirmed packet loss. Late replies supplement their original timeout and are not counted as extra attempts. Before/after p95 values describe available replies on either side of the marker.</p>
    <div class="network-table"><table><thead><tr><th>Path</th><th>Attempts / uncertain</th><th>Timeouts</th><th>Above threshold</th><th>Late replies</th><th>Before / after p95</th><th>Peak RTT</th></tr></thead><tbody>${r.paths.map(p=>`<tr><td>${e(p.source)} → ${e(p.target)}</td><td>${p.attempts} / ${p.uncertain}</td><td>${p.timeouts} (${networkNumber(p.timeout_percent)}%)</td><td>${p.high_latency}</td><td>${p.late_replies}</td><td>${networkNumber(p.before.p95_ms)} / ${networkNumber(p.after.p95_ms)} ms</td><td>${networkNumber(p.peak_ms)} ms</td></tr>`).join('')||'<tr><td colspan="7">No probes are available.</td></tr>'}</tbody></table></div>
    <h3>Evidence and possible contributors</h3><p class="hint">These local rules identify observations worth investigating. They do not assign a definitive cause. Nearby means within ${r.method.correlation_seconds} seconds in the same recorder run; delayed telemetry is excluded.</p>
    <div class="incident-findings">${r.findings.map(f=>`<article class="incident-finding"><div class="between"><h4>${e(f.title)}</h4><span class="tag">${e(f.assessment)}</span></div><p>${e(f.explanation)}</p><p><strong>Next check:</strong> ${e(f.next_check)}</p><p class="hint">${f.sample_count} supporting observations · ${e(incidentTime(f.first_at))} – ${e(incidentTime(f.last_at))}</p><div class="flex">${f.evidence_refs.map(ref=>button('incident-evidence',`Sample ${ref}`,'','small ghost',`data-ref="${ref}"`)).join('')}</div></article>`).join('')||'<p>No supported contributor was found. Review missing evidence and collect a comparison window.</p>'}</div>
    <h3>Event timeline</h3><p class="hint">Showing ${r.timeline.length} of ${r.timeline_total} notable observations in time order. Log times reflect receipt at the recorder.</p>
    <ol class="incident-timeline">${r.timeline.map(t=>`<li><time>${e(incidentTime(t.at))}</time><span>${e(t.description)}</span>${button('incident-evidence',`Sample ${t.ref}`,'','small ghost',`data-ref="${t.ref}"`)}</li>`).join('')||'<li>No notable observations in the available window.</li>'}</ol>
    <h3>Referenced evidence</h3><p class="hint">Sample numbers are zero-based indexes in the original export’s samples array. All original samples remain in the evidence download.</p>
    ${r.evidence.map(item=>`<details class="incident-evidence" data-ref="${item.ref}"><summary>Sample ${item.ref} · ${e(item.sample.kind)} · ${e(incidentTime(item.sample.at))}</summary><pre>${e(JSON.stringify(item.sample,null,2))}</pre></details>`).join('')}
    <p>${incidentLinks(id,r.incident.id)}</p><p class="hint">Analyzer version ${r.analyzer_version}. ${e(r.method.scope)}</p>
  </div>`;
}
async function openIncident(id,event){
  modal('Incident analysis',`<div id="incident-content"><p role="status">Reading incident evidence…</p></div>`,true);
  const request=++incidentRequest;
  $('.modal').classList.add('incident-modal');
  try{
    const report=await api(`${incidentBase(id)}/${encodeURIComponent(event)}`);
    if(request!==incidentRequest||!$('#incident-content'))return;
    $('#incident-content').innerHTML=`${incidentExplorer?.id===id?button('incident-back','Back to patterns','', 'small ghost'):''}${renderIncidentReport(id,report)}`;
  }catch(error){if(request===incidentRequest&&$('#incident-content'))$('#incident-content').innerHTML=`<p class="form-error" role="alert">${e(error.message)}</p><a href="${incidentRaw(id,event)}">Download original evidence</a>`;}
}
function explorerQuery(v,download=false){
  const p=new URLSearchParams({limit:'200',offset:String(v.offset),kind:v.kind,until:String(v.until)});
  if(v.hours)p.set('since',String(v.until-v.hours*3600));
  if(download)p.set('download','true');
  return p.toString();
}
function renderIncidentExplorer(v){
  const d=v.data;
  const filtered=(d?.reports||[]).filter(r=>!v.pattern||d.patterns.patterns.find(p=>p.key===v.pattern)?.incident_ids.includes(r.incident.id));
  const page=filtered.slice(v.page*20,(v.page+1)*20);
  return `<div id="incident-explorer" data-id="${e(v.id)}">
    <p>Compare retained incidents using the evidence saved with each window. Open a report to inspect the observations behind each finding.</p>
    <form id="incident-filters"><div class="form-grid"><label>Markers<select name="kind">${[['all','All markers'],['automatic','Automatic only'],['manual','Manual only']].map(([key,label])=>`<option value="${key}" ${v.kind===key?'selected':''}>${label}</option>`).join('')}</select></label><label>Period<select name="hours">${[[0,'All retained'],[24,'Last 24 hours'],[168,'Last 7 days'],[720,'Last 30 days']].map(([key,label])=>`<option value="${key}" ${v.hours===key?'selected':''}>${label}</option>`).join('')}</select></label></div><div class="form-actions"><button class="primary small" type="submit" ${v.busy?'disabled':''}>Refresh analysis</button></div></form>
    ${v.busy?'<p role="status">Analysing saved evidence…</p>':''}${v.error?`<p class="form-error" role="alert">${e(v.error)}</p>`:''}
    ${d?`<div class="incident-stats"><div><strong>${d.patterns.incident_count}</strong><span>Incidents analysed</span></div><div><strong>${d.patterns.window_clusters}</strong><span>Window groups</span></div><div><strong>${d.total}</strong><span>Matching retained markers</span></div></div>
    <p class="hint">${e(d.patterns.note)}<br>Analysis covers markers ${d.total?v.offset+1:0}–${Math.min(v.offset+d.limit,d.total)} of ${d.total} through ${e(incidentTime(v.until))}. Results reflect this batch only.</p>
    <p class="hint">The patterns download contains summaries and recurrence statistics for this batch. Harbour prepares a compressed file locally before the download starts.</p>
    ${d.errors.map(error=>`<p class="form-error">${e(error.message)} <a href="${incidentRaw(v.id,error.id)}">Original evidence</a></p>`).join('')}
    <h3>Recurring observations</h3><p class="hint">Select an observation to show matching incidents. Counts include manual markers and open windows if selected above.</p>
    <div class="incident-patterns">${button('incident-pattern','All incidents','','small '+(!v.pattern?'primary':'ghost'),'data-pattern=""')}${d.patterns.patterns.map(p=>button('incident-pattern',`${e(p.title)} · ${p.incidents} incidents / ${p.window_clusters} groups`,'','small '+(v.pattern===p.key?'primary':'ghost'),`data-pattern="${e(p.key)}"`)).join('')}</div>
    <h3>Incident overviews</h3><div class="network-table incident-overviews"><table><thead><tr><th>Time / marker</th><th>Overview</th><th>Report</th></tr></thead><tbody>${page.map(r=>`<tr><td>${e(incidentTime(r.incident.measured))}<br><span class="hint">${r.incident.automatic?'Automatic':'Manual'} · ${r.incident.complete?'Sealed':'Collecting'}</span></td><td><strong>${e(r.incident.label)}</strong><p>${e(r.summary)}</p><span class="hint">${r.quality.length} evidence limitations</span></td><td>${button('incident-open','Analyse','','small',`data-server="${e(v.id)}" data-event="${e(r.incident.id)}"`)}</td></tr>`).join('')||'<tr><td colspan="3">No incidents match this selection.</td></tr>'}</tbody></table></div>
    <div class="between incident-pagination"><div class="flex">${button('incident-page','Previous','','small ghost',`data-direction="-1" ${v.page===0?'disabled':''}`)}<span class="hint">${filtered.length? v.page*20+1:0}–${Math.min((v.page+1)*20,filtered.length)} of ${filtered.length}</span>${button('incident-page','Next','','small ghost',`data-direction="1" ${(v.page+1)*20>=filtered.length?'disabled':''}`)}</div><a href="/api${incidentBase(v.id)}?${explorerQuery(v,true)}">Download patterns (.json.gz)</a></div>
    <div class="flex">${v.offset?button('incident-batch','Newer batch','','small ghost','data-direction="-1"'):''}${d.has_more?button('incident-batch','Older batch','','small ghost','data-direction="1"'):''}</div>`:''}
  </div>`;
}
function paintExplorer(){if(incidentExplorer&&$('#incident-explorer'))$('#incident-explorer').outerHTML=renderIncidentExplorer(incidentExplorer);}
async function loadIncidentExplorer(){
  const v=incidentExplorer,request=++incidentRequest;v.busy=true;v.error=null;paintExplorer();
  try{const data=await api(`${incidentBase(v.id)}?${explorerQuery(v)}`);if(request!==incidentRequest)return;v.data=data;}
  catch(error){if(request!==incidentRequest)return;v.error=error.message;}
  finally{if(request===incidentRequest){v.busy=false;paintExplorer();}}
}
async function openIncidentExplorer(id){
  incidentExplorer={id,offset:0,page:0,hours:0,kind:'all',until:Date.now()/1000,pattern:'',data:null,busy:false,error:null};
  modal('Incident patterns',renderIncidentExplorer(incidentExplorer),true);$('.modal').classList.add('incident-modal');
  await loadIncidentExplorer();
}
document.addEventListener('harbour:overlay-close',()=>{incidentRequest++;});
document.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action]');if(!el)return;
  const a=el.dataset.action;
  if(a==='incident-open')await openIncident(el.dataset.server,el.dataset.event);
  else if(a==='incident-explore')await openIncidentExplorer(el.dataset.server);
  else if(a==='incident-evidence'){
    const detail=document.querySelector(`.incident-evidence[data-ref="${Number(el.dataset.ref)}"]`);
    if(detail){detail.open=true;detail.scrollIntoView({block:'center'});detail.querySelector('summary').focus();}
  }else if(a==='incident-back'&&incidentExplorer){incidentRequest++;modal('Incident patterns',renderIncidentExplorer(incidentExplorer),true);$('.modal').classList.add('incident-modal');}
  else if(a==='incident-pattern'&&incidentExplorer){incidentExplorer.pattern=el.dataset.pattern;incidentExplorer.page=0;paintExplorer();}
  else if(a==='incident-page'&&incidentExplorer){incidentExplorer.page=Math.max(0,incidentExplorer.page+Number(el.dataset.direction));paintExplorer();}
  else if(a==='incident-batch'&&incidentExplorer&&!incidentExplorer.busy){incidentExplorer.offset=Math.max(0,incidentExplorer.offset+200*Number(el.dataset.direction));incidentExplorer.page=0;incidentExplorer.pattern='';incidentExplorer.data=null;await loadIncidentExplorer();}
});
document.addEventListener('submit',async event=>{
  if(event.target.id!=='incident-filters'||!incidentExplorer)return;
  event.preventDefault();const data=new FormData(event.target);
  Object.assign(incidentExplorer,{kind:data.get('kind'),hours:Number(data.get('hours')),until:Date.now()/1000,offset:0,page:0,pattern:'',data:null});
  await loadIncidentExplorer();
});

const incidentBriefState=new WeakMap();
function renderIncidentBriefForm(id,event){
  return `<details class="incident-brief"><summary>Export condensed evidence brief</summary>
    <p>Pair each conclusion with measured evidence for an independent review. Includes path totals, coverage limits and references back to the original samples.</p>
    <form id="incident-brief-form" data-server="${e(id)}" data-event="${e(event)}">
      <div class="form-grid"><label>Format<select name="format"><option value="markdown">Markdown · readable brief</option><option value="json">JSON · structured data</option></select></label>
      <label>Evidence examples per conclusion<select name="examples"><option value="1">1 · shortest</option><option value="3" selected>3 · balanced</option><option value="6">6 · more evidence</option></select></label></div>
      <label class="check"><input type="checkbox" name="include_review" checked> Include instructions for an independent LLM review</label>
      <p class="hint">Examples are selected supporting observations, not the full record. Hostnames and IP/MAC addresses present in the evidence remain in the brief. Nothing is sent to an LLM.</p>
      <div class="flex"><button type="submit" class="small primary">Generate preview</button>${button('incident-brief-copy','Copy brief','','small ghost','disabled')}${button('incident-brief-download','Download brief','','small ghost','disabled')}</div>
      <p class="incident-brief-status hint" role="status" aria-live="polite"></p><p class="form-error" role="alert"></p>
      <label class="incident-brief-preview" hidden>Export preview<textarea name="preview" readonly rows="14" spellcheck="false"></textarea></label>
    </form></details>`;
}
function invalidateIncidentBrief(form){
  incidentBriefState.delete(form);
  form.querySelector('[type=submit]').disabled=false;
  form.querySelectorAll('[data-action^="incident-brief-"]').forEach(b=>b.disabled=true);
  form.querySelector('.incident-brief-preview').hidden=true;
  form.elements.preview.value='';
  form.querySelector('.incident-brief-status').textContent='Generate a preview to use these options.';
  form.querySelector('.form-error').textContent='';
  markFormSaved(form);
}
document.addEventListener('change',event=>{
  const form=event.target.closest('#incident-brief-form');
  if(form&&event.target.matches('select,input'))invalidateIncidentBrief(form);
});
document.addEventListener('submit',async event=>{
  const form=event.target;if(form.id!=='incident-brief-form')return;
  event.preventDefault();markFormSaved(form);
  invalidateIncidentBrief(form);
  const request={};incidentBriefState.set(form,request);
  form.querySelector('[type=submit]').disabled=true;
  form.querySelector('.incident-brief-status').textContent='Preparing evidence brief…';
  const query=new URLSearchParams({format:form.elements.format.value,examples:form.elements.examples.value,include_review:String(form.elements.include_review.checked)});
  try{
    const result=await api(`${incidentBase(form.dataset.server)}/${encodeURIComponent(form.dataset.event)}/brief?${query}`);
    if(!form.isConnected||incidentBriefState.get(form)!==request)return;
    request.result=result;
    form.elements.preview.value=result.content;form.elements.preview.scrollTop=0;
    form.querySelector('.incident-brief-preview').hidden=false;
    form.querySelectorAll('[data-action^="incident-brief-"]').forEach(b=>b.disabled=false);
    form.querySelector('.incident-brief-status').textContent=`${(result.bytes/1024).toFixed(1)} KiB · Copy or download the exact preview below. Original evidence remains available separately.`;
    markFormSaved(form);
  }catch(error){
    if(form.isConnected&&incidentBriefState.get(form)===request){form.querySelector('.form-error').textContent=error.message;form.querySelector('.incident-brief-status').textContent='';}
  }finally{if(form.isConnected&&incidentBriefState.get(form)===request)form.querySelector('[type=submit]').disabled=false;}
});
document.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action^="incident-brief-"]');if(!el)return;
  const form=el.closest('#incident-brief-form'),result=incidentBriefState.get(form)?.result;if(!result)return;
  if(el.dataset.action==='incident-brief-copy'){
    try{await navigator.clipboard.writeText(result.content);if(form.isConnected&&incidentBriefState.get(form)?.result===result)form.querySelector('.incident-brief-status').textContent='Brief copied.';}
    catch{if(form.isConnected&&incidentBriefState.get(form)?.result===result){form.elements.preview.focus();form.elements.preview.select();form.querySelector('.incident-brief-status').textContent='Clipboard access is unavailable. The preview is selected; use your usual Copy shortcut.';}}
  }else if(el.dataset.action==='incident-brief-download'){
    const url=URL.createObjectURL(new Blob([result.content],{type:result.media_type+';charset=utf-8'})),link=document.createElement('a');
    link.href=url;link.download=result.filename;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
});
