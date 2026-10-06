'use strict';
const resourceMeta = {cpu:{label:'CPU',unit:'%',color:'#2baf91',icon:'cpu'},memory:{label:'Memory',unit:'%',color:'#668bee',icon:'memory'},disk:{label:'Disk',unit:'%',color:'#bc86d6',icon:'disk'},temperature:{label:'CPU / SoC',unit:'°C',color:'#e89548',icon:'temperature'},load:{label:'Load average',unit:'',color:'#54bcca',icon:'activity'}};
const loadKeys=['load1','load5','load15'];
const seriesMeta={...resourceMeta,...Object.fromEntries(loadKeys.map((key,i)=>[key,{label:`Load · ${[1,5,15][i]} min`,short:`${[1,5,15][i]} min`,unit:'',color:['#54bcca','#8d9cf4','#e89548'][i],dash:['','7 4','2 4'][i]}]))};
const resourceSeries=keys=>keys.flatMap(key=>key==='load'?loadKeys:[key]);
const resourceValue=(value,key)=>value==null?'—':loadKeys.includes(key)?value.toFixed(2):percent(value);
const loadLegend=()=>`<div class="load-legend">${loadKeys.map(key=>`<span style="--series:${seriesMeta[key].color}"><i class="${key}"></i>${seriesMeta[key].short}</span>`).join('')}</div>`;
const loadHelp=()=>help('Load average','Load averages describe demand over 1, 5 and 15 minutes. macOS counts runnable tasks; Linux also includes tasks in uninterruptible wait (often I/O). They are not percentages or disk throughput. Compare with the host’s logical CPU count, and with CPU usage, when interpreting demand.');
const hourChoices=[1,3,6,12,24,48,168,336,672,2160,4320,8760,17520];
let cardHours=Number(localStorage.getItem('harbour-card-hours'))||6;
if(!hourChoices.includes(cardHours))cardHours=6;
const cardCache=new Map();
const historyState={server:null,hours:24,resolution:0,mode:'combined',stat:'average',selected:new Set(Object.keys(resourceMeta)),resource:'cpu',disk:'',data:null,sequence:0};
const monitorActions=new Set(['history','history-mode','history-tab','history-refresh','sensor-history','monitoring']);
const monitorForms=new Set(['monitoring-form','server-settings-form']);
const uptime=n=>n==null?'Unavailable':`${Math.floor(n/86400)}d ${Math.floor(n%86400/3600)}h ${Math.floor(n%3600/60)}m`;
const hourOptions=value=>hourChoices.map(h=>`<option value="${h}" ${h===value?'selected':''}>${h<48?count(h,'hour'):h/24+' days'}</option>`).join('');
const resolutionLabel=n=>n<3600?n/60+' min':n<86400?n/3600+' hour'+(n>3600?'s':''):n/86400+' days';
const connectionState=s=>!s.monitoring_enabled?'paused':s.connection_status==='down'?'down':s.stale?'stale':s.connection_status;
function serverConnection(s,withHost=false){
  const status=connectionState(s);
  const label=({up:'UP',down:'DOWN',unknown:'CHECK FAILED',pending:'PENDING',paused:'PAUSED',stale:'STALE'})[status]||'PENDING';
  return `<div class="server-connection"><span class="connection-pill ${e(status)}"><i></i>${label}</span>${withHost?`<span class="card-host mono" title="${e(s.host)}">${e(s.host)}</span><span class="connection-separator">–</span>`:''}<span class="latency" title="Authenticated SSH command round-trip; excludes connection setup. Not ICMP ping.">${s.latency_ms==null?'SSH —':`${percent(s.latency_ms)} ms${withHost?'':' SSH'}`}${status!=='up'&&s.latency_ms!=null?' · last':''}</span></div>`;
}
function resourceCards(s){
  const m=s.metrics,t=s.thresholds,disk=cardDisk(s),temp=m.temperature||{};
  const cpuPeriod=m.cpu==null?'Waiting for next poll':m.cpu_sample_seconds>0?`${duration(Math.round(m.cpu_sample_seconds))} average`:s.stale?'Last known usage':'Host CPU';
  const values={cpu:[m.cpu,`${m.cores??'—'} cores`,cpuPeriod],memory:[m.memory.percent,`${gb(m.memory.used)} / ${gb(m.memory.total)} GB`,'RAM'],disk:[disk?.percent,disk?`${disk.mount} · ${capacity(disk.used)} / ${capacity(disk.total)}`:'No volume selected',disk?`${capacity(disk.free)} free`:''],temperature:[temp.package,temp.package==null?'No CPU / SoC sensor':temp.package_label,temp.package_count>1?`${temp.package_count} packages · first shown`:temp.package==null?'Unavailable':'Selected sensor']};
  return Object.entries(values).map(([key,[value,detail,aside]])=>{
    const meta=resourceMeta[key],warn=key==='disk'?!!disk?.warning:s.warnings.some(w=>key==='temperature'?w.kind==='cpu_package':w.id===key||w.id.startsWith(key+':'));
    return `<article data-key="metric:${key}" class="metric ${warn?'warning':''}"><div class="metric-label">${icon(meta.icon)}${meta.label}<span class="tag ${warn?'red':''}">${warn?'Attention':value==null?'Unavailable':s.stale?'Stale':'Latest'}</span></div><div class="metric-value">${value==null?'—':percent(value)}<span>${value==null?'':meta.unit}</span></div><div class="metric-bottom"><span>${e(detail)}</span><span>${e(aside)}</span></div><button class="card-chart" type="button" data-action="history" data-resource="${key}" aria-label="Explore ${meta.label} history"><span data-card-chart="${key}" data-preserve-children>Loading history…</span></button>${key==='temperature'&&temp.sensors?.length?`<details class="sensor-details"><summary>${count(temp.sensors.length,'sensor')}</summary>${temp.sensors.map(sensor=>`<div class="between ${sensor.kind!=='cpu_auxiliary'&&sensor.celsius>=t.temperature?'sensor-warning':''}"><span>${e(sensor.label)}${sensor.kind==='cpu_auxiliary'?' · separate reading':''}</span><b>${percent(sensor.celsius)}°C</b></div>`).join('')}</details>`:''}</article>`;
  }).join('')+loadResourceCard(s);
}
function loadResourceCard(s){
  const m=s.metrics,available=loadKeys.some(key=>m[key]!=null);
  return `<article data-key="metric:load" class="metric load-metric"><div><div class="metric-label">${icon('activity')}Load average<span class="tag">${!available?'Unavailable':s.stale?'Stale':'Latest'}</span></div><div class="load-values">${loadKeys.map(key=>`<div><small style="color:${seriesMeta[key].color}">${seriesMeta[key].short}</small><b>${resourceValue(m[key],key)}</b></div>`).join('')}</div><div class="metric-bottom">${m.cores??'—'} logical CPUs · system load</div></div><button class="card-chart" type="button" data-action="history" data-resource="load" aria-label="Explore Load average history"><span data-card-chart="load" data-preserve-children>Loading history…</span></button></article>`;
}
const sensorIdentity=s=>JSON.stringify([s.id,s.unit,s.source||'']);
const temperatureReadings=sensors=>(sensors||[]).map(s=>({...s,value:s.celsius,unit:'°C',source:'Temperature sensor'}));
const hardwareReadings=m=>[...temperatureReadings(m?.temperature?.sensors),...(m?.hardware||[])];
const sensorValue=(value,unit)=>value==null?'—':`${Number(value.toFixed(unit==='A'?3:unit==='RPM'||unit==='cycles'?0:2)).toLocaleString()} ${unit}`;
function hardwarePanel(s){
  const readings=hardwareReadings(s.metrics),batteries=s.metrics?.batteries||[];
  return `<details data-key="hardware:${e(s.id)}" class="hardware-panel"><summary>Hardware sensors <span class="tag">${readings.length}${s.stale?' · Stale':''}</span></summary>${batteries.length?`<p class="hint">${batteries.map(b=>`${e(b.name)}: ${e(b.status)}${b.condition&&b.condition!=='Unknown'?' · '+e(b.condition):''}`).join(' · ')}</p>`:''}<p class="hint">Readings depend on the hardware and account permissions. Power channels can overlap; they are not a combined total or wall-plug measurement.</p>${readings.length?`<div class="history-table-wrap"><table class="hardware-table"><thead><tr><th>Sensor</th><th>Reading</th><th>Source</th><th>History</th></tr></thead><tbody>${readings.map(r=>`<tr><td>${e(r.label)}</td><td>${e(sensorValue(r.value,r.unit))}</td><td>${e(r.source)}</td><td>${button('sensor-history','History','activity','small',`data-sensor="${e(sensorIdentity(r))}" aria-label="History for ${e(r.label)}"`)}</td></tr>`).join('')}</tbody></table></div>`:'<p class="hint">No hardware sensors are exposed by this host.</p>'}${button('sensor-history','Explore sensor history','activity','small')}</details>`;
}
function sensorHistoryData(data){
  return {...data,points:data.points.map(p=>{
    const s=[...temperatureReadings(p.sensors),...(p.hardware||[])].find(s=>sensorIdentity(s)===historyState.sensor);
    return {...p,sensor:s?.value??null,sensor_peak:s?.peak??null,
      sample_first:s?.sample_first??p.sample_first,sample_last:s?.sample_last??p.sample_last};
  })};
}
function hardwareHistory(data){
  const known=new Map();
  for(const p of data.points)for(const r of [...temperatureReadings(p.sensors),...(p.hardware||[])])known.set(sensorIdentity(r),r);
  for(const r of hardwareReadings(current()?.metrics))known.set(sensorIdentity(r),r);
  if(!known.size)return '<div class="empty">No sensor readings in this period.</div>';
  if(!known.has(historyState.sensor))historyState.sensor=known.keys().next().value;
  const selected=known.get(historyState.sensor);
  seriesMeta.sensor={label:selected.label,unit:selected.unit,color:'#54bcca'};
  return `<label class="sensor-selector">Sensor<select id="history-sensor">${[...known].map(([key,s])=>`<option value="${e(key)}" ${key===historyState.sensor?'selected':''}>${e(s.label)} (${e(s.unit)}) · ${e(s.source)}</option>`).join('')}</select></label><p class="hint">${e(selected.source)} · ${selected.unit==='J'?'Accumulated energy counter; can wrap or reset. This is not watts.':'Each sensor uses its own units and scale.'}</p>${chartSVG(sensorHistoryData(data),['sensor'])}`;
}

