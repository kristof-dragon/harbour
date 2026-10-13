// Invented device identity and documentation addresses; no real site data.
// Synthetic OpenWRT UI: no live router, network probes or configuration changes.
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {chromium}=require('playwright');
const now=Date.now()/1000,thresholds={cpu:85,memory:85,disk:85,disk_free_gb:10,temperature:80};
const board={model:'Example MT7621 router',system:'MediaTek MT7621',kernel:'6.6.0',release:{description:'OpenWrt 24.10.0 r00000-example',version:'24.10.0'}};
const router={board,profile:'mt7621-mt76',capabilities:{wireless:true,queues:true,ethernet_details:false},
 interfaces:{wan:{rx_mbps:10,tx_mbps:2}},network:[{interface:'wan',l3_device:'wan'}],
 stations:[{mac:'aa:bb:cc:dd:ee:ff',interface:'phy1-ap0',signal_dbm:-53,tx_mbps:433,rx_mbps:200,tx_retries_delta:3,tx_failed_delta:0}],
 cpu_percent:{cpu:25,cpu0:35},softirq_percent:{cpu0:10},queue_stats:[{device:'wan',discipline:'fq_codel',backlog_bytes:2000},{device:'example-tunnel',discipline:'noqueue',backlog_bytes:0}],queues:'qdisc fq_codel 0: dev wan\n backlog 2000b 2p',routes:'default via 192.0.2.1 dev wan',rules:'32766: from all lookup main',wireless:'Interface phy1-ap0\n channel 36',surveys:{}};
