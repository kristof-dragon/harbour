'use strict';
const analysisViews=new Map();
function analysisView(s){if(!analysisViews.has(s.id))analysisViews.set(s.id,{hours:168,until:Date.now()/1000,data:null,error:'',busy:false,loaded:0,request:0,detailRequest:0,filter:'all'});return analysisViews.get(s.id);}
const analysisBase=id=>`/servers/${encodeURIComponent(id)}/network/analysis`;
const analysisQuery=v=>new URLSearchParams({hours:String(v.hours),until:String(v.until)}).toString();
const analysisDate=t=>Number.isFinite(t)?new Date(t*1000).toLocaleString():'Unaligned';
const analysisObservations=[
 ['both','Ping and call-flow impairment','Ping to the test endpoint and call-flow receiver measurements both show impairment in this episode.'],
 ['udp_only','Call-flow impairment with normal endpoint ping','Receiver loss or jitter is impaired while enough matching endpoint ping evidence is normal.'],
 ['ping_only','Ping impairment with normal call flow','Ping is impaired while enough aligned upload and download receiver evidence is normal.'],
 ['udp_unconfirmed','Call-flow impairment; ping comparison incomplete','Call-flow impairment is measured, but endpoint ping coverage or timing cannot establish a comparison.'],
 ['ping_unconfirmed','Ping impairment; call-flow comparison incomplete','Ping impairment is measured, but call-flow coverage or timing cannot establish a comparison.']
];
function analysisRefLabel(ref){
 const parts=ref.split(':');
 if(parts[0]==='summary')return `${parts[1]==='upload'?'Upload':'Download'} summary`;
 if(parts[0]==='receiver')return `${parts[1]==='upload'?'Upload':'Download'} interval ${Number(parts[2])+1}`;
 if(parts[0]==='probe-summary')return `Ping path ${Number(parts[1])+1}`;
 if(parts[0]==='sample')return `Source sample ${parts[1]}`;
 if(parts[0]==='route')return 'Recorded route';
 return ref;
}
function analysisRefs(refs){return [...new Set(refs||[])].map(ref=>button('analysis-evidence',e(analysisRefLabel(ref)),'','small ghost',`data-key="analysis-ref:${e(ref)}" data-ref="${e(ref)}" title="${e(ref)}"`)).join(' ');}
function analysisObservationHelp(r){
 return `<dl class="analysis-observation-guide">${analysisObservations.map(([,label,meaning])=>`<dt>${e(label)}</dt><dd>${e(meaning)}</dd>`).join('')}</dl><p>Call-flow impairment means measured packet loss or receiver jitter above ${e(r.rules?.jitter_ms??30)} ms. Ping impairment means a timeout or RTT above the saved run threshold. A normal comparison needs at least ${e((r.rules?.minimum_coverage??.8)*100)}% eligible coverage; receiver timing uncertainty must be at most ${e(r.rules?.max_alignment_seconds??1)} second.</p><p>Episodes contain measured impairment, so “neither test impaired” and “insufficient comparison evidence” are whole-report outcomes, not episode rows. These observations do not identify a root cause.</p>`;
}
function analysisOverview(r){
 const verdict=r.symptoms_observed?'Impairment observed':r.comparison_complete?'No impairment observed':'Comparison incomplete';
 const observations=analysisObservations.map(([key,label])=>({label,count:r.episodes.filter(p=>p.classification===key).length})).filter(x=>x.count);
 const findings=(r.contributors||[]).map(f=>`<article class="analysis-finding"><div class="between"><h4>${e(f.title)}</h4><span class="tag">${e(f.assessment)}</span></div><p>${e(f.conclusion)}</p><p><b>Next check:</b> ${e(f.next_check)}</p><details><summary>Supporting evidence and limits</summary>${f.support?`<p>${e(f.support)}</p>`:''}<p class="hint">${e(f.limitations||'')}</p><div class="flex">${analysisRefs(f.evidence_refs)}</div></details></article>`).join('');
 return `<section class="analysis-verdict"><h3>${verdict}</h3>${observations.length?`<ul>${observations.map(x=>`<li>${e(x.label)} <span class="tag">${x.count} ${r.episodes_omitted?'shown ':''}episode${x.count===1?'':'s'}</span></li>`).join('')}</ul>`:`<p>${e(r.conclusion)}</p>`}<div class="analysis-verdict-note"><span>Root cause not established</span>${help('Observation and cause','The findings describe measured symptoms and possible contributors. They do not prove a faulty device, link or provider. Missing evidence is not a normal reading.')}</div></section>
 <div class="analysis-direction-grid">${r.directions.map(d=>`<article class="analysis-direction"><div class="compact-heading"><h3>${d.direction==='upload'?'Upload':'Download'}</h3><span class="hint">Receiver measurements</span></div><dl><div><dt>Packet loss</dt><dd>${networkNumber(d.summary.lost_percent,2)}<small>%</small></dd></div><div><dt>Summary jitter</dt><dd>${networkNumber(d.summary.jitter_ms)}<small>ms</small></dd></div><div><dt>Affected / observed</dt><dd>${networkNumber(d.affected_seconds)}<small>s / ${networkNumber(d.observed_seconds)} s</small></dd></div></dl><div class="compact-heading"><span class="hint">${d.impaired_intervals} impaired intervals · peak jitter ${networkNumber(d.peak_interval_jitter_ms)} ms</span>${help('Affected time','Affected time sums measured receiver intervals with loss or high jitter. It is not the duration of a continuous outage or a measure of actual call quality.')}</div>${d.unaligned_intervals?`<p class="analysis-coverage-note">${d.unaligned_intervals} intervals cannot be aligned precisely.</p>`:''}<details><summary>Measured evidence</summary><div class="flex">${analysisRefs(d.evidence_refs)}</div></details></article>`).join('')}</div>
 <div class="between compact-section-heading"><div class="compact-heading"><h3>Possible contributors</h3>${help('Possible contributors','Router signals are compared with the affected periods and available control periods. An association does not establish causation; supporting and contrary evidence remain available.')}</div></div>${findings||'<p class="hint">No contributor is supported by the available aligned evidence.</p>'}`;
}
function analysisEpisodes(r){
 return `<div class="compact-heading"><h3>Degradation episodes</h3>${infoMenu('episode-meaning','Observation meanings',analysisObservationHelp(r))}${help('Episode duration','Consecutive or overlapping measured impairments are grouped. Spans reflect sampling, not exact continuous outage duration. Unaligned receiver symptoms remain in the directional totals.')}</div>
 <div class="network-table"><table data-table="episodes:${e(r.id)}"><thead><tr><th data-type="date">Time</th><th data-type="number">Span (s)</th><th>Evidence-backed observation</th><th>Evidence</th></tr></thead><tbody>${r.episodes.map(p=>`<tr><td data-value="${e(p.start??'')}">${e(analysisDate(p.start))}<br><small>±${networkNumber(p.uncertainty_seconds,3)} s alignment</small></td><td data-value="${p.sampled_span_seconds}">${networkNumber(p.sampled_span_seconds)}</td><td>${e(p.conclusion)}<br><small>${e(p.directions.join(' + '))}${p.ping_targets.length?' · '+e(p.ping_targets.join(', ')):''}</small></td><td><details><summary>${p.evidence_refs.length} sources</summary><div class="analysis-reference-list">${analysisRefs(p.evidence_refs)}</div></details></td></tr>`).join('')||'<tr><td colspan="4">No precisely aligned degradation episodes. Check the summary and evidence coverage.</td></tr>'}</tbody></table></div>${r.episodes_omitted?`<p class="hint">${r.episodes_omitted} further episodes exceed the bounded report; original evidence retains their samples.</p>`:''}`;
}
function analysisComparisons(r){
 return `<h3>Recorded ping paths</h3><div class="network-table"><table data-table="analysis-paths:${e(r.id)}"><thead><tr><th>Path</th><th>Usable attempts / timeouts</th><th>p95 RTT</th><th>Evidence</th></tr></thead><tbody>${(r.probe_summary||[]).map(p=>`<tr><td>${e(p.source)} → ${e(p.target)}</td><td>${p.usable_attempts} / ${p.timeouts}</td><td>${networkNumber(p.p95_ms)} ms</td><td>${analysisRefs(p.evidence_refs)}</td></tr>`).join('')||'<tr><td colspan="4">No probe evidence.</td></tr>'}</tbody></table></div>
 <details class="analysis-comparison-group"><summary>Path differences by episode <span class="tag">${r.path_findings.length}</span></summary>${r.path_findings.map(p=>`<article class="analysis-finding"><h4>Episode ${p.episode_id}</h4>${p.conclusions.map(t=>`<p>${e(t)}</p>`).join('')}<div class="network-table"><table data-table="episode-paths:${e(r.id)}:${e(p.episode_id)}"><thead><tr><th>Path</th><th>Observation</th><th>Evidence</th></tr></thead><tbody>${p.paths.map(x=>`<tr><td>${e(x.source)} → ${e(x.target)}</td><td>${e(x.state)} · ${x.observations} readings</td><td>${analysisRefs(x.evidence_refs)}</td></tr>`).join('')}</tbody></table></div><p class="hint">${e(p.limitation)}</p></article>`).join('')||'<p>No comparable paths were recorded.</p>'}</details>
 <details class="analysis-comparison-group"><summary>Baseline, load and recovery</summary>${r.load_response.map(p=>`<article class="analysis-finding"><h4>${e(p.source)} → ${e(p.target)}</h4><p>${e(p.conclusion)}</p><p class="hint">p95 RTT: ${networkNumber(p.phases.baseline?.p95_ms)} → ${networkNumber(p.phases.load?.p95_ms)} → ${networkNumber(p.phases.recovery?.p95_ms)} ms. Timeouts: ${p.phases.baseline?.timeouts??'—'} → ${p.phases.load?.timeouts??'—'} → ${p.phases.recovery?.timeouts??'—'}.</p>${analysisRefs(p.evidence_refs)}</article>`).join('')||'<p>No controlled baseline/load/recovery comparison is available.</p>'}</details>
 <details class="analysis-comparison-group"><summary>Sender versus receiver delivery</summary>${r.delivery.map(p=>`<p><b>${e(p.direction)}:</b> ${e(p.conclusion)} ${analysisRefs(p.evidence_refs)}</p>`).join('')||'<p>No sender measurements in this ping incident.</p>'}</details>`;
}
function analysisEvidence(r){
 return `<h3>Evidence coverage</h3>${r.quality.length?`<ul class="analysis-quality">${r.quality.map(q=>`<li>${e(q)}</li>`).join('')}</ul>`:'<p>No additional coverage limitations were reported.</p>'}
 <details class="analysis-comparison-group"><summary>What this test can establish</summary><ul>${(r.exclusions||[]).map(q=>`<li>${e(q)}</li>`).join('')}</ul><p>${e(r.conclusion)}</p><p class="hint">Analyzer ${e(r.analyzer_version)} · ${e(r.status)}. Root cause is not established.</p></details>
 <div class="compact-heading"><h3>Referenced measurements</h3>${help('Evidence selection',`${r.evidence_selection.method} ${r.evidence_selection.sample_count} source observations; ${r.evidence_selection.omitted_examples} additional examples omitted. Original-source indexes are local to this report.`)}</div>${Object.entries(r.evidence).map(([key,data])=>`<details class="analysis-evidence" data-ref="${e(key)}"><summary>${e(analysisRefLabel(key))} <small>${e(key)}</small></summary><pre class="code-block">${e(JSON.stringify(data,null,2))}</pre></details>`).join('')}`;
}
function analysisDetail(r){
 const tabs=[['summary','Summary'],['episodes',`Episodes (${r.episode_count})`],['comparisons','Comparisons'],['evidence','Evidence']];
 return `<div class="analysis-review-context"><div><strong>${e(r.context?.schedule_name||r.context?.profile_snapshot?.name||r.server_name)}</strong><span>${e(analysisDate(r.start))} – ${e(analysisDate(r.end))}</span></div><a href="/api${analysisBase(r.server_id)}/${r.source}/${encodeURIComponent(r.source_id)}?download=true">Download conclusion (.json.gz)</a></div>
 ${r.quality.length?`<div class="analysis-coverage-note">${r.quality.length} evidence limitation${r.quality.length===1?'':'s'} ${button('analysis-review-tab','Review coverage','','small ghost','data-review-tab="evidence"')}</div>`:''}
 <div class="analysis-review-tabs" role="tablist" aria-label="Review sections">${tabs.map(([key,label],i)=>`<button type="button" id="analysis-tab-${key}" role="tab" aria-controls="analysis-panel-${key}" aria-selected="${i===0}" tabindex="${i===0?0:-1}" data-action="analysis-review-tab" data-review-tab="${key}">${label}</button>`).join('')}</div>
 <div class="analysis-review-body">${tabs.map(([key])=>`<section id="analysis-panel-${key}" class="analysis-review-panel" role="tabpanel" aria-labelledby="analysis-tab-${key}" tabindex="0" ${key==='summary'?'':'hidden'}>${({summary:analysisOverview,episodes:analysisEpisodes,comparisons:analysisComparisons,evidence:analysisEvidence})[key](r)}</section>`).join('')}</div>`;
}
function selectAnalysisReviewTab(key){
 const root=$('#analysis-review');if(!root||!root.querySelector(`#analysis-tab-${key}`))return;
 for(const tab of root.querySelectorAll('[role=tab]')){const selected=tab.dataset.reviewTab===key;tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;}
 for(const panel of root.querySelectorAll('[role=tabpanel]'))panel.hidden=panel.id!==`analysis-panel-${key}`;
 root.querySelector('.analysis-review-body').scrollTop=0;
}
async function openAnalysisReview(s,el){
 const v=analysisView(s),request=++v.detailRequest;
 modal(el.dataset.source==='call'?'Call-flow conclusion':'Ping incident conclusion','<div id="analysis-review" class="analysis-review"><p role="status">Loading conclusion…</p></div>',true);
 const root=$('#analysis-review');root.closest('.modal').classList.add('analysis-review-modal');
 try{
  const r=await api(`${analysisBase(s.id)}/${el.dataset.source}/${encodeURIComponent(el.dataset.id)}`);
  if(!root.isConnected||v.detailRequest!==request||current()?.id!==s.id||state.tab!=='analysis')return;
  updateHTML(root,analysisDetail(r));
 }catch(error){if(root.isConnected&&v.detailRequest===request)updateHTML(root,`<p class="form-error" role="alert">${e(error.message)}</p>`);}
}
document.addEventListener('keydown',event=>{
 const tab=event.target.closest('#analysis-review [role=tab]');if(!tab||!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
 event.preventDefault();const tabs=[...tab.parentElement.querySelectorAll('[role=tab]')],index=tabs.indexOf(tab);
 const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
 selectAnalysisReviewTab(tabs[next].dataset.reviewTab);tabs[next].focus({preventScroll:true});
});
function analysisContent(s,v){
 const d=v.data,rows=(d?.reports||[]).filter(r=>v.filter==='all'||r.source===v.filter);
 return `<div class="between network-heading"><div><h2>Connection analysis</h2><p>Compare ping, call flows and router evidence. Download conclusions for an independent review in a new session.</p></div></div>
 <div class="analysis-controls"><label>Period<select data-analysis-filter="hours">${[[24,'Last 24 hours'],[168,'Last 7 days'],[720,'Last 30 days'],[8760,'Last year']].map(([k,t])=>`<option value="${k}" ${v.hours===k?'selected':''}>${t}</option>`).join('')}</select></label><label>Show<select data-analysis-filter="source">${[['all','All evidence'],['call','Call tests'],['incident','Ping incidents']].map(([k,t])=>`<option value="${k}" ${v.filter===k?'selected':''}>${t}</option>`).join('')}</select></label>${button('analysis-refresh',v.busy?'Analysing…':'Analyse saved evidence','activity','small primary',v.busy?'disabled':'')}${v.busy?button('analysis-stop','Stop after this batch','','small ghost'):''}${d?.reports.length?`<a href="/api${analysisBase(s.id)}/download?${analysisQuery(v)}">Download all conclusions in period (.json.gz)</a>`:''}</div>
 <p class="hint" role="status">${v.busy?'Analysing saved evidence locally. ':''}${d?`${d.reports.length} saved reports · ${d.pending} awaiting analysis. ${e(d.retention)}`:'Loading saved conclusions…'}</p>${v.error?`<p class="form-error" role="alert">${e(v.error)}</p>`:''}
 ${d?.errors?.map(x=>`<p class="form-error">${e(x.id)}: ${e(x.message)}</p>`).join('')||''}
 <details class="network-notes"><summary>Evidence boundaries and handoff contents</summary><p>The compressed file includes readable conclusions, measured examples, comparison counts, timing limits, method/version, context and content hashes. No external AI service receives data. Host names, IPs and selected client identifiers remain in the evidence.</p><ul>${(d?.exclusions||[]).map(t=>`<li>${e(t)}</li>`).join('')}</ul><p class="hint">${e(d?.scope||'')} Downloads include all analyzed reports in the selected period; the Show filter only changes this list.</p></details>
 <h3>Saved conclusions</h3><div class="network-table"><table data-table="conclusions"><thead><tr><th data-type="date">Time / source</th><th>Conclusion</th><th>Review</th></tr></thead><tbody>${rows.map(r=>`<tr data-key="analysis-row:${e(r.id)}"><td data-value="${e(r.start??'')}">${e(analysisDate(r.start))}<br>${r.source==='call'?'Call test':'Ping incident'} · ${e(r.context?.schedule_name||r.context?.profile_snapshot?.name||r.label||'')}</td><td><b>${e(r.conclusion)}</b><br><span class="hint">${r.episode_count} episodes · ${r.quality.length} evidence limitations</span></td><td>${button('analysis-open','Review','','small',`data-source="${r.source}" data-id="${e(r.source_id)}"`)}</td></tr>`).join('')||'<tr><td colspan="3">No saved conclusions in this selection. Use Analyse saved evidence to process retained tests and sealed incidents.</td></tr>'}</tbody></table></div>
 ${(d?.sources||[]).filter(x=>!x.ready).length?'<p class="hint">Active tests, open incident windows and tests without collected evidence are not treated as healthy comparisons. Collect missing evidence in Call tests.</p>':''}
 <details open><summary>Comparable recurring tests</summary><p class="hint">Only matching recorded settings, observers and route context are grouped. These are retained test results, not whole-day outage rates.</p><div class="network-table"><table data-table="comparisons"><thead><tr><th>Profile / endpoint</th><th>Compared tests</th><th>Directional exposure</th></tr></thead><tbody>${(d?.patterns||[]).map(p=>`<tr><td>${e(p.context.profile_snapshot?.name||p.context.profile)}<br>${e(p.context.endpoint)} · ${p.context.upload_kbps}/${p.context.download_kbps} kbps</td><td>${e(p.conclusion)}<br><small>${p.incomplete_comparisons} incomplete comparisons · ${p.overlapping_tests_excluded} overlapping tests excluded</small></td><td>${['upload','download'].map(k=>`${e(k)}: ${networkNumber(p.directions[k].affected_seconds)} / ${networkNumber(p.directions[k].observed_seconds)} seconds affected`).join('<br>')}</td></tr>`).join('')||'<tr><td colspan="3">No comparable call-test reports yet.</td></tr>'}</tbody></table></div></details>`;
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
 v.request++;v.detailRequest++;v.busy=false;v.hours=Number(event.target.value);v.until=Date.now()/1000;v.data=null;loadConnectionAnalysis(s,true);
});
document.addEventListener('click',async event=>{
 const el=event.target.closest('[data-action]'),s=current();if(!el||!s||!el.dataset.action.startsWith('analysis-'))return;
 const v=analysisView(s),action=el.dataset.action;
 if(action==='analysis-refresh'){v.until=Date.now()/1000;await loadConnectionAnalysis(s,true,true);}
 else if(action==='analysis-stop'){v.request++;v.busy=false;paintAnalysis(s);}
 else if(action==='analysis-review-tab'){selectAnalysisReviewTab(el.dataset.reviewTab);}
 else if(action==='analysis-open'){await openAnalysisReview(s,el); }else if(action==='analysis-evidence'){
  const detail=[...document.querySelectorAll('#analysis-review .analysis-evidence')].find(d=>d.dataset.ref===el.dataset.ref);
  if(detail){selectAnalysisReviewTab('evidence');detail.open=true;const body=detail.closest('.analysis-review-body');body.scrollTop+=detail.getBoundingClientRect().top-body.getBoundingClientRect().top-12;detail.querySelector('summary').focus({preventScroll:true});}
 }
});
