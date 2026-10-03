'use strict';
const resourceMeta = {cpu:{label:'CPU',unit:'%',color:'#2baf91',icon:'cpu'},memory:{label:'Memory',unit:'%',color:'#668bee',icon:'memory'},disk:{label:'Disk',unit:'%',color:'#bc86d6',icon:'disk'},temperature:{label:'CPU package',unit:'°C',color:'#e89548',icon:'temperature'}};
const hourChoices=[1,3,6,12,24,48,168,336,672,2160,4320,8760,17520];
let cardHours=Number(localStorage.getItem('harbour-card-hours'))||6;
if(!hourChoices.includes(cardHours))cardHours=6;
const cardCache=new Map();
const historyState={server:null,hours:24,resolution:0,mode:'combined',stat:'average',selected:new Set(Object.keys(resourceMeta)),resource:'cpu',data:null,sequence:0};
const monitorActions=new Set(['history','history-mode','history-tab','history-refresh','monitoring']);
const monitorForms=new Set(['monitoring-form','server-monitoring-form']);
const uptime=n=>n==null?'Unavailable':`${Math.floor(n/86400)}d ${Math.floor(n%86400/3600)}h ${Math.floor(n%3600/60)}m`;
const hourOptions=value=>hourChoices.map(h=>`<option value="${h}" ${h===value?'selected':''}>${h<48?h+' hours':h/24+' days'}</option>`).join('');
const resolutionLabel=n=>n<3600?n/60+' min':n<86400?n/3600+' hour'+(n>3600?'s':''):n/86400+' days';
const connectionState=s=>!s.monitoring_enabled?'paused':s.connection_status==='down'?'down':s.stale?'stale':s.connection_status;
function serverConnection(s,withHost=false){
  const status=connectionState(s);
  const label=({up:'UP',down:'DOWN',unknown:'CHECK FAILED',pending:'PENDING',paused:'PAUSED',stale:'STALE'})[status]||'PENDING';
  return `<div class="server-connection"><span class="connection-pill ${e(status)}"><i></i>${label}</span>${withHost?`<span class="card-host mono" title="${e(s.host)}">${e(s.host)}</span><span class="connection-separator">–</span>`:''}<span class="latency" title="Authenticated SSH command round-trip; excludes connection setup. Not ICMP ping.">${s.latency_ms==null?'SSH —':`${percent(s.latency_ms)} ms${withHost?'':' SSH'}`}${status!=='up'&&s.latency_ms!=null?' · last':''}</span></div>`;
}
function resourceCards(s){
  const m=s.metrics,t=s.thresholds,disk=m.disks.reduce((a,b)=>!a||b.percent>a.percent?b:a,null),temp=m.temperature||{};
  const values={cpu:[m.cpu,`${m.cores??'—'} cores`,s.stale?'Last known usage':'Host CPU'],memory:[m.memory.percent,`${gb(m.memory.used)} / ${gb(m.memory.total)} GB`,'RAM'],disk:[disk?.percent,disk?`${gb(disk.used)} / ${gb(disk.total)} GB`:'No filesystem data',disk?`${gb(disk.free)} GB free`:''],temperature:[temp.package,temp.package==null?'No package sensor':temp.package_label,temp.package_count>1?`${temp.package_count} packages · first shown`:temp.package==null?'Unavailable':'Package reading']};
  return Object.entries(values).map(([key,[value,detail,aside]])=>{
    const meta=resourceMeta[key],warn=s.warnings.some(w=>key==='temperature'?w.kind==='cpu_package':w.id===key||w.id.startsWith(key+':'));
    return `<article class="metric ${warn?'warning':''}"><div class="metric-label">${icon(meta.icon)}${meta.label}<span class="tag ${warn?'red':''}">${warn?'Attention':value==null?'Unavailable':s.stale?'Stale':'Latest'}</span></div><div class="metric-value">${value==null?'—':percent(value)}<span>${value==null?'':meta.unit}</span></div><div class="metric-bottom"><span>${e(detail)}</span><span>${e(aside)}</span></div><button class="card-chart" type="button" data-action="history" data-resource="${key}" aria-label="Explore ${meta.label} history"><span data-card-chart="${key}">Loading history…</span></button>${key==='temperature'&&temp.sensors?.length?`<details class="sensor-details"><summary>${count(temp.sensors.length,'sensor')}</summary>${temp.sensors.map(sensor=>`<div class="between ${sensor.kind!=='cpu_auxiliary'&&sensor.celsius>=t.temperature?'sensor-warning':''}"><span>${e(sensor.label)}${sensor.kind==='cpu_auxiliary'?' · separate reading':''}</span><b>${percent(sensor.celsius)}°C</b></div>`).join('')}</details>`:''}</article>`;
  }).join('');
}
function graphValue(point,key){return point[historyState.stat==='peak'?key+'_peak':key];}
function chartSVG(data,keys,{mini=false}={}){
  if(!data?.points.length)return `<span class="history-empty">No samples in this period</span>`;
  const width=mini?240:900,height=mini?55:310,left=mini?0:55,right=mini?240:840,top=mini?3:30,bottom=mini?52:260;
  const vals=keys.flatMap(k=>data.points.map(p=>mini?p[k]:graphValue(p,k))).filter(n=>n!=null);
  if(!vals.length)return `<span class="history-empty">${keys[0]==='temperature'?'CPU package sensor unavailable':'No successful readings'}</span>`;
  const temps=keys.includes('temperature'),percents=keys.some(k=>k!=='temperature');
  const tv=data.points.map(p=>mini?p.temperature:graphValue(p,'temperature')).filter(n=>n!=null);
  const tMin=Math.min(0,Math.floor(Math.min(0,...tv)/10)*10),tMax=Math.max(100,Math.ceil(Math.max(0,...tv)/10)*10);
  const x=time=>left+Math.max(0,Math.min(1,(time-data.from)/(data.to-data.from)))*(right-left);
  const y=(v,k)=>bottom-((v-(k==='temperature'?tMin:0))/((k==='temperature'?tMax-tMin:100)))*(bottom-top);
  let grid='';
  if(!mini){
    for(let i=0;i<=4;i++){
      const py=top+(bottom-top)*i/4;
      grid+=`<line class="chart-grid" x1="${left}" x2="${right}" y1="${py}" y2="${py}"/>`;
      if(percents)grid+=`<text x="${left-9}" y="${py+4}" text-anchor="end">${100-i*25}%</text>`;
      if(temps)grid+=`<text x="${right+9}" y="${py+4}">${Math.round(tMax-(tMax-tMin)*i/4)}°</text>`;
    }
    for(let i=0;i<=4;i++){
      const stamp=data.from+(data.to-data.from)*i/4,date=new Date(stamp*1000);
      grid+=`<text x="${left+(right-left)*i/4}" y="${bottom+24}" text-anchor="middle">${e(date.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}))}</text>${data.hours>24?`<text x="${left+(right-left)*i/4}" y="${bottom+41}" text-anchor="middle">${e(date.toLocaleDateString([],{month:'short',day:'numeric'}))}</text>`:''}`;
    }
    grid+=`<text x="${left}" y="15">${percents?'Utilisation (%)':''}</text>${temps?`<text x="${right}" y="15" text-anchor="end">Temperature (°C)</text>`:''}`;
  }
  const gap=Math.max(data.resolution_seconds, (current()?.poll_seconds||60))*1.75;
  const lines=keys.map(key=>{
    let segments=[],segment=[],previous=null;
    for(const p of data.points){
      const value=mini?p[key]:graphValue(p,key);
      if(value==null||previous!=null&&p.time-previous>gap){if(segment.length)segments.push(segment);segment=[];}
      if(value!=null)segment.push([x(p.time),y(value,key)]);
      previous=p.time;
    }
    if(segment.length)segments.push(segment);
    return segments.map(s=>s.length===1?`<circle cx="${s[0][0]}" cy="${s[0][1]}" r="2.5" fill="${resourceMeta[key].color}"/>`:`<polyline fill="none" stroke="${resourceMeta[key].color}" stroke-width="${mini?2:2.3}" points="${s.map(p=>p.join(',')).join(' ')}"/>`).join('');
  }).join('');
  return `<svg class="history-chart ${mini?'mini-chart':''}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${e(keys.map(k=>resourceMeta[k].label).join(', '))} history, ${data.hours} hours, ${resolutionLabel(data.resolution_seconds)} buckets">${grid}${lines}</svg>`;
}
async function loadCardHistory(s,force=false){
  const key=s.id+':'+cardHours,cached=cardCache.get(key);
  if(!force&&cached&&Date.now()-cached.loaded<30000){drawCardHistory(cached.data);return;}
  if(cached?.pending)return;
  cardCache.set(key,{...cached,pending:true});
  try{
    const data=await api(`/servers/${s.id}/history?hours=${cardHours}`);
    cardCache.set(key,{data,loaded:Date.now(),pending:false});
    if(current()?.id===s.id&&key===s.id+':'+cardHours)drawCardHistory(data);
  }catch(error){cardCache.delete(key);if(current()?.id===s.id)document.querySelectorAll('[data-card-chart]').forEach(el=>{el.textContent='History unavailable';});}
}
function drawCardHistory(data){document.querySelectorAll('[data-card-chart]').forEach(el=>{el.innerHTML=chartSVG(data,[el.dataset.cardChart],{mini:true})+`<small>${data.hours}h · ${resolutionLabel(data.resolution_seconds)} averages</small>`;});}
function openHistory(resource){
  historyState.server=current().id;historyState.data=null;
  if(resource){historyState.mode='tabs';historyState.resource=resource;}
  modal(`${e(current().name)} · Resource history`,`<div id="history-explorer"></div>`,true);
  $('.modal').classList.add('history-modal');renderHistory();fetchHistory();
}
function renderHistory(){
  const root=$('#history-explorer');if(!root)return;
  const h=historyState,data=h.data,keys=[...h.selected];
  root.innerHTML=`<div class="history-controls"><label>Time window<select id="history-hours">${hourOptions(h.hours)}</select></label><label>Display resolution<select id="history-resolution">${[0,60,300,600,900,1800,3600,10800,21600,43200,86400,604800].map(r=>`<option value="${r}" ${r===h.resolution?'selected':''}>${r?resolutionLabel(r):'Automatic'}</option>`).join('')}</select></label><label>Values<select id="history-stat"><option value="average" ${h.stat==='average'?'selected':''}>Averages</option><option value="peak" ${h.stat==='peak'?'selected':''}>Peaks</option></select></label>${button('history-refresh','Refresh','refresh','small')}</div>
    <div class="history-modes segmented" aria-label="History layout">${[['tabs','Resource tabs'],['table','Table'],['side','Side by side'],['combined','Combined']].map(([mode,name])=>button('history-mode',name,'',h.mode===mode?'active':'',`data-mode="${mode}"`)).join('')}</div>
    ${h.mode==='tabs'?`<div class="resource-legend">${Object.entries(resourceMeta).map(([k,m])=>button('history-tab',m.label,m.icon,h.resource===k?'active':'ghost',`data-resource="${k}"`)).join('')}</div>`:`<div class="resource-legend">${Object.entries(resourceMeta).map(([k,m])=>`<label style="--series:${m.color}"><input type="checkbox" data-history-resource="${k}" ${h.selected.has(k)?'checked':''}><i></i>${m.label}${k==='temperature'?' (°C)':' (%)'}</label>`).join('')}</div>`}
    <p class="hint history-caption">${data?`${data.hours} hours · actual resolution <b>${resolutionLabel(data.resolution_seconds)}</b> · ${count(data.points.length,'bucket')} · times in ${e(Intl.DateTimeFormat().resolvedOptions().timeZone)}.${data.requested_resolution&&data.resolution_seconds>data.requested_resolution?' Stored resolution or the 2,000-point display limit requires coarser buckets.':''}`:'Loading stored readings…'}</p>
    <div class="history-content">${!data?'<div class="empty">Loading history…</div>':h.mode==='tabs'?`<h3>${resourceMeta[h.resource].label}</h3>${chartSVG(data,[h.resource])}`:!keys.length?'<div class="empty">Select at least one resource.</div>':h.mode==='table'?historyTable(data,keys):h.mode==='side'?`<div class="history-grid">${keys.map(k=>`<article><h3>${icon(resourceMeta[k].icon)}${resourceMeta[k].label}</h3>${chartSVG(data,[k])}</article>`).join('')}</div>`:chartSVG(data,keys)}</div>
    <p class="hint">${data?.demo?'Synthetic demo history. ':''}Disk shows the busiest monitored filesystem; CPU temperature uses ${e(data?.temperature_source||'the package sensor')}. Missing readings leave gaps. Peaks remain available after consolidation.</p><div class="form-error" role="alert"></div>`;
}
function historyTable(data,keys){
  if(!data.points.length)return '<div class="empty">No readings in this window.</div>';
  return `<div class="history-table-wrap" tabindex="0" aria-label="Scrollable resource readings"><table class="history-table"><thead><tr><th>Bucket start</th>${keys.map(k=>`<th>${resourceMeta[k].label} (${resourceMeta[k].unit})</th>`).join('')}<th>Disk used / free (GB)</th><th>SSH (ms)</th><th>Samples / checks</th></tr></thead><tbody>${[...data.points].reverse().map(p=>`<tr><td>${e(new Date(p.time*1000).toLocaleString())}</td>${keys.map(k=>`<td>${graphValue(p,k)==null?'—':percent(graphValue(p,k))}</td>`).join('')}<td>${p.disks.map(d=>`<div>${e(d.mount)}: ${percent(d.used_gb)} / ${percent(d.free_gb)}</div>`).join('')||'—'}</td><td>${p.latency_ms==null?'—':percent(p.latency_ms)}</td><td>${p.samples} / ${p.attempts}</td></tr>`).join('')}</tbody></table></div>`;
}
async function fetchHistory(){
  const seq=++historyState.sequence,{server,hours,resolution}=historyState;
  try{const data=await api(`/servers/${server}/history?hours=${hours}&resolution=${resolution}`);if(seq!==historyState.sequence||!$('#history-explorer'))return;historyState.data=data;renderHistory();}
  catch(error){if($('#history-explorer'))formError(error.message);}
}
function serverMonitoringForm(s){return `<hr class="section-rule"><h3>Server checks</h3><form id="server-monitoring-form" data-id="${e(s.id)}"><label class="check-line"><input name="enabled" type="checkbox" ${s.monitoring_enabled?'checked':''}>Background monitoring enabled</label><label>Polling interval (seconds)<input name="poll_seconds" type="number" min="15" max="3600" placeholder="Use global interval" value="${s.poll_override??''}"></label><p class="hint">Leave blank to inherit. Effective interval: ${s.poll_seconds}s. A slower host can use 300s; checks never overlap on one host. Pausing keeps history and permits manual refresh.</p><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary" type="submit">Save server checks</button></div></form>`;}
async function monitoringSettings(){
  const p=await api('/monitoring');
  const resolutionSelect=(label,key,options)=>`<label>${label}<select name="${key}">${options.map(n=>`<option value="${n}" ${p[key]===n?'selected':''}>${n} minutes</option>`).join('')}</select></label>`;
  modal('Monitoring & history',`<p>Checks continue while nobody is signed in. SQLite keeps sample-weighted averages and peaks; older readings are consolidated in the background.</p><form id="monitoring-form"><div class="form-grid"><label>Default polling (seconds)<input name="poll_seconds" type="number" min="15" max="3600" value="${p.poll_seconds}" required></label><label>Image update checks (hours)<input name="update_check_hours" type="number" min="1" max="168" value="${p.update_check_hours}" required></label>${resolutionSelect('First 7 days','week1_minutes',[1,5,10,15])}${resolutionSelect('Days 8–14','week2_minutes',[5,15,30,60])}${resolutionSelect('Days 15–28','weeks3_4_minutes',[15,30,60])}${resolutionSelect('After 28 days','older_minutes',[60,180,360])}<label>Keep history (days)<input name="retention_days" type="number" min="1" max="730" value="${p.retention_days}" required></label></div><div class="info-box"><p><b>Recommended:</b> 60s checks; 1 → 5 → 15 → 60 minute storage; keep 90 days. For a small VPS: 300s checks and 5-minute initial storage. Keep 180–365 days if seasonal trends matter.</p></div><p class="hint">Older tiers must be multiples of the previous tier. Finer settings cannot restore previously consolidated data. Shortening retention permanently removes older history at the next hourly sweep. Allocated space is reused by SQLite.</p><p class="hint">${p.history_rows.toLocaleString()} stored buckets · ${(p.db_bytes/1048576).toFixed(1)} MiB database (${(p.reusable_bytes/1048576).toFixed(1)} MiB reusable).</p><div class="form-error" role="alert"></div><div class="form-actions"><button type="submit" class="primary">Save monitoring policy</button></div></form>`,true);
}
async function handleMonitorAction(action,el){
  if(action==='history')openHistory(el.dataset.resource);
  if(action==='history-mode'){historyState.mode=el.dataset.mode;renderHistory();}
  if(action==='history-tab'){historyState.resource=el.dataset.resource;renderHistory();}
  if(action==='history-refresh')await fetchHistory();
  if(action==='monitoring')await monitoringSettings();
}
async function handleMonitorForm(form,data){
  if(form.id==='monitoring-form'){await api('/monitoring','PUT',Object.fromEntries(Object.entries(data).map(([k,v])=>[k,Number(v)])));closeOverlay();await load();toast('Monitoring and retention saved');}
  if(form.id==='server-monitoring-form'){await api(`/servers/${form.dataset.id}/monitoring`,'PUT',{enabled:data.enabled==='on',poll_seconds:data.poll_seconds?Number(data.poll_seconds):null});closeOverlay();await load();toast('Server checks saved');}
}
document.addEventListener('change',async event=>{
  const el=event.target;
  if(el.id==='card-hours'){cardHours=Number(el.value);localStorage.setItem('harbour-card-hours',cardHours);await loadCardHistory(current(),true);}
  if(el.id==='history-hours'||el.id==='history-resolution'){historyState[el.id==='history-hours'?'hours':'resolution']=Number(el.value);historyState.data=null;renderHistory();await fetchHistory();}
  if(el.id==='history-stat'){historyState.stat=el.value;renderHistory();}
  if(el.dataset.historyResource){el.checked?historyState.selected.add(el.dataset.historyResource):historyState.selected.delete(el.dataset.historyResource);renderHistory();}
});
start();
setInterval(()=>{if(state.user&&!document.hidden)load();},5000);