const host={id:'router',name:'Example router',host:'192.0.2.2',port:22,username:'root',server_type:'openwrt',services:[],warnings:[],updates:0,checked:now,monitoring_enabled:true,poll_seconds:60,record_seconds:60,disk_seconds:300,inventory_seconds:300,connection_status:'up',thresholds,recording:{mode:'openwrt'},metrics:{model:board.model,architecture:board.system,os:board.release.description,kernel:board.kernel,cpu:25,cpu_sample_seconds:1,cores:4,uptime:1716000,measured_at:now,memory:{percent:20,total:256e6,used:51.2e6},disks:[],temperature:{sensors:[]},hardware:[],openwrt:router}};
const settings={enabled:true,interval:.25,targets:['1.1.1.1'],router_probes:true,wan_device:'wan',client_mac:'',latency_limit_ms:150,retention_hours:168,max_rows:0,max_payload_mib:1024,archive_enabled:true,archive_retention_days:90,archive_max_mib:4096,incident_max_count:200,incident_max_mib:128,buffer_mib:128};
const archives={directory:'/data/network-archives',file_count:2,bytes:300000,files:[{id:'today',day:Math.floor(now/86400)*86400,sample_count:1000,size:100000,complete:0},{id:'yesterday',day:Math.floor(now/86400)*86400-86400,sample_count:2000,size:200000,complete:1}]};
const rows=Array.from({length:120},(_,i)=>[{at:now-120+i,kind:'telemetry',metrics:{...host.metrics,openwrt:router}},{at:now-120+i,kind:'probe',source:'recorder',target:'1.1.1.1',status:i===60?'timeout':'reply',rtt_ms:i===60?null:20+i%10}]).flat();
const incidentRows=rows.map(row=>({...row,run_id:'example-run'}));
incidentRows.push({at:now-60,kind:'log',run_id:'example-run',line:'kernel: link down <img src=x onerror="window.incidentInjection=true">'});
const incidentInput={schema:1,server_id:'router',server_name:'Example router',incident:{id:'event',measured:now-60,label:'Video degraded',automatic:1,complete:1},samples:incidentRows,settings,runs:[{id:'example-run',config:settings}]};
const generated=require('node:child_process').spawnSync(process.env.HARBOUR_TEST_PYTHON||'.venv/bin/python',['-c','import json,sys; from harbour.incidents import analyze; from harbour.incident_brief import export; b=json.load(sys.stdin); print(json.dumps(dict(report=analyze(b), briefs={f+str(n)+str(r).lower():export(b,f,n,r) for f in ("markdown","json") for n in (1,3,6) for r in (True,False)})))'],{input:JSON.stringify(incidentInput),encoding:'utf8'});
assert.equal(generated.status,0,generated.stderr);
const {report:incidentReport,briefs:incidentBriefs}=JSON.parse(generated.stdout);
const diagnosticGenerated=require('node:child_process').spawnSync(process.env.HARBOUR_TEST_PYTHON||'.venv/bin/python',['-c',`import sys,json,time
sys.path.insert(0,'tests')
from test_connection_analysis import fixture,result
from harbour.network_diagnostics import summarize
from harbour.connection_analysis import patterns
t,e=fixture(120,losses={'upload':list(range(0,120,3))})
shift=time.time()-1200
for row in e['local']['samples']: row['at']+=shift
for row in e['analysis']['timeline']:
 row['start']+=shift;row['end']+=shift
for key in ('start','end'): e['window'][key]+=shift
for key in ('load_started','load_finished'): t['result'][key]+=shift
r=result(t,e);r['server_id']='router'
r['quality'].append('<img src=x onerror="window.analysisInjection=true">')
print(json.dumps({'detail':r,'summary':summarize(r),'patterns':patterns([r])}))`],{encoding:'utf8'});
assert.equal(diagnosticGenerated.status,0,diagnosticGenerated.stderr);
const diagnostic=JSON.parse(diagnosticGenerated.stdout);
let diagnosticPrepared=false,delayDiagnostic=false,releaseDiagnostic;
const diagnosticListing=()=>({reports:diagnosticPrepared?[diagnostic.summary]:[],sources:[],pending:diagnosticPrepared?0:1,errors:[],patterns:diagnosticPrepared?diagnostic.patterns:[],retention:'Up to 200 reports / 128 MiB.',exclusions:diagnostic.detail.exclusions,scope:'Retained tests only.'});
let briefFailure=false,delayBrief=false,releaseBrief;
const incidentReports=Array.from({length:23},(_,i)=>({...incidentReport,incident:{...incidentReport.incident,id:i?`event-${i}`:'event',measured:now-60-i*1000,automatic:i<21?1:0}}));
let delayIncident=false,releaseIncident;
const callProfiles=[{id:'facetime',name:'FaceTime · provisional video',upload_kbps:2000,download_kbps:2000,basis:'Harbour approximation',note:'Not an Apple specification.',source:'https://support.apple.com/guide/security/facetime-security-seca331c55cd/web'},{id:'teams-meeting',name:'Teams · video meeting',upload_kbps:2500,download_kbps:4000,basis:'Microsoft recommended video bandwidth',note:'Asymmetric meeting profile',source:'https://learn.microsoft.com/en-us/microsoftteams/prepare-network'},{id:'zoom-meeting',name:'Zoom · group 720p',upload_kbps:2600,download_kbps:1800,basis:'Zoom recommended 720p bandwidth',note:'Group video',source:'https://support.zoom.com/'}];
let liveTick=1,liveRequests=0;
const callData={schedules:[],profiles:callProfiles,available:true,tests:[]};
let callRequest=null,callStopped=false,endpointRequest=null,deployRequest=null;
const remoteHost={...host,id:'remote',name:'Remote Docker host',host:'192.0.2.10',server_type:'docker'};
const endpointData={endpoints:[]};
let saved=null,markers=[],scheduleRequest=null;
const server=http.createServer((req,res)=>{try{const file=path.join(process.cwd(),'harbour/static',req.url==='/'?'index.html':req.url.replace('/static/',''));res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html');res.end(fs.readFileSync(file));}catch{res.writeHead(404).end();}});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({headless:true,executablePath:process.env.HARBOUR_TEST_BROWSER||(fs.existsSync(chromium.executablePath())?chromium.executablePath():'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/**',async route=>{
   const url=new URL(route.request().url()),method=route.request().method();let data;
   if(url.pathname==='/api/config')data={demo:false,version:'test'};
   else if(url.pathname==='/api/me')data={id:'admin',name:'admin',role:'admin',csrf:'test'};
   else if(url.pathname==='/api/dashboard')data={servers:[host],thresholds,jobs:[]};
   else if(url.pathname==='/api/session/activity')data={ok:true};
   else if(url.pathname==='/api/call-endpoints')data=endpointData;
   else if(url.pathname==='/api/call-endpoints/deploy'&&method==='POST'){
    endpointRequest=route.request().postDataJSON();
    endpointData.endpoints=[{id:'remote',server_id:'remote',endpoint:endpointRequest.endpoint,port:endpointRequest.port,http_port:endpointRequest.http_port,deployed:true}];
    await route.fulfill({contentType:'application/x-ndjson',body:[{kind:'started',id:'endpoint-fixture'},{kind:'output',text:'Building endpoint…\n'},{kind:'result',ok:true,endpoint_id:'remote',message:'Endpoint confirmed and reachable.'}].map(x=>JSON.stringify(x)).join('\n')+'\n'});return;
   }
   else if(url.pathname==='/api/call-endpoints/remote/logs')data=url.searchParams.has('day')?{files:[{name:'upload-fixture.jsonl',bytes:200}]}:{days:['26-10-13']};
   else if(url.pathname.endsWith('/call-tests/load-example/analysis'))data={test:callData.tests[0],analysis:{summary:'Receiver impairment was recorded.',root_cause:'not_established',clock_aligned:true,remote_matches:['upload','download'],directions:[{direction:'upload',remote_match:true,summary:{lost_percent:2,jitter_ms:7}},{direction:'download',remote_match:true,summary:{lost_percent:0,jitter_ms:3}}],findings:[{kind:'observation',text:'Upload receiver measured 2.00% UDP packet loss.'}],paths:[],timeline:[],quality:['Fixture evidence'],method:'Run IDs and cookies.'}};
   else if(url.pathname.endsWith('/network/analysis/download')){
    await route.fulfill({contentType:'application/gzip',headers:{'Content-Disposition':'attachment; filename="harbour-connection-analysis.json.gz"'},body:require('node:zlib').gzipSync(JSON.stringify({reports:[diagnostic.detail]}))});return;
   }
   else if(url.pathname.includes('/network/analysis/call/')){
    if(delayDiagnostic)await new Promise(resolve=>{releaseDiagnostic=resolve;});
    data=diagnostic.detail;
   }
   else if(url.pathname.endsWith('/network/analysis')){
    if(url.searchParams.get('prepare')==='true')diagnosticPrepared=true;
    data=diagnosticListing();
   }
   else if(url.pathname.endsWith('/history'))data={points:[],hours:6,from:now-21600,to:now,resolution_seconds:60,poll_seconds:60};
   else if(url.pathname.endsWith('/network/samples'))data={start:now-120,end:now,samples:rows,truncated:false};
   else if(url.pathname.endsWith('/network/markers')){markers.push(route.request().postDataJSON());data={id:'new-event'};}
   else if(url.pathname.endsWith('/network/incidents')){
    const kind=url.searchParams.get('kind'),reports=incidentReports.filter(r=>kind==='automatic'?r.incident.automatic:kind==='manual'?!r.incident.automatic:true);
    data={reports,errors:[],total:reports.length,offset:0,limit:200,has_more:false,patterns:{incident_count:reports.length,window_clusters:reports.length,note:'Overlapping windows are grouped; these are marker counts, not outage rates.',patterns:reports.length?[{key:'finding:probe_degradation',title:'Recorder probe degradation',incidents:reports.length,window_clusters:reports.length,incident_ids:reports.map(r=>r.incident.id)}]:[]}};
    if(url.searchParams.get('download')==='true'){
     await route.fulfill({status:200,contentType:'application/gzip',headers:{'Content-Disposition':'attachment; filename="harbour-incident-patterns.json.gz"'},body:require('node:zlib').gzipSync(JSON.stringify(data))});return;
    }
   }else if(url.pathname.endsWith('/brief')){
    if(delayBrief)await new Promise(resolve=>{releaseBrief=resolve;});
    if(briefFailure){await route.fulfill({status:422,json:{detail:'Evidence unavailable for brief'}});return;}
    data=incidentBriefs[url.searchParams.get('format')+url.searchParams.get('examples')+url.searchParams.get('include_review')];
   }else if(url.pathname.includes('/network/incidents/')){
    if(delayIncident)await new Promise(resolve=>{releaseIncident=resolve;});
    data={...incidentReport,incident:{...incidentReport.incident,id:url.pathname.split('/').at(-1)}};
   }
   else if(url.pathname.endsWith('/network/call-tests/live')){
    liveRequests++;const running=callData.tests.find(t=>t.status==='running')||null;
    if(running?.phase==='loading')running.live={upload:{bits_per_second:1000000,observed_at:Date.now()/1000},upload_receiver:{sender:false,lost_percent:liveTick,jitter_ms:7,bits_per_second:1000000,stale:false},download:{sender:false,lost_percent:liveTick/2,jitter_ms:3,bits_per_second:2000000,observed_at:Date.now()/1000}};
    const filter=url.searchParams.get('schedule_id');
    data={...callData,tests:filter?callData.tests.filter(t=>t.config.schedule_id===filter):callData.tests,running};
   }
   else if(url.pathname.endsWith('/network/call-tests/schedules')&&method==='POST'){scheduleRequest=route.request().postDataJSON();const row={id:'schedule-'+(callData.schedules.length+1),name:scheduleRequest.name,enabled:true,config:scheduleRequest.test,interval_seconds:scheduleRequest.interval_minutes*60,next_run:now+7200};callData.schedules.push(row);data=row;}
   else if(url.pathname.includes('/network/call-tests/schedules/')&&method==='PUT'){
    const row=callData.schedules.find(r=>r.id===url.pathname.split('/').at(-1)),body=route.request().postDataJSON();
    Object.assign(row,{name:body.name,config:body.test,enabled:body.enabled,interval_seconds:body.interval_minutes*60});data=row;
   }
   else if(url.pathname.includes('/network/call-tests/schedules/')&&url.pathname.endsWith('/start')){
    const row=callData.schedules.find(r=>r.id===url.pathname.split('/').at(-2));
    const test={id:'scheduled-example',created:Date.now()/1000,status:'running',phase:'loading',config:{...row.config,remote_live:true,schedule_id:row.id,schedule_name:row.name,source:'schedule-now',profile_snapshot:callProfiles.find(p=>p.id===row.config.profile)},result:{}};
    callData.tests.unshift(test);row.last_test=test.id;data={id:test.id};
   }
   else if(url.pathname.includes('/network/call-tests/schedules/')&&method==='DELETE'){callData.schedules=callData.schedules.filter(r=>r.id!==url.pathname.split('/').at(-1));data={ok:true};}
   else if(url.pathname.endsWith('/network/call-tests')&&method==='POST'){
    callRequest=route.request().postDataJSON();callData.tests=[{id:'load-example',created:now,status:'running',phase:'baseline',config:{...callRequest,profile_snapshot:callProfiles.find(p=>p.id===callRequest.profile)},result:{}}];data={id:'load-example'};
   }else if(url.pathname.includes('/network/call-tests/')&&url.pathname.endsWith('/stop')&&method==='POST'){
    callStopped=true;callData.tests.find(t=>t.id===url.pathname.split('/').at(-2)).status='cancelled';data={ok:true};
   }
   else if(url.pathname.endsWith('/network')){
    if(method==='PUT'){saved=route.request().postDataJSON();Object.assign(settings,saved);data={ok:true};}
    else data={settings,archives,call_tests:callData,status:{state:'recording',recorder_online:true,observer:'onsite-server',probe_errors:{},buffer_bytes:1048576,buffer_limit_bytes:settings.buffer_mib*1048576,buffer_samples:12,oldest_unsaved_seconds:1.2,probe_online:true,router_collector_online:true,timestamp_modes:{example:'kernel_receive'}},device:router,events:[{id:'event',measured:now-60,label:'Video degraded',complete:1}]};
   }else throw Error('Unexpected API '+url.pathname);
   await route.fulfill({json:data});
  });
  await page.goto('http://127.0.0.1:'+server.address().port);
  await page.getByText('Example MT7621 router',{exact:true}).first().waitFor();
  await page.locator('.network-chart svg').first().waitFor();
  assert.equal(await page.locator('[data-tab=containers],[data-tab=logins]').count(),0);
  assert.match(await page.locator('.network-view').textContent(),/ethernet details: unavailable/);
  assert.equal(await page.locator('.network-chart svg').count(),5);
  assert.equal(await page.locator('.probe-card').count(),2);
  assert.equal(await page.locator('[data-tab=wireless],[data-tab=incidents],[data-tab=calls],[data-tab=archives]').count(),4);
  assert.match(await page.locator('.network-view').textContent(),/Memory buffer: 1.0 \/ 128 MiB/);
  assert.match(await page.locator('.network-view').textContent(),/Uncertain timing/);
  assert.doesNotMatch(await page.locator('.network-legend').allTextContents().then(x=>x.join(' ')),/example-tunnel/);
  await page.locator('[data-tab=analysis]').click();
  await page.getByText('No saved conclusions in this selection.',{exact:false}).waitFor();
  await page.getByRole('button',{name:'Analyse saved evidence',exact:true}).click();
  await page.getByRole('button',{name:'Review',exact:true}).waitFor();
  await page.getByRole('button',{name:'Review',exact:true}).click();
  await page.locator('.analysis-detail').waitFor();
  assert.match(await page.locator('.analysis-detail').textContent(),/Call-flow impairment with normal endpoint ping/);
  assert.equal(await page.evaluate(()=>window.analysisInjection),undefined);
  assert.equal(await page.locator('.analysis-detail .network-table').first().locator('tbody tr').count(),20);
  await page.locator('[data-action=analysis-page][data-step="1"]').click();
  assert.match(await page.locator('.analysis-detail').textContent(),/21–40 of 40/);
  assert.equal(await page.locator('[data-action=analysis-page]').count(),2);
  assert.equal(await page.locator('.analysis-detail .probe-card').first().locator('[data-action=analysis-evidence]').count(),4);
  await page.locator('[data-action=analysis-evidence]').first().click();
  assert(await page.locator('.analysis-evidence[open]').count()>0);
  const downloadWait=page.waitForEvent('download');
  await page.getByRole('link',{name:'Download all conclusions in period (.json.gz)',exact:true}).click();
  const handoff=await downloadWait;
  assert.equal(handoff.suggestedFilename(),'harbour-connection-analysis.json.gz');
  assert.equal(JSON.parse(require('node:zlib').gunzipSync(fs.readFileSync(await handoff.path()))).reports.length,1);
  await page.evaluate(()=>document.querySelector('.connection-analysis').scrollIntoView({block:'start'}));
  fs.mkdirSync('test-results',{recursive:true});
  await page.screenshot({path:'test-results/connection-analysis-desktop.png'});
  await page.setViewportSize({width:390,height:844});
  await page.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth);
  await page.waitForFunction(()=>document.querySelector('#sidebar').getBoundingClientRect().right<=0);
  await page.evaluate(()=>document.querySelector('.connection-analysis').scrollIntoView({block:'start'}));
  await page.screenshot({path:'test-results/connection-analysis-mobile.png'});
  await page.setViewportSize({width:1440,height:1000});
  // A late detail response must not restore a report after changing period.
  delayDiagnostic=true;
  await page.getByRole('button',{name:'Review',exact:true}).click();
  while(!releaseDiagnostic)await new Promise(resolve=>setTimeout(resolve,10));
  await page.locator('[data-analysis-filter=hours]').selectOption('24');
  const detailResponse=page.waitForResponse(r=>r.url().includes('/network/analysis/call/'));
  releaseDiagnostic();delayDiagnostic=false;await detailResponse;
  await page.waitForFunction(()=>document.querySelector('.analysis-detail')===null);
  assert.equal(await page.locator('.analysis-detail').count(),0);
  await page.locator('[data-tab=calls]').click();
  await page.getByRole('button',{name:'Remote call flow test',exact:true}).click();
  // No saved Linux server: the test overlay itself starts one-shot deployment.
  await page.locator('#endpoint-form').waitFor();
  assert.equal(await page.locator('#endpoint-form [name=host]').count(),0);
  await page.locator('#endpoint-form [name=endpoint]').fill('192.0.2.50');
  await page.locator('#endpoint-form [name=username]').fill('dockeruser');
  await page.locator('#endpoint-form [name=password]').fill('one-shot-browser-fixture');
  await page.locator('#endpoint-form details summary').click();
  const deploymentPort=Number(await page.locator('#endpoint-form [name=http_port]').inputValue());assert(deploymentPort>=20000&&deploymentPort<60000);
  await page.locator('#endpoint-form [name=http_port]').fill('25432');
  fs.mkdirSync('test-results',{recursive:true});
  // Capture only after clearing the fixture password field.
  await page.locator('#endpoint-form [name=password]').fill('');
  await page.screenshot({path:'test-results/remote-endpoint-deploy.png'});
  await page.locator('#endpoint-form [name=password]').fill('one-shot-browser-fixture');
  await page.getByRole('button',{name:'Deploy',exact:true}).click();
  await page.getByRole('button',{name:'Continue to call test',exact:true}).waitFor();
  assert.equal(await page.locator('#call-test-form').count(),0);
  assert.equal(await page.locator('#endpoint-form [name=password]').inputValue(),'');
  assert.equal(await page.getByRole('button',{name:'Deploy',exact:true}).isDisabled(),true);
  assert.match(await page.getByRole('link',{name:'Download output',exact:true}).getAttribute('href'),/^\/api\/install-artifacts\/.+\/download$/);
  await page.screenshot({path:'test-results/remote-endpoint-completed.png'});
  await page.getByRole('button',{name:'Continue to call test',exact:true}).click();
  await page.locator('#call-test-form').waitFor();
  assert.match(await page.locator('#call-profile-note').textContent(),/Not an Apple specification/);
  await page.locator('#call-test-form [name=profile]').selectOption('teams-meeting');
  assert.equal(await page.locator('#call-test-form [name=upload_kbps]').inputValue(),'2500');
  assert.equal(await page.locator('#call-test-form [name=download_kbps]').inputValue(),'4000');
  fs.mkdirSync('test-results',{recursive:true});
  await page.screenshot({path:'test-results/call-profile-desktop.png'});
  await page.setViewportSize({width:390,height:844});
  await page.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.screenshot({path:'test-results/call-profile-mobile.png'});
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('#call-test-form [name=profile]').selectOption('zoom-meeting');
  assert.equal(await page.locator('#call-test-form [name=download_kbps]').inputValue(),'1800');
  assert.equal(await page.locator('#call-test-form [name=endpoint]').inputValue(),'192.0.2.50');
  assert.equal(endpointRequest.password,'one-shot-browser-fixture');
  assert.equal(endpointRequest.http_port,25432);
  assert.equal(await page.locator('.collector-sudo-form').count(),0);
  await page.locator('#call-test-form [name=duration]').fill('30');
  assert.match(await page.locator('#call-test-estimate').textContent(),/16.5 MB/);
  await page.getByRole('button',{name:'Start traffic test',exact:true}).click();
  await page.getByRole('button',{name:'Stop test',exact:true}).waitFor();
  assert.equal(callRequest.profile,'zoom-meeting');assert.equal(callRequest.download_kbps,1800);
  await page.getByRole('button',{name:'Stop test',exact:true}).click();
  await page.getByRole('button',{name:'Remote call flow test',exact:true}).waitFor();
  assert.equal(callStopped,true);
  await page.getByRole('button',{name:'Add recurring test',exact:true}).click();
  await page.locator('#call-test-form [name=duration]').fill('3600');
  await page.locator('#call-test-form [name=interval_minutes]').fill('120');
  await page.getByRole('button',{name:'Save schedule',exact:true}).click();
  await page.getByRole('button',{name:'Remove',exact:true}).waitFor();
  assert.equal(scheduleRequest.test.duration,3600);assert.equal(scheduleRequest.interval_minutes,120);
  assert.equal(scheduleRequest.test.endpoint_id,'remote');
  await page.getByRole('button',{name:'Remove',exact:true}).click();


  // Multiple schedules remain independently editable; starting a paused one runs once.
  for(const [name,profile] of [['Morning','facetime'],['Afternoon','zoom-meeting']]){
    await page.getByRole('button',{name:'Add recurring test',exact:true}).click();
    await page.locator('#call-test-form [name=schedule_name]').fill(name);
    await page.locator('#call-test-form [name=profile]').selectOption(profile);
    await page.locator('#call-test-form [name=duration]').fill('30');
    await page.locator('#call-test-form [name=quiet]').selectOption('30');
    await page.locator('#call-test-form [name=recovery]').selectOption('120');
    await page.getByRole('button',{name:'Save schedule',exact:true}).click();
    await page.getByRole('heading',{name:'Recurring tests',exact:true}).waitFor();
  }
  assert.equal(callData.schedules.length,2);
  const morning=()=>page.locator('.call-schedules tr').filter({hasText:'Morning'});
  await morning().getByRole('button',{name:'Edit',exact:true}).click();
  assert.equal(await page.locator('#call-test-form [name=schedule_name]').inputValue(),'Morning');
  assert.equal(await page.locator('#call-test-form [name=quiet]').inputValue(),'30');
  assert.equal(await page.locator('#call-test-form [name=recovery]').inputValue(),'120');
  await page.locator('#call-test-form [name=profile]').selectOption('teams-meeting');
  await page.getByRole('button',{name:'Save schedule',exact:true}).click();
  await page.waitForFunction(()=>[...document.querySelectorAll('.call-schedules tr')].some(r=>r.textContent.includes('Morning')&&r.textContent.includes('Teams · video meeting')));
  assert.equal(callData.schedules[1].config.profile,'zoom-meeting');
  assert.equal(callData.schedules[0].config.baseline,30);assert.equal(callData.schedules[0].config.recovery,120);
  await morning().getByRole('button',{name:'Pause',exact:true}).click();
  await morning().getByRole('button',{name:'Resume',exact:true}).waitFor();
  const nextBefore=callData.schedules[0].next_run;
  await morning().getByRole('button',{name:'Start now',exact:true}).click();
  await page.locator('[data-key="live-upload"] [data-key=loss]').getByText('1.00%',{exact:true}).waitFor();
  assert.equal(callData.schedules[0].enabled,false);assert.equal(callData.schedules[0].next_run,nextBefore);
  await page.locator('#call-results-filter').focus();
  await page.evaluate(()=>{
    window.liveNodes=['.network-view','.call-tests','.call-live','[data-key="live-upload"]','[data-key="live-upload"] [data-key=loss]','.call-schedules','#call-results-filter','.call-results'].map(s=>document.querySelector(s));
    window.liveFocus=document.activeElement;window.liveScroll=scrollY;
  });
  liveTick=4;const beforeRequests=liveRequests;
  await page.locator('[data-key="live-upload"] [data-key=loss]').getByText('4.00%',{exact:true}).waitFor({timeout:12000});
  assert(liveRequests>beforeRequests);
  assert.equal(await page.evaluate(()=>window.liveNodes.every(n=>n.isConnected)&&document.activeElement===window.liveFocus&&scrollY===window.liveScroll),true);
  await page.screenshot({path:'test-results/call-schedules-live-desktop.png'});
  // Background polling must not reset edits or change the active run's snapshot.
  await morning().getByRole('button',{name:'Edit',exact:true}).click();
  await page.locator('#call-test-form [name=schedule_name]').fill('Morning edited');
  await page.locator('#call-test-form [name=profile]').selectOption('facetime');
  const editRequests=liveRequests;
  await page.waitForFunction(()=>document.querySelector('.call-live [data-key="live-phase"]')?.textContent.includes('seconds elapsed'));
  await new Promise(resolve=>setTimeout(resolve,5500));
  assert(liveRequests>editRequests);
  assert.equal(await page.locator('#call-test-form [name=schedule_name]').inputValue(),'Morning edited');
  await page.getByRole('button',{name:'Save schedule',exact:true}).click();
  await page.getByRole('button',{name:'Stop test',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.call-live')?.hidden===true);
  assert.equal(callData.tests[0].config.profile,'teams-meeting');
  await morning().getByRole('button',{name:'Results',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.call-results tbody tr').length===1);
  assert.match(await page.locator('.call-results').textContent(),/Teams · video meeting/);
  assert.equal(await page.locator('#call-results-filter').inputValue(),callData.schedules[0].id);
  await page.setViewportSize({width:390,height:844});
  await page.waitForFunction(()=>document.querySelector('.sidebar').getBoundingClientRect().right<=1);
  await page.locator('.call-schedules').scrollIntoViewIfNeeded();
  await page.screenshot({path:'test-results/call-schedules-mobile.png'});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.setViewportSize({width:1440,height:1000});
  await morning().getByRole('button',{name:'Remove',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.call-schedules tbody tr').length===1);
  assert.equal(callData.schedules[0].name,'Afternoon');
  await page.locator('#call-results-filter').selectOption('');
  await page.waitForFunction(()=>document.querySelectorAll('.call-results tbody tr').length===2);
  // Keep the existing analysis fixture aimed at its original manual test.
  callData.tests=callData.tests.filter(t=>t.id==='load-example');

  const sparse=await page.evaluate(()=>{
   const end=Date.now()/1000,start=end-120;
   const render=points=>new DOMParser().parseFromString(networkChart('Sparse readings','ms',[{name:'Example target',maxGap:1.5,points}],start,end),'text/html');
   const isolated=render([[end-110,10],[end-100,20],[end-10,0]]);
   const broken=render([[end-3,3],[end-2.5,4],[end-1,null],[end-.5,2]]);
   return {dots:isolated.querySelectorAll('.network-point').length,lines:isolated.querySelectorAll('polyline').length,brokenLines:broken.querySelectorAll('polyline').length,brokenDots:broken.querySelectorAll('.network-point').length,missing:render([[end-1,null]]).querySelectorAll('svg').length};
  });
  assert.deepEqual(sparse,{dots:3,lines:0,brokenLines:1,brokenDots:1,missing:0});
  await page.locator('[data-tab=archives]').click();
  assert.equal(await page.getByRole('link',{name:'Download .jsonl.gz',exact:true}).getAttribute('href'),'/api/servers/router/network/archives/yesterday');
  assert.match(await page.locator('.network-view').textContent(),/Collecting · available after midnight/);
  await page.getByRole('button',{name:'Mark degradation',exact:true}).click();
  await page.waitForTimeout(100);assert.equal(markers[0].label,'Call degradation');
  await page.getByRole('button',{name:'Recording settings',exact:true}).click();
  await page.locator('[name=interval]').selectOption('0.5');
  await page.locator('[name=client_mac]').fill('aa:bb:cc:dd:ee:ff');
  await page.locator('[name=buffer_mib]').fill('256');
  await page.locator('[name=retention_hours]').fill('720');
  await page.locator('[name=max_payload_mib]').fill('0');
  await page.locator('[name=archive_retention_days]').fill('180');
  await page.locator('[name=archive_max_mib]').fill('0');
  await page.getByRole('button',{name:'Save recording settings',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('#network-settings-form'));
  assert.equal(saved.interval,.5);assert.equal(saved.client_mac,'aa:bb:cc:dd:ee:ff');
  assert.equal(saved.buffer_mib,256);assert.equal(saved.retention_hours,720);assert.equal(saved.max_payload_mib,0);assert.equal(saved.archive_retention_days,180);assert.equal(saved.archive_max_mib,0);assert.equal(saved.archive_enabled,true);
  await page.locator('[data-tab=incidents]').click();
  assert.match(await page.locator('.network-view').textContent(),/720 hours/);
  await page.locator('[data-action=server-settings]').click();
  assert.equal(await page.locator('[data-action=recorder-setup],[data-action=recorder-remove]').count(),0);
  assert.equal(await page.getByRole('button',{name:'OpenWRT',exact:true}).count(),1);
  await page.getByRole('button',{name:'Close dialog',exact:true}).click();
  fs.mkdirSync('test-results',{recursive:true});
  await page.getByRole('button',{name:'Analyse',exact:true}).click();
  await page.locator('.incident-report').waitFor();
  assert.match(await page.locator('.incident-summary').textContent(),/Root cause is not established/);
  assert.match(await page.locator('.incident-report').textContent(),/reconstructed trigger/);
  assert.equal(await page.locator('.incident-report img').count(),0);
  assert.equal(await page.evaluate(()=>window.incidentInjection),undefined);
  await page.locator('.incident-finding [data-action=incident-evidence]').first().click();
  assert.equal(await page.locator('.incident-evidence[open]').count(),1);
  assert.equal(await page.getByRole('link',{name:'Download analysis JSON'}).getAttribute('href'),'/api/servers/router/network/incidents/event?download=true');
  await page.getByText('Export condensed evidence brief',{exact:true}).click();
  await page.getByRole('button',{name:'Generate preview',exact:true}).click();
  const briefPreview=page.locator('#incident-brief-form textarea');
  await page.getByRole('button',{name:'Download brief',exact:true}).waitFor();
  await page.waitForFunction(()=>!document.querySelector('[data-action=incident-brief-download]').disabled);
  assert.equal(await briefPreview.inputValue(),incidentBriefs.markdown3true.content);
  assert.equal(await page.locator('.incident-brief img').count(),0);
  const downloadPromise=page.waitForEvent('download');
  await page.getByRole('button',{name:'Download brief',exact:true}).click();
  const briefDownload=await downloadPromise;
  assert.equal(briefDownload.suggestedFilename(),incidentBriefs.markdown3true.filename);
  assert.equal(fs.readFileSync(await briefDownload.path(),'utf8'),incidentBriefs.markdown3true.content);
  await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{window.copiedBrief=value;}}}));
  await page.getByRole('button',{name:'Copy brief',exact:true}).click();
  assert.equal(await page.evaluate(()=>window.copiedBrief),incidentBriefs.markdown3true.content);
  await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw Error('denied');}}}));
  await page.getByRole('button',{name:'Copy brief',exact:true}).click();
  await page.getByText('Clipboard access is unavailable.',{exact:false}).waitFor();
  assert.equal(await briefPreview.evaluate(el=>el.selectionEnd-el.selectionStart),incidentBriefs.markdown3true.content.length);
  await page.locator('#incident-brief-form [name=format]').selectOption('json');
  await page.locator('#incident-brief-form [name=examples]').selectOption('6');
  await page.locator('#incident-brief-form [name=include_review]').uncheck();
  assert.equal(await page.getByRole('button',{name:'Download brief',exact:true}).isDisabled(),true);
  briefFailure=true;
  await page.getByRole('button',{name:'Generate preview',exact:true}).click();
  await page.getByText('Evidence unavailable for brief',{exact:true}).waitFor();
  briefFailure=false;
  await page.getByRole('button',{name:'Generate preview',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('[data-action=incident-brief-download]').disabled);
  assert.equal(await briefPreview.inputValue(),incidentBriefs.json6false.content);
  delayBrief=true;
  await page.getByRole('button',{name:'Generate preview',exact:true}).click();
  for(let tries=0;!releaseBrief&&tries<30;tries++)await page.waitForTimeout(20);
  assert.equal(typeof releaseBrief,'function');
  await page.locator('#incident-brief-form [name=format]').selectOption('markdown');
  releaseBrief();delayBrief=false;
  await page.waitForTimeout(100);
  assert.equal(await page.getByRole('button',{name:'Download brief',exact:true}).isDisabled(),true);
  assert.equal(await briefPreview.inputValue(),'');
  await page.getByRole('button',{name:'Generate preview',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('[data-action=incident-brief-download]').disabled);
  await page.locator('.incident-brief').scrollIntoViewIfNeeded();
  await page.screenshot({path:'test-results/incident-brief-desktop.png'});
  await page.locator('.modal').evaluate(el=>el.scrollTop=0);
  await page.screenshot({path:'test-results/incident-report-desktop.png'});
  await page.getByRole('button',{name:'Close dialog',exact:true}).click();
  await page.getByRole('button',{name:'Explore incident patterns',exact:true}).click();
  await page.locator('.incident-overviews').waitFor();
  assert.equal(await page.locator('.incident-overviews tbody tr').count(),20);
  await page.getByRole('button',{name:'Next',exact:true}).click();
  assert.equal(await page.locator('.incident-overviews tbody tr').count(),3);
  await page.getByRole('button',{name:'Previous',exact:true}).click();
  await page.locator('.incident-overviews [data-action=incident-open]').first().click();
  await page.locator('.incident-report').waitFor();
  await page.getByRole('button',{name:'Back to patterns'}).click();
  await page.locator('#incident-filters [name=kind]').selectOption('manual');
  await page.getByRole('button',{name:'Refresh analysis',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.incident-overviews tbody tr').length===2);
  assert.match(await page.getByRole('link',{name:'Download patterns (.json.gz)'}).getAttribute('href'),/kind=manual/);
  const patternsDownloadPromise=page.waitForEvent('download');
  await page.getByRole('link',{name:'Download patterns (.json.gz)'}).click();
  const patternsDownload=await patternsDownloadPromise;
  assert.equal(patternsDownload.suggestedFilename(),'harbour-incident-patterns.json.gz');
  const downloadedPatterns=JSON.parse(require('node:zlib').gunzipSync(fs.readFileSync(await patternsDownload.path())).toString('utf8'));
  assert.equal(downloadedPatterns.patterns.incident_count,2);
  assert.equal(downloadedPatterns.reports.length,2);
  await page.locator('#incident-filters [name=kind]').selectOption('all');
  await page.getByRole('button',{name:'Refresh analysis',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.incident-overviews tbody tr').length===20);
  await page.screenshot({path:'test-results/incident-patterns-desktop.png'});
  await page.setViewportSize({width:390,height:844});
  await page.waitForTimeout(200);
  await page.screenshot({path:'test-results/incident-patterns-mobile.png'});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.locator('.incident-overviews [data-action=incident-open]').first().click();
  await page.locator('.incident-report').waitFor();
  await page.getByText('Export condensed evidence brief',{exact:true}).click();
  await page.getByRole('button',{name:'Generate preview',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('[data-action=incident-brief-download]').disabled);
  await page.locator('.incident-brief').scrollIntoViewIfNeeded();
  await page.screenshot({path:'test-results/incident-brief-mobile.png'});
  await page.screenshot({path:'test-results/incident-report-mobile.png'});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.getByRole('button',{name:'Close dialog',exact:true}).click();
  await page.setViewportSize({width:1440,height:1000});
  delayIncident=true;
  await page.getByRole('button',{name:'Analyse',exact:true}).click();
  await page.locator('#incident-content [role=status]').waitFor();
  await page.getByRole('button',{name:'Close dialog',exact:true}).click();
  await page.getByRole('button',{name:'Recording settings',exact:true}).click();
  for(let tries=0;!releaseIncident&&tries<30;tries++)await page.waitForTimeout(20);
  assert.equal(typeof releaseIncident,'function');releaseIncident();delayIncident=false;
  await page.waitForTimeout(100);
  assert.equal(await page.locator('#network-settings-form').count(),1);
  assert.equal(await page.locator('.incident-report').count(),0);
  await page.getByRole('button',{name:'Close dialog',exact:true}).click();
  await page.locator('[data-tab=network]').click();
  await page.screenshot({path:'test-results/openwrt-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await page.waitForTimeout(200);
  const chartSize=await page.locator('.network-chart svg').first().boundingBox();
  assert.equal(chartSize.height,180);
  assert.equal(await page.locator('.network-axis').first().evaluate(e=>getComputedStyle(e).fontSize),'12px');
  await page.screenshot({path:'test-results/openwrt-mobile.png',fullPage:true});
  const overflows=await page.evaluate(()=>[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth+1).map(e=>[e.tagName,e.className,e.getBoundingClientRect().width,e.getBoundingClientRect().right]).slice(0,18));
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,JSON.stringify(overflows));
  await page.getByRole('button',{name:'Recording settings',exact:true}).click();
  await page.locator('[name=archive_enabled]').uncheck();
  await page.locator('[name=retention_hours]').fill('48');
  await page.screenshot({path:'test-results/openwrt-retention-mobile.png',fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.getByRole('button',{name:'Save recording settings',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('#network-settings-form'));
  assert.equal(saved.archive_enabled,false);assert.equal(saved.retention_hours,48);
  await page.locator('[data-tab=archives]').click();
  archives.error='Daily archiving failed. Unarchived readings are protected from cleanup.';
  await page.evaluate(()=>loadNetwork(current(),true));
  assert.match(await page.locator('.network-view .form-error').textContent(),/protected from cleanup/);
  await page.setViewportSize({width:1440,height:1000});
  // Both wide and intermediate desktop sizes reserve the sidebar's actual width.
  for(const width of [2600,1200,900]){
   await page.setViewportSize({width,height:1000});
   await page.locator('[data-tab=network]').click();
   const sizes=await page.evaluate(()=>({sidebar:document.querySelector('.sidebar').getBoundingClientRect().width,left:document.querySelector('.workspace').getBoundingClientRect().left,overflow:document.documentElement.scrollWidth>innerWidth}));
   assert(sizes.sidebar>=280&&sizes.sidebar<=408);assert(sizes.left>=sizes.sidebar-1);assert.equal(sizes.overflow,false);
   if(width===2600){const cards=await page.locator('.probe-card').evaluateAll(es=>es.map(e=>e.getBoundingClientRect().top));assert.equal(cards[0],cards[1]);await page.screenshot({path:'test-results/openwrt-wide.png'});}
  }
  // The mobile backdrop must not dim or intercept the drawer at 700px.
  await page.setViewportSize({width:700,height:1000});
  await page.getByRole('button',{name:'Toggle server list',exact:true}).click();
  await page.waitForTimeout(250);
  assert.equal(await page.evaluate(()=>document.querySelector('.sidebar').contains(document.elementFromPoint(100,150))),true);
  await page.screenshot({path:'test-results/openwrt-sidebar.png'});
  await page.getByRole('button',{name:'Close server list',exact:true}).click();
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('[data-tab=wireless]').click();
  assert.equal(await page.locator('.network-chart svg').count(),1);
  await page.locator('[data-tab=calls]').click();
  await page.locator('[data-action=call-analysis][data-test=load-example]').click();
  await page.locator('#call-analysis').waitFor();
  assert.match(await page.locator('#call-analysis').textContent(),/Receiver impairment was recorded/);
  assert.equal(await page.locator('#call-analysis a').getAttribute('href'),'/api/servers/router/network/call-tests/load-example/export');
  assert.equal(await page.getByText('Remote session verified',{exact:true}).count(),2);
  await page.screenshot({path:'test-results/call-flow-analysis.png'});
  await page.getByRole('button',{name:'Close dialog',exact:true}).click();
  await page.getByRole('button',{name:'Remote logs',exact:true}).click();
  await page.locator('.remote-log-list a').waitFor();
  assert.equal(await page.locator('.remote-log-list a').getAttribute('href'),'/api/call-endpoints/remote/logs/26-10-13/upload-fixture.jsonl');
  await page.getByRole('button',{name:'Close dialog',exact:true}).click();
  await page.evaluate(()=>onboard());
  assert.equal(await page.locator('[name=server_type] option').count(),3);
  await page.locator('[name=server_type]').selectOption('openwrt');
  await page.evaluate(()=>{state.key={id:'test',public_key:'ssh-ed25519 TEST'};updateOpenWrtOnboarding();});
  assert.equal(await page.locator('[data-action=install-key]').count(),0);
  assert.match(await page.locator('#key-output').textContent(),/already authorised/);
  assert.deepEqual(errors,[]);
  console.log('PASS: OpenWRT details, capabilities, charts, markers, settings, read-only onboarding and mobile layout');
 }finally{await browser.close();server.close();}
})().catch(error=>{console.error(error);server.close();process.exitCode=1;});