function graphValue(point,key){return point[historyState.stat==='peak'?key+'_peak':key];}
function chartSVG(data,keys,{mini=false}={}){
  keys=resourceSeries(keys);
  if(!data?.points.length)return `<span class="history-empty">No samples in this period</span>`;
  const width=mini?240:900,height=mini?55:310,left=mini?0:55,right=mini?240:840,top=mini?3:30,bottom=mini?52:260;
  const vals=keys.flatMap(k=>data.points.map(p=>mini?p[k]:graphValue(p,k))).filter(n=>n!=null);
  if(!vals.length)return `<span class="history-empty">${keys[0]==='temperature'?'CPU package sensor unavailable':loadKeys.includes(keys[0])?'No load readings in this period':'No successful readings'}</span>`;
  const sensor=keys.includes('sensor'),temps=keys.includes('temperature'),loads=keys.some(k=>loadKeys.includes(k)),percents=keys.some(k=>k!=='temperature'&&k!=='sensor'&&!loadKeys.includes(k));
  const tv=data.points.map(p=>mini?p.temperature:graphValue(p,'temperature')).filter(n=>n!=null);
  const tMin=Math.min(0,Math.floor(Math.min(0,...tv)/10)*10),tMax=Math.max(100,Math.ceil(Math.max(0,...tv)/10)*10);
  const loadMax=Math.max(1,...vals),loadStep=10**Math.floor(Math.log10(loadMax))/2,lMax=Math.ceil(loadMax/loadStep)*loadStep;
  const sMin=Math.min(0,...vals),sMax=Math.max(1,...vals)*1.05;
  const x=time=>left+Math.max(0,Math.min(1,(time-data.from)/(data.to-data.from)))*(right-left);
  const y=(v,k)=>bottom-((v-(k==='sensor'?sMin:k==='temperature'?tMin:0))/(k==='sensor'?sMax-sMin:k==='temperature'?tMax-tMin:loadKeys.includes(k)?lMax:100))*(bottom-top);
  let grid='';
  if(!mini){
    for(let i=0;i<=4;i++){
      const py=top+(bottom-top)*i/4;
      grid+=`<line class="chart-grid" x1="${left}" x2="${right}" y1="${py}" y2="${py}"/>`;
      if(sensor)grid+=`<text x="${left-9}" y="${py+4}" text-anchor="end">${Number((sMax-(sMax-sMin)*i/4).toFixed(2))}</text>`;
      if(percents)grid+=`<text x="${left-9}" y="${py+4}" text-anchor="end">${100-i*25}%</text>`;
      if(loads)grid+=`<text x="${left-9}" y="${py+4}" text-anchor="end">${Number((lMax*(1-i/4)).toFixed(2))}</text>`;
      if(temps)grid+=`<text x="${right+9}" y="${py+4}">${Math.round(tMax-(tMax-tMin)*i/4)}°</text>`;
    }
    for(let i=0;i<=4;i++){
      const stamp=data.from+(data.to-data.from)*i/4,date=new Date(stamp*1000);
      grid+=`<text x="${left+(right-left)*i/4}" y="${bottom+24}" text-anchor="middle">${e(date.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}))}</text>${data.hours>24?`<text x="${left+(right-left)*i/4}" y="${bottom+41}" text-anchor="middle">${e(date.toLocaleDateString([],{month:'short',day:'numeric'}))}</text>`:''}`;
    }
    grid+=`<text x="${left}" y="15">${sensor?e(seriesMeta.sensor.unit):loads?'Load average':percents?'Utilisation (%)':''}</text>${temps?`<text x="${right}" y="15" text-anchor="end">Temperature (°C)</text>`:''}`;
  }
  const pollGap=(data.poll_seconds||current()?.poll_seconds||60)*3.5;
  const legacyGap=Math.max(data.resolution_seconds*1.75,pollGap);
  const lines=keys.map(key=>{
    let segments=[],segment=[],previous=null,previousTimed=false;
    for(const p of data.points){
      const value=mini?p[key]:graphValue(p,key);
      if(value==null)continue;
      const first=p.sample_first??p.time,last=p.sample_last??p.time;
      if(previous!=null&&first-previous>(p.sample_first!=null&&previousTimed?pollGap:legacyGap)){if(segment.length)segments.push(segment);segment=[];}
      segment.push([x(p.time),y(value,key)]);
      previous=last;previousTimed=p.sample_last!=null;
    }
    if(segment.length)segments.push(segment);
    return segments.map(s=>s.length===1?`<circle cx="${s[0][0]}" cy="${s[0][1]}" r="2.5" fill="${seriesMeta[key].color}"/>`:`<polyline fill="none" stroke="${seriesMeta[key].color}" stroke-dasharray="${seriesMeta[key].dash||''}" stroke-width="${mini?2:2.3}" points="${s.map(p=>p.join(',')).join(' ')}"/>`).join('');
  }).join('');
  const svg=`<svg class="history-chart ${mini?'mini-chart':''}" ${mini?'preserveAspectRatio="none"':`data-chart-keys="${keys.join(',')}" tabindex="0"`} viewBox="0 0 ${width} ${height}" role="img" aria-label="${e(keys.map(k=>seriesMeta[k].label).join(', '))} history, ${data.hours} hours, ${resolutionLabel(data.resolution_seconds)} buckets${mini?'':'. Hover, tap or use left and right arrows to inspect readings.'}">${grid}${lines}${mini?'':'<line class="chart-cursor" x1="55" x2="55" y1="30" y2="260" visibility="hidden"/>'}</svg>`;
  return mini?svg:`<div class="history-plot">${svg}<div class="chart-tooltip" role="status" hidden></div></div>`;
}
function showChartReading(svg,time){
  const keys=svg.dataset.chartKeys.split(',');
  const data=keys.includes('sensor')&&historyState.data?sensorHistoryData(historyState.data):historyState.data;if(!data?.points.length)return;
  const nearest=data.points.reduce((a,b)=>Math.abs(b.time-time)<Math.abs(a.time-time)?b:a);
  const point=Math.abs(nearest.time-time)<=data.resolution_seconds/2?nearest:{time:Math.round(time/data.resolution_seconds)*data.resolution_seconds};
  svg.dataset.inspectTime=point.time;
  const x=55+Math.max(0,Math.min(1,(point.time-data.from)/(data.to-data.from)))*785;
  const line=$('.chart-cursor',svg);line.setAttribute('x1',x);line.setAttribute('x2',x);line.setAttribute('visibility','visible');
  const bubble=$('.chart-tooltip',svg.parentElement);bubble.hidden=false;
  updateHTML(bubble,`<b>${e(new Date(point.time*1000).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'}))}</b><small>${resolutionLabel(data.resolution_seconds)} · ${historyState.stat==='peak'?'Peaks':'Averages'}</small>${keys.map(key=>`<div><span style="color:${seriesMeta[key].color}">${e(seriesMeta[key].label)}</span><strong>${graphValue(point,key)==null?'No sample':e(key==='sensor'?sensorValue(graphValue(point,key),seriesMeta[key].unit):resourceValue(graphValue(point,key),key)+seriesMeta[key].unit)}</strong></div>`).join('')}`);
  const rect=svg.getBoundingClientRect(),plot=svg.parentElement.getBoundingClientRect();
  const location=new DOMPoint(x,30).matrixTransform(svg.getScreenCTM());
  bubble.style.left=Math.max(4,Math.min(location.x-plot.left+12,plot.width-bubble.offsetWidth-4))+'px';
  bubble.style.top=Math.max(0,rect.top-plot.top)+'px';
}
function hideChartReading(svg){$('.chart-cursor',svg)?.setAttribute('visibility','hidden');const bubble=$('.chart-tooltip',svg.parentElement);if(bubble)bubble.hidden=true;}
function inspectChart(event){
  const svg=event.target.closest('[data-chart-keys]');if(!svg)return;
  const point=new DOMPoint(event.clientX,event.clientY).matrixTransform(svg.getScreenCTM().inverse());
  if(point.x<55||point.x>840||point.y<30||point.y>260){hideChartReading(svg);return;}
  const data=historyState.data;if(data)showChartReading(svg,data.from+(point.x-55)/785*(data.to-data.from));
}
document.addEventListener('pointermove',inspectChart);
document.addEventListener('pointerdown',inspectChart);
document.addEventListener('pointerout',event=>{const svg=event.target.closest('[data-chart-keys]');if(svg&&!svg.contains(event.relatedTarget)&&event.pointerType!=='touch')hideChartReading(svg);});
document.addEventListener('focusout',event=>{if(event.target.matches('[data-chart-keys]'))hideChartReading(event.target);});
document.addEventListener('keydown',event=>{
  const svg=event.target.closest('[data-chart-keys]'),data=historyState.data;if(!svg||!data?.points.length)return;
  if(event.key==='Escape'){event.stopImmediatePropagation();hideChartReading(svg);return;}
  if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
  event.preventDefault();const points=data.points,index=points.findIndex(p=>p.time===Number(svg.dataset.inspectTime));
  const next=event.key==='Home'?0:event.key==='End'?points.length-1:Math.max(0,Math.min(points.length-1,index+(event.key==='ArrowRight'?1:-1)));
  showChartReading(svg,points[next].time);
},true);
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
function drawCardHistory(data){document.querySelectorAll('[data-card-chart]').forEach(el=>{updateHTML(el,chartSVG(data,[el.dataset.cardChart],{mini:true})+`<small>${data.hours}h · ${resolutionLabel(data.resolution_seconds)} averages</small>`);});}
function openHistory(resource,sensor){
  historyState.server=current().id;historyState.disk='';historyState.data=null;
  if(resource){historyState.mode='tabs';historyState.resource=resource;}
  if(sensor!==undefined){historyState.mode='sensors';historyState.sensor=sensor;}
  modal(`${e(current().name)} · Resource history`,`<div id="history-explorer"></div>`,true);
  $('.modal').classList.add('history-modal');renderHistory();fetchHistory();
}
function loadHistoryChart(data){
  return `<section class="load-history"><div class="load-history-heading"><h3>Load average ${loadHelp()}</h3>${loadLegend()}</div>${chartSVG(data,['load'])}</section>`;
}
function combinedHistoryChart(data,keys){
  const other=keys.filter(key=>key!=='load');
  if(!keys.includes('load'))return chartSVG(data,other);
  return other.length?`<div class="history-combined">${chartSVG(data,other)}${loadHistoryChart(data)}</div>`:loadHistoryChart(data);
}
function renderHistory(){
  const root=$('#history-explorer');if(!root)return;
  const h=historyState,data=h.data,keys=[...h.selected];
  root.innerHTML=`<div class="history-controls"><label>Time window<select id="history-hours">${hourOptions(h.hours)}</select></label><label>Display resolution<select id="history-resolution">${[0,60,300,600,900,1800,3600,10800,21600,43200,86400,604800].map(r=>`<option value="${r}" ${r===h.resolution?'selected':''}>${r?resolutionLabel(r):'Automatic'}</option>`).join('')}</select></label><label>Values<select id="history-stat"><option value="average" ${h.stat==='average'?'selected':''}>Averages</option><option value="peak" ${h.stat==='peak'?'selected':''}>Peaks</option></select></label><label>Disk volume<select id="history-disk"><option value="">Card selection</option>${[...new Set([...(current()?.metrics?.disks||[]).map(d=>d.mount),...(data?.disk_mounts||[])])].map(m=>`<option value="${e(m)}" ${m===h.disk?'selected':''}>${e(m)}</option>`).join('')}</select></label>${button('history-refresh','Refresh','refresh','small')}</div>
    <div class="history-modes segmented" aria-label="History layout">${[['tabs','Resource tabs'],['table','Table'],['side','Side by side'],['combined','Combined'],['sensors','Hardware sensors']].map(([mode,name])=>button('history-mode',name,'',h.mode===mode?'active':'',`data-mode="${mode}"`)).join('')}</div>
    ${h.mode==='sensors'?'':h.mode==='tabs'?`<div class="resource-legend">${Object.entries(resourceMeta).map(([k,m])=>button('history-tab',m.label,m.icon,h.resource===k?'active':'ghost',`data-resource="${k}"`)).join('')}</div>`:`<div class="resource-legend">${Object.entries(resourceMeta).map(([k,m])=>`<label style="--series:${m.color}"><input type="checkbox" data-history-resource="${k}" ${h.selected.has(k)?'checked':''}><i></i>${m.label}${m.unit?' ('+m.unit+')':''}</label>`).join('')}</div>`}
    <p class="hint history-caption">${data?`${data.hours} hours · actual resolution <b>${resolutionLabel(data.resolution_seconds)}</b> · ${count(data.points.length,'bucket')} · times in ${e(Intl.DateTimeFormat().resolvedOptions().timeZone)}.${data.requested_resolution&&data.resolution_seconds>data.requested_resolution?' Stored resolution or the 2,000-point display limit requires coarser buckets.':''}`:'Loading stored readings…'}</p>
    <div class="history-content">${!data?'<div class="empty">Loading history…</div>':h.mode==='sensors'?hardwareHistory(data):h.mode==='tabs'?h.resource==='load'?loadHistoryChart(data):`<h3>${resourceMeta[h.resource].label}</h3>${chartSVG(data,[h.resource])}`:!keys.length?'<div class="empty">Select at least one resource.</div>':h.mode==='table'?historyTable(data,keys):h.mode==='side'?`<div class="history-grid">${keys.map(k=>`<article><h3>${icon(resourceMeta[k].icon)}${resourceMeta[k].label}</h3>${k==='load'?loadLegend():''}${chartSVG(data,[k])}</article>`).join('')}</div>`:combinedHistoryChart(data,keys)}</div>
    <p class="hint">${data?.demo?'Synthetic demo history. ':''}${h.mode==='sensors'?'Hardware history starts when readings become available. ':h.mode==='tabs'&&h.resource==='load'?`Load uses a task-count scale · ${current()?.metrics?.cores??'—'} logical CPUs on this host.`:`Disk: ${e(h.disk||'highest usage among volumes selected for cards')}; CPU temperature uses ${e(data?.temperature_source||'the package sensor')}. Load uses a separate task-count scale.`} Lines bridge up to two missed polls; longer gaps remain. Missing values are excluded from averages. Peaks remain available after consolidation.</p><div class="form-error" role="alert"></div>`;
}
function historyTable(data,keys){
  keys=resourceSeries(keys);
  if(!data.points.length)return '<div class="empty">No readings in this window.</div>';
  return `<div class="history-table-wrap" tabindex="0" aria-label="Scrollable resource readings"><table class="history-table"><thead><tr><th>Bucket start</th>${keys.map(k=>`<th>${seriesMeta[k].label}${seriesMeta[k].unit?' ('+seriesMeta[k].unit+')':''}</th>`).join('')}<th>Disk used / free</th><th>SSH (ms)</th><th>Samples / checks</th></tr></thead><tbody>${[...data.points].reverse().map(p=>`<tr><td>${e(new Date(p.time*1000).toLocaleString())}</td>${keys.map(k=>`<td>${resourceValue(graphValue(p,k),k)}</td>`).join('')}<td>${p.disks.map(d=>`<div>${e(d.mount)}: ${capacity(d.used_gb*1e9)} / ${capacity(d.free_gb*1e9)}</div>`).join('')||'—'}</td><td>${p.latency_ms==null?'—':percent(p.latency_ms)}</td><td>${p.samples} / ${p.attempts}</td></tr>`).join('')}</tbody></table></div>`;
}
async function fetchHistory(){
  const seq=++historyState.sequence,{server,hours,resolution,disk}=historyState;
  try{const data=await api(`/servers/${server}/history?hours=${hours}&resolution=${resolution}${disk?'&disk='+encodeURIComponent(disk):''}`);if(seq!==historyState.sequence||!$('#history-explorer'))return;historyState.data=data;renderHistory();}
  catch(error){if($('#history-explorer'))formError(error.message);}
}
function volumeSettingsTable(s){
  const size=n=>n==null?'—':(n/1e9).toLocaleString(undefined,{maximumSignificantDigits:4});
  return `<div class="volume-table-wrap" tabindex="0" aria-label="Volume settings"><table class="volume-settings"><thead><tr><th scope="col">Volume path</th><th scope="col">Total GB</th><th scope="col">Free GB</th><th scope="col">Monitor</th><th scope="col">Warn</th><th scope="col">Use in cards</th></tr></thead><tbody>${(s.metrics?.disks||[]).map(d=>`<tr data-volume="${e(d.mount)}"><th scope="row" class="mono" title="${e(d.mount)}${d.present?'':' · Not mounted'}">${e(d.mount)}</th><td title="${e(d.present?capacity(d.total):'Not mounted')}">${size(d.total)}</td><td title="${e(d.present?capacity(d.free):'Not mounted')}">${size(d.free)}</td>${['monitor','warn','card'].map(key=>`<td><input type="checkbox" data-volume-option="${key}" aria-label="${key==='card'?'Use in cards':key==='warn'?'Warn':'Monitor'} ${e(d.mount)}" ${d[key]?'checked':''} ${key!=='monitor'&&!d.monitor?'disabled':''}></td>`).join('')}</tr>`).join('')}</tbody></table></div>${!s.metrics?.disks?.length?'<p class="hint">Volumes appear after a successful check.</p>':''}`;
}
function serverSettings(s){
  const mode=s.override?'custom':'global';
  const fields=[['cpu','CPU %',1,100],['memory','Memory %',1,100],['disk','Disk %',1,100],['disk_free_gb','Free space GB',0,1000000],['temperature','Temperature °C',1,180]];
  modal(`<span class="server-settings-name">${e(s.name)}</span><input name="name" aria-label="Server name" value="${e(s.name)}" maxlength="80" required hidden>${iconButton('edit-server-name','Edit server name','pencil')}<span class="settings-context">settings</span>`,
    `<form id="server-settings-form" data-id="${e(s.id)}">
      <section class="settings-section"><div class="settings-section-heading"><h3>${fieldCaption('Warning thresholds','Global inherits the workspace limits. Custom sets limits for this server. Disk warnings trigger on either usage or free space for volumes with Warn enabled. CPU uses the average between successful polls; memory uses the latest reading. CPU temperature uses package / SoC sensors; other device sensors warn separately.')}</h3><input type="hidden" name="threshold_mode" value="${mode}">${slidingControl('threshold-mode','Threshold source',[['global','Global'],['custom','Custom']],mode,'settings-choice','data-field="threshold_mode"')}</div>
      <div class="threshold-row">${fields.map(([key,label,min,max])=>`<label>${label}<input name="${key}" type="number" min="${min}" max="${max}" step="0.1" value="${s.thresholds[key]}" required ${s.override?'':'disabled'}></label>`).join('')}</div></section>
      <section class="settings-section settings-checks"><div class="settings-type"><h3>${fieldCaption('Server type','Plain servers collect resources only. Docker hosts also discover containers, check images and support Docker actions.')}</h3><input type="hidden" name="server_type" value="${s.server_type}">${slidingControl('settings-server-type','Server type',[['docker','Docker host'],['plain','Plain server']],s.server_type,'settings-choice','data-field="server_type"')}</div>
      <label class="settings-poll">${fieldCaption('Poll interval (s)','Leave blank to inherit the global interval. Checks never overlap on a host; pausing keeps history and permits manual refresh.')}<input aria-label="Polling interval (seconds)" name="poll_seconds" type="number" min="15" max="3600" placeholder="Global" title="Current effective interval: ${s.poll_seconds}s" value="${s.poll_override??''}"></label><label class="check-line settings-enabled"><input name="enabled" type="checkbox" ${s.monitoring_enabled?'checked':''}>Monitoring enabled</label></section>
      <section class="settings-section settings-volumes"><div class="settings-section-heading"><h3>${fieldCaption('Volumes','Monitor stores this volume’s history. Warn enables disk thresholds. Use in cards selects the disk in the resource card and sidebar; if several are selected, the highest percentage is shown. Existing history is retained when monitoring is disabled. Capacities use decimal GB.')}</h3></div>${volumeSettingsTable(s)}</section>
      <div class="form-error" role="alert"></div><div class="form-actions settings-save">${button('remove-server','Remove server','trash','danger ghost small',`title="Remove from Harbour; services on the host are unaffected"`)}<button class="primary" type="submit">Save</button></div>
    </form>`);
  const form=$('#server-settings-form'),header=$('.modal-header');
  form.prepend(header); // Keep the editable title inside the same saved/guarded form.
  const close=$('[data-action=close]',header);
  close.before(buttonElement('edit-connection','SSH connection','key','small'));
  $('.modal').classList.add('server-settings-modal');
}
function buttonElement(action,label,ico,cls){const template=document.createElement('template');template.innerHTML=button(action,label,ico,cls);return template.content.firstElementChild;}
function setServerSettingsChoice(el){
  const form=$('#server-settings-form');if(!form||form.dataset.busy)return;
  const field=el.dataset.field,value=el.dataset.value,input=form.elements[field];
  if(input.value===value)return;
  if(field==='threshold_mode'){
    const inputs=[...form.querySelectorAll('.threshold-row input')];
    if(input.value==='custom')form.thresholdDraft=Object.fromEntries(inputs.map(i=>[i.name,i.value]));
    for(const i of inputs){i.disabled=value==='global';i.value=value==='global'?state.data.thresholds[i.name]:(form.thresholdDraft?.[i.name]??i.value);}
  }
  input.value=value;updateSlidingControl(el.closest('.sliding-control'),value);
}
async function monitoringSettings(){
  const p=await api('/monitoring');
  const resolutionSelect=(label,key,options)=>`<label>${label}<select name="${key}">${options.map(n=>`<option value="${n}" ${p[key]===n?'selected':''}>${n} minutes</option>`).join('')}</select></label>`;
  modal('Monitoring & history',`<p>Checks continue while nobody is signed in. SQLite keeps sample-weighted averages and peaks; older readings are consolidated in the background.</p><form id="monitoring-form"><div class="form-grid"><label>Default polling (seconds)<input name="poll_seconds" type="number" min="15" max="3600" value="${p.poll_seconds}" required></label><label>Image update checks (hours)<input name="update_check_hours" type="number" min="1" max="168" value="${p.update_check_hours}" required></label>${resolutionSelect('First 7 days','week1_minutes',[1,5,10,15])}${resolutionSelect('Days 8–14','week2_minutes',[5,15,30,60])}${resolutionSelect('Days 15–28','weeks3_4_minutes',[15,30,60])}${resolutionSelect('After 28 days','older_minutes',[60,180,360])}<label>Keep history (days)<input name="retention_days" type="number" min="1" max="730" value="${p.retention_days}" required></label></div><div class="info-box"><p><b>Recommended:</b> 60s checks; 1 → 5 → 15 → 60 minute storage; keep 90 days. For a small VPS: 300s checks and 5-minute initial storage. Keep 180–365 days if seasonal trends matter.</p></div><p class="hint">Older tiers must be multiples of the previous tier. Finer settings cannot restore previously consolidated data. Shortening retention permanently removes older history at the next hourly sweep. Allocated space is reused by SQLite.</p><p class="hint">${p.history_rows.toLocaleString()} stored buckets · ${(p.db_bytes/1048576).toFixed(1)} MiB database (${(p.reusable_bytes/1048576).toFixed(1)} MiB reusable).</p><div class="form-error" role="alert"></div><div class="form-actions"><button type="submit" class="primary">Save monitoring policy</button></div></form>`,true);
}
async function handleMonitorAction(action,el){
  if(action==='history')openHistory(el.dataset.resource);
  if(action==='sensor-history')openHistory(null,el.dataset.sensor||'');
  if(action==='history-mode'){historyState.mode=el.dataset.mode;renderHistory();}
  if(action==='history-tab'){historyState.resource=el.dataset.resource;renderHistory();}
  if(action==='history-refresh')await fetchHistory();
  if(action==='monitoring')await monitoringSettings();
}
async function handleMonitorForm(form,data){
  if(form.id==='server-settings-form'){
    const volumes=[...form.querySelectorAll('[data-volume]')].map(row=>({mount:row.dataset.volume,...Object.fromEntries(['monitor','warn','card'].map(key=>[key,row.querySelector('[data-volume-option='+key+']').checked]))}));
    const thresholds=data.threshold_mode==='global'?null:Object.fromEntries(['cpu','memory','disk','disk_free_gb','temperature'].map(key=>[key,Number(data[key])]));
    const saved=await api(`/servers/${form.dataset.id}/settings`,'PUT',{name:data.name,server_type:data.server_type,thresholds,volumes,enabled:data.enabled==='on',poll_seconds:data.poll_seconds?Number(data.poll_seconds):null});
    form.elements.name.value=saved.name;form.elements.name.hidden=true;$('.server-settings-name',form).textContent=saved.name;$('.server-settings-name',form).hidden=false;$('[data-action=edit-server-name]',form).hidden=false;
    markFormSaved(form);cardCache.clear();state.selected.clear();await load();toast('Server settings saved');
  }

  if(form.id==='monitoring-form'){await api('/monitoring','PUT',Object.fromEntries(Object.entries(data).map(([k,v])=>[k,Number(v)])));markFormSaved(form);await load();toast('Monitoring and retention saved');}
}
document.addEventListener('change',async event=>{
  const el=event.target;
  if(el.id==='history-disk'){historyState.disk=el.value;await fetchHistory();}
  if(el.dataset.volumeOption==='monitor'){const row=el.closest('[data-volume]');for(const key of ['warn','card']){const input=row.querySelector('[data-volume-option='+key+']');input.disabled=!el.checked;if(!el.checked)input.checked=false;}}
  if(el.id==='card-hours'){cardHours=Number(el.value);localStorage.setItem('harbour-card-hours',cardHours);await loadCardHistory(current(),true);}
  if(el.id==='history-hours'||el.id==='history-resolution'){historyState[el.id==='history-hours'?'hours':'resolution']=Number(el.value);historyState.data=null;renderHistory();await fetchHistory();}
  if(el.id==='history-sensor'){historyState.sensor=el.value;renderHistory();}
  if(el.id==='history-stat'){historyState.stat=el.value;renderHistory();}
  if(el.dataset.historyResource){el.checked?historyState.selected.add(el.dataset.historyResource):historyState.selected.delete(el.dataset.historyResource);renderHistory();}
});
start();
setInterval(()=>{if(state.user&&!document.hidden)load();},5000);
