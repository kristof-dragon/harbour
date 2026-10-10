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
const settings={enabled:true,interval:.25,targets:['1.1.1.1'],router_probes:true,wan_device:'wan',client_mac:'',latency_limit_ms:150,retention_hours:168,max_rows:0,max_payload_mib:1024,archive_enabled:true,archive_retention_days:90,archive_max_mib:4096,incident_max_count:200,incident_max_mib:128};
const archives={directory:'/data/network-archives',file_count:2,bytes:300000,files:[{id:'today',day:Math.floor(now/86400)*86400,sample_count:1000,size:100000,complete:0},{id:'yesterday',day:Math.floor(now/86400)*86400-86400,sample_count:2000,size:200000,complete:1}]};
const rows=Array.from({length:120},(_,i)=>[{at:now-120+i,kind:'telemetry',metrics:{...host.metrics,openwrt:router}},{at:now-120+i,kind:'probe',source:'recorder',target:'1.1.1.1',status:i===60?'timeout':'reply',rtt_ms:i===60?null:20+i%10}]).flat();
let saved=null,markers=[];
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
   else if(url.pathname.endsWith('/history'))data={points:[],hours:6,from:now-21600,to:now,resolution_seconds:60,poll_seconds:60};
   else if(url.pathname.endsWith('/network/samples'))data={start:now-120,end:now,samples:rows,truncated:false};
   else if(url.pathname.endsWith('/network/markers')){markers.push(route.request().postDataJSON());data={id:'new-event'};}
   else if(url.pathname.endsWith('/network')){
    if(method==='PUT'){saved=route.request().postDataJSON();Object.assign(settings,saved);data={ok:true};}
    else data={settings,archives,status:{state:'recording',recorder_online:true,observer:'onsite-server',probe_errors:{}},device:router,events:[{id:'event',measured:now-60,label:'Video degraded',complete:1}]};
   }else throw Error('Unexpected API '+url.pathname);
   await route.fulfill({json:data});
  });
  await page.goto('http://127.0.0.1:'+server.address().port);
  await page.getByText('Example MT7621 router',{exact:true}).first().waitFor();
  await page.locator('.network-chart svg').first().waitFor();
  assert.equal(await page.locator('[data-tab=containers],[data-tab=logins]').count(),0);
  assert.match(await page.locator('.network-view').textContent(),/ethernet details: unavailable/);
  assert.equal(await page.locator('.network-chart svg').count(),6);
  assert.doesNotMatch(await page.locator('.network-legend').allTextContents().then(x=>x.join(' ')),/example-tunnel/);
  const sparse=await page.evaluate(()=>{
   const end=Date.now()/1000,start=end-120;
   const render=points=>new DOMParser().parseFromString(networkChart('Sparse readings','ms',[{name:'Example target',maxGap:1.5,points}],start,end),'text/html');
   const isolated=render([[end-110,10],[end-100,20],[end-10,0]]);
   const broken=render([[end-3,3],[end-2.5,4],[end-1,null],[end-.5,2]]);
   return {dots:isolated.querySelectorAll('.network-point').length,lines:isolated.querySelectorAll('polyline').length,brokenLines:broken.querySelectorAll('polyline').length,brokenDots:broken.querySelectorAll('.network-point').length,missing:render([[end-1,null]]).querySelectorAll('svg').length};
  });
  assert.deepEqual(sparse,{dots:3,lines:0,brokenLines:1,brokenDots:1,missing:0});
  assert.equal(await page.getByRole('link',{name:'Download .jsonl.gz',exact:true}).getAttribute('href'),'/api/servers/router/network/archives/yesterday');
  assert.match(await page.locator('.network-view').textContent(),/Collecting · available after midnight/);
  await page.getByRole('button',{name:'Mark degradation',exact:true}).click();
  await page.waitForTimeout(100);assert.equal(markers[0].label,'Call degradation');
  await page.getByRole('button',{name:'Recording settings',exact:true}).click();
  await page.locator('[name=interval]').selectOption('0.5');
  await page.locator('[name=client_mac]').fill('aa:bb:cc:dd:ee:ff');
  await page.locator('[name=retention_hours]').fill('720');
  await page.locator('[name=max_payload_mib]').fill('0');
  await page.locator('[name=archive_retention_days]').fill('180');
  await page.locator('[name=archive_max_mib]').fill('0');
  await page.getByRole('button',{name:'Save recording settings',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('#network-settings-form'));
  assert.equal(saved.interval,.5);assert.equal(saved.client_mac,'aa:bb:cc:dd:ee:ff');
  assert.equal(saved.retention_hours,720);assert.equal(saved.max_payload_mib,0);assert.equal(saved.archive_retention_days,180);assert.equal(saved.archive_max_mib,0);assert.equal(saved.archive_enabled,true);
  assert.match(await page.locator('.network-view').textContent(),/720 hours/);
  await page.locator('[data-action=server-settings]').click();
  assert.equal(await page.locator('[data-action=recorder-setup],[data-action=recorder-remove]').count(),0);
  assert.equal(await page.getByRole('button',{name:'OpenWRT',exact:true}).count(),1);
  await page.getByRole('button',{name:'Close dialog',exact:true}).click();
  fs.mkdirSync('test-results',{recursive:true});
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
  archives.error='Daily archiving failed. Unarchived readings are protected from cleanup.';
  await page.evaluate(()=>loadNetwork(current(),true));
  assert.match(await page.locator('.network-view .form-error').textContent(),/protected from cleanup/);
  await page.setViewportSize({width:1440,height:1000});
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
