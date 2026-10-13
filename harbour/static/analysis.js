'use strict';
const analysisViews=new Map();
function analysisView(s){if(!analysisViews.has(s.id))analysisViews.set(s.id,{hours:168,until:Date.now()/1000,data:null,report:null,page:0,error:'',busy:false,loaded:0,request:0,detailRequest:0,filter:'all'});return analysisViews.get(s.id);}
const analysisBase=id=>`/servers/${encodeURIComponent(id)}/network/analysis`;
const analysisQuery=v=>new URLSearchParams({hours:String(v.hours),until:String(v.until)}).toString();
const analysisDate=t=>Number.isFinite(t)?new Date(t*1000).toLocaleString():'Unaligned';
function analysisRefs(refs){return [...new Set(refs||[])].map(ref=>button('analysis-evidence',e(ref),'','small ghost',`data-key="analysis-ref:${e(ref)}" data-ref="${e(ref)}"`)).join(' ');}
function analysisDetail(r,v){
 const episodes=r.episodes.slice(v.page*20,(v.page+1)*20);
 const findings=(r.contributors||[]).map(f=>`<article class="incident-finding"><div class="between"><h4>${e(f.title)}</h4><span class="tag">${e(f.assessment)}</span></div><p>${e(f.conclusion)}</p>${f.support?`<p>${e(f.support)}</p>`:''}<p><b>Next check:</b> ${e(f.next_check)}</p><p class="hint">${e(f.limitations||'')}</p><div class="flex">${analysisRefs(f.evidence_refs)}</div></article>`).join('');
 return `<section class="analysis-detail" data-key="analysis-report:${e(r.id)}"><div class="between"><h3>${r.source==='call'?'Call-flow':'Ping incident'} conclusion</h3><a href="/api${analysisBase(r.server_id)}/${r.source}/${encodeURIComponent(r.source_id)}?download=true">Download this conclusion (.json.gz)</a></div>
 <p class="incident-summary">${e(r.conclusion)}</p><p class="hint">${e(analysisDate(r.start))} – ${e(analysisDate(r.end))} · ${e(r.status)} · analyzer ${r.analyzer_version}. Root cause is not established.</p>
 <div class="probe-cards">${r.directions.map(d=>`<article class="probe-card"><h4>${e(d.direction)} receiver</h4><p>${networkNumber(d.summary.lost_percent,2)}% loss · ${networkNumber(d.summary.jitter_ms)} ms summary jitter</p><p>${d.impaired_intervals} impaired intervals · ${networkNumber(d.affected_seconds)} affected seconds / ${networkNumber(d.observed_seconds)} observed</p><p class="hint">${d.unaligned_intervals} intervals without precise alignment · peak reported interval jitter ${networkNumber(d.peak_interval_jitter_ms)} ms</p>${analysisRefs(d.evidence_refs)}</article>`).join('')}</div>
 ${r.quality.length?`<details class="incident-quality" open><summary>Evidence limitations (${r.quality.length})</summary><ul>${r.quality.map(q=>`<li>${e(q)}</li>`).join('')}</ul></details>`:''}
 <h3>Degradation episodes</h3><p class="hint">Consecutive or overlapping measured impairments are grouped. Spans reflect sampling, not exact continuous outage duration. Unaligned receiver symptoms remain in the directional totals.</p>
 <div class="network-table"><table><thead><tr><th>Time</th><th>Span</th><th>Evidence-backed observation</th><th>Evidence</th></tr></thead><tbody>${episodes.map(p=>`<tr><td>${e(analysisDate(p.start))}<br><small>±${networkNumber(p.uncertainty_seconds,3)}s alignment</small></td><td>${networkNumber(p.sampled_span_seconds)}s</td><td>${e(p.conclusion)}<br><small>${e(p.directions.join(' + '))}${p.ping_targets.length?' · '+e(p.ping_targets.join(', ')):''}</small></td><td>${analysisRefs(p.evidence_refs)}</td></tr>`).join('')||'<tr><td colspan="4">No precisely aligned degradation episodes. See observations and evidence coverage above.</td></tr>'}</tbody></table></div>
 <div class="between"><div class="flex">${button('analysis-page','Previous','','small ghost',`data-key="analysis-previous" data-step="-1" ${v.page===0?'disabled':''}`)}<span class="hint">${r.episodes.length?v.page*20+1:0}–${Math.min((v.page+1)*20,r.episodes.length)} of ${r.episodes.length}</span>${button('analysis-page','Next','','small ghost',`data-key="analysis-next" data-step="1" ${(v.page+1)*20>=r.episodes.length?'disabled':''}`)}</div>${r.episodes_omitted?`<p class="hint">${r.episodes_omitted} further episodes exceed the bounded report; original evidence retains their samples.</p>`:''}</div>
 <details><summary>Path comparisons</summary><h4>Recorded ping paths across this window</h4><div class="network-table"><table><thead><tr><th>Path</th><th>Usable attempts / timeouts</th><th>p95 RTT</th><th>Evidence</th></tr></thead><tbody>${(r.probe_summary||[]).map(p=>`<tr><td>${e(p.source)} → ${e(p.target)}</td><td>${p.usable_attempts} / ${p.timeouts}</td><td>${networkNumber(p.p95_ms)} ms</td><td>${analysisRefs(p.evidence_refs)}</td></tr>`).join('')||'<tr><td colspan="4">No probe evidence.</td></tr>'}</tbody></table></div>${r.path_findings.map(p=>`<article class="incident-finding"><h4>Episode ${p.episode_id}</h4>${p.conclusions.map(t=>`<p>${e(t)}</p>`).join('')}<div class="network-table"><table><thead><tr><th>Path</th><th>Observation</th><th>Evidence</th></tr></thead><tbody>${p.paths.map(x=>`<tr><td>${e(x.source)} → ${e(x.target)}</td><td>${e(x.state)} · ${x.observations} readings</td><td>${analysisRefs(x.evidence_refs)}</td></tr>`).join('')}</tbody></table></div><p class="hint">${e(p.limitation)}</p></article>`).join('')||'<p>No comparable paths were recorded.</p>'}</details>
 <h3>Possible contributors and contrary evidence</h3>${findings||'<p>No contributor is supported by the available aligned evidence.</p>'}
 <details><summary>Baseline, load and recovery</summary>${r.load_response.map(p=>`<article class="incident-finding"><h4>${e(p.source)} → ${e(p.target)}</h4><p>${e(p.conclusion)}</p><p class="hint">p95 RTT: ${networkNumber(p.phases.baseline?.p95_ms)} → ${networkNumber(p.phases.load?.p95_ms)} → ${networkNumber(p.phases.recovery?.p95_ms)} ms. Timeouts: ${p.phases.baseline?.timeouts??'—'} → ${p.phases.load?.timeouts??'—'} → ${p.phases.recovery?.timeouts??'—'}.</p>${analysisRefs(p.evidence_refs)}</article>`).join('')||'<p>No controlled baseline/load/recovery comparison is available.</p>'}</details>
 <details><summary>Sender versus receiver delivery</summary>${r.delivery.map(p=>`<p><b>${e(p.direction)}:</b> ${e(p.conclusion)} ${analysisRefs(p.evidence_refs)}</p>`).join('')||'<p>No sender measurements in this ping incident.</p>'}</details>
 <details><summary>Referenced measured evidence</summary><p class="hint">${e(r.evidence_selection.method)} ${r.evidence_selection.sample_count} source observations; ${r.evidence_selection.omitted_examples} additional examples omitted. Original-source indexes are local to this report.</p>${Object.entries(r.evidence).map(([key,data])=>`<details class="analysis-evidence" data-ref="${e(key)}"><summary>${e(key)}</summary><pre class="code-block">${e(JSON.stringify(data,null,2))}</pre></details>`).join('')}</details>
 </section>`;
}
function analysisContent(s,v){
 const d=v.data,rows=(d?.reports||[]).filter(r=>v.filter==='all'||r.source===v.filter);
 return `<div class="between network-heading"><div><h2>Connection analysis</h2><p>Compare ping, call flows and router evidence. Download conclusions for an independent review in a new session.</p></div></div>
 <div class="analysis-controls"><label>Period<select data-analysis-filter="hours">${[[24,'Last 24 hours'],[168,'Last 7 days'],[720,'Last 30 days'],[8760,'Last year']].map(([k,t])=>`<option value="${k}" ${v.hours===k?'selected':''}>${t}</option>`).join('')}</select></label><label>Show<select data-analysis-filter="source">${[['all','All evidence'],['call','Call tests'],['incident','Ping incidents']].map(([k,t])=>`<option value="${k}" ${v.filter===k?'selected':''}>${t}</option>`).join('')}</select></label>${button('analysis-refresh',v.busy?'Analysing…':'Analyse saved evidence','activity','small primary',v.busy?'disabled':'')}${v.busy?button('analysis-stop','Stop after this batch','','small ghost'):''}${d?.reports.length?`<a href="/api${analysisBase(s.id)}/download?${analysisQuery(v)}">Download all conclusions in period (.json.gz)</a>`:''}</div>
 <p class="hint" role="status">${v.busy?'Analysing saved evidence locally. ':''}${d?`${d.reports.length} saved reports · ${d.pending} awaiting analysis. ${e(d.retention)}`:'Loading saved conclusions…'}</p>${v.error?`<p class="form-error" role="alert">${e(v.error)}</p>`:''}
 ${d?.errors?.map(x=>`<p class="form-error">${e(x.id)}: ${e(x.message)}</p>`).join('')||''}
 <details class="network-notes"><summary>Evidence boundaries and handoff contents</summary><p>The compressed file includes readable conclusions, measured examples, comparison counts, timing limits, method/version, context and content hashes. No external AI service receives data. Host names, IPs and selected client identifiers remain in the evidence.</p><ul>${(d?.exclusions||[]).map(t=>`<li>${e(t)}</li>`).join('')}</ul><p class="hint">${e(d?.scope||'')} Downloads include all analyzed reports in the selected period; the Show filter only changes this list.</p></details>
 <h3>Saved conclusions</h3><div class="network-table"><table><thead><tr><th>Time / source</th><th>Conclusion</th><th>Review</th></tr></thead><tbody>${rows.map(r=>`<tr data-key="analysis-row:${e(r.id)}"><td>${e(analysisDate(r.start))}<br>${r.source==='call'?'Call test':'Ping incident'} · ${e(r.context?.schedule_name||r.context?.profile_snapshot?.name||r.label||'')}</td><td><b>${e(r.conclusion)}</b><br><span class="hint">${r.episode_count} episodes · ${r.quality.length} evidence limitations</span></td><td>${button('analysis-open','Review','','small',`data-source="${r.source}" data-id="${e(r.source_id)}"`)}</td></tr>`).join('')||'<tr><td colspan="3">No saved conclusions in this selection. Use Analyse saved evidence to process retained tests and sealed incidents.</td></tr>'}</tbody></table></div>
 ${(d?.sources||[]).filter(x=>!x.ready).length?'<p class="hint">Active tests, open incident windows and tests without collected evidence are not treated as healthy comparisons. Collect missing evidence in Call tests.</p>':''}
 <details open><summary>Comparable recurring tests</summary><p class="hint">Only matching recorded settings, observers and route context are grouped. These are retained test results, not whole-day outage rates.</p><div class="network-table"><table><thead><tr><th>Profile / endpoint</th><th>Compared tests</th><th>Directional exposure</th></tr></thead><tbody>${(d?.patterns||[]).map(p=>`<tr><td>${e(p.context.profile_snapshot?.name||p.context.profile)}<br>${e(p.context.endpoint)} · ${p.context.upload_kbps}/${p.context.download_kbps} kbps</td><td>${e(p.conclusion)}<br><small>${p.incomplete_comparisons} incomplete comparisons · ${p.overlapping_tests_excluded} overlapping tests excluded</small></td><td>${['upload','download'].map(k=>`${e(k)}: ${networkNumber(p.directions[k].affected_seconds)} / ${networkNumber(p.directions[k].observed_seconds)} seconds affected`).join('<br>')}</td></tr>`).join('')||'<tr><td colspan="3">No comparable call-test reports yet.</td></tr>'}</tbody></table></div></details>
 ${v.report?analysisDetail(v.report,v):''}`;
}
function renderConnectionAnalysis(s){const v=analysisView(s);return `<section class="network-view connection-analysis" data-server="${e(s.id)}" data-key="analysis:${e(s.id)}" data-preserve-children>${analysisContent(s,v)}</section>`;}
function paintAnalysis(s){const root=$('.connection-analysis');if(root?.dataset.server===s.id&&state.tab==='analysis')updateHTML(root,analysisContent(s,analysisView(s)));}
async function loadConnectionAnalysis(s,force=false,prepare=false){
 const v=analysisView(s);if(v.busy||(!force&&Date.now()-v.loaded<30000))return;
 const request=++v.request;v.busy=true;v.error='';paintAnalysis(s);
 let previous=Infinity;
 try{
  do{
   const data=await api(`${analysisBase(s.id)}?${analysisQuery(v)}${prepare?'&prepare=true':''}`);
   if(v.request!==request)return;
   v.data=data;v.loaded=Date.now();paintAnalysis(s);
   if(!prepare||!data.pending||data.errors.length||data.pending>=previous||current()?.id!==s.id||state.tab!=='analysis')break;
   previous=data.pending;
  }while(true);
 }catch(error){if(v.request===request)v.error=error.message;}
 finally{if(v.request===request){v.busy=false;paintAnalysis(s);}}
}
document.addEventListener('change',event=>{
 const field=event.target.dataset.analysisFilter,s=current();if(!field||!s)return;const v=analysisView(s);
 if(field==='source'){v.filter=event.target.value;paintAnalysis(s);return;}
 v.request++;v.detailRequest++;v.busy=false;v.hours=Number(event.target.value);v.until=Date.now()/1000;v.data=null;v.report=null;loadConnectionAnalysis(s,true);
});
document.addEventListener('click',async event=>{
 const el=event.target.closest('[data-action]'),s=current();if(!el||!s||!el.dataset.action.startsWith('analysis-'))return;
 const v=analysisView(s),action=el.dataset.action;
 if(action==='analysis-refresh'){v.until=Date.now()/1000;await loadConnectionAnalysis(s,true,true);}
 else if(action==='analysis-stop'){v.request++;v.busy=false;paintAnalysis(s);}
 else if(action==='analysis-page'){v.page=Math.max(0,v.page+Number(el.dataset.step));paintAnalysis(s);}
 else if(action==='analysis-open'){
  const request=++v.detailRequest;el.disabled=true;
  try{const r=await api(`${analysisBase(s.id)}/${el.dataset.source}/${encodeURIComponent(el.dataset.id)}`);if(v.detailRequest!==request||current()?.id!==s.id||state.tab!=='analysis')return;v.report=r;v.page=0;v.error='';paintAnalysis(s);$('.analysis-detail')?.scrollIntoView({block:'start'});}
  catch(error){if(v.detailRequest===request&&current()?.id===s.id&&state.tab==='analysis'){v.error=error.message;paintAnalysis(s);}}finally{el.disabled=false;}
 }else if(action==='analysis-evidence'){
  const detail=[...document.querySelectorAll('.analysis-evidence')].find(d=>d.dataset.ref===el.dataset.ref);
  if(detail){let parent=detail.parentElement.closest('details');if(parent)parent.open=true;detail.open=true;detail.scrollIntoView({block:'center'});detail.querySelector('summary').focus();}
 }
});
