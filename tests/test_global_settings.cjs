// Synthetic counters and documentation addresses. No live infrastructure is changed.
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {chromium}=require('playwright');
const now=Date.now()/1000,thresholds={cpu:85,memory:85,disk:85,disk_free_gb:10,temperature:80};
const host={id:'host',name:'Example host',host:'192.0.2.1',port:22,server_type:'plain',services:[],warnings:[],updates:0,checked:now,monitoring_enabled:true,poll_seconds:60,connection_status:'up',thresholds,metrics:{cpu:20,cores:4,memory:{total:8e9,used:3e9,percent:37.5},disks:[],temperature:{sensors:[]},hardware:[],os:'Linux',kernel:'6.1',uptime:1000}};
const profiles={standard:{web_memory_mib:768,recorder_memory_mib:768,processing_mib:64,buffer_total_mib:128,write_interval_seconds:1},'sbc-1g':{web_memory_mib:320,recorder_memory_mib:384,processing_mib:16,buffer_total_mib:16,write_interval_seconds:10}};
let cfg={profile:'standard',...profiles.standard};
let data={available:true,saved:cfg,active:cfg,applied:true,host_memory_mib:8192,revision:'a'.repeat(64),profiles,operation:null};
let monitoring={poll_seconds:60,update_check_hours:6,week1_minutes:1,week2_minutes:5,weeks3_4_minutes:15,older_minutes:60,retention_days:90,history_rows:50000,db_bytes:5e8,reusable_bytes:1e8};
let telegram={enabled:false,chat_id:'',token_saved:false,demo:true,last_sent:null,last_error:'',rules:[],servers:Array.from({length:24},(_,i)=>({id:'host'+i,name:'Example server '+i,monitoring_enabled:true}))};
let bridgeEnabled=false,monitorFailures=1,releaseResource,holdResource=true,telegramTests=0;
const calls={};
const server=http.createServer((req,res)=>{try{const file=path.join(process.cwd(),'harbour/static',req.url==='/'?'index.html':req.url.replace('/static/',''));res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html');res.end(fs.readFileSync(file));}catch{res.writeHead(404).end();}});
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({headless:true,executablePath:process.env.HARBOUR_TEST_BROWSER||(fs.existsSync(chromium.executablePath())?chromium.executablePath():'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')});try{
const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.route('**/api/**',async route=>{
 const url=new URL(route.request().url()),method=route.request().method(),body=route.request().postDataJSON();let value;
 calls[method+' '+url.pathname]=(calls[method+' '+url.pathname]||0)+1;
 if(url.pathname==='/api/config')value={demo:false,version:'test'};
 else if(url.pathname==='/api/me')value={id:'admin',name:'admin',role:'admin',csrf:'test'};
 else if(url.pathname==='/api/dashboard')value={servers:[host],thresholds,jobs:[],bridge_enabled:bridgeEnabled};
 else if(url.pathname==='/api/session/activity')value={ok:true};
 else if(url.pathname.endsWith('/history'))value={points:[],hours:6,from:now-21600,to:now,resolution_seconds:60,poll_seconds:60};
 else if(url.pathname==='/api/monitoring'){
  if(method==='GET'&&monitorFailures-- > 0){await route.fulfill({status:503,json:{detail:'Monitoring temporarily unavailable'}});return;}
  if(method==='PUT')monitoring={...monitoring,...body};value=monitoring;
 }else if(url.pathname==='/api/notifications'){
  if(method==='PUT')telegram={...telegram,...body};value=telegram;
 }else if(url.pathname==='/api/notifications/test'){telegramTests++;value={simulated:true};}
 else if(url.pathname==='/api/thresholds'){Object.assign(thresholds,body);value=thresholds;}
 else if(url.pathname==='/api/bridge/settings'){bridgeEnabled=body.enabled;value=body;}
 else if(url.pathname==='/api/bridge/stream'){await route.fulfill({contentType:'text/event-stream',body:': heartbeat\n\n'});return;}
 else if(url.pathname==='/api/resource-settings'){if(holdResource)await new Promise(r=>releaseResource=r);value=data;}
 else throw Error('Unexpected API '+url.pathname);
 await route.fulfill({json:value});
});
await page.goto('http://127.0.0.1:'+server.address().port);
await page.locator('#sidebar-menu>summary').click();await page.getByRole('button',{name:'Global settings',exact:true}).click();
const dialog=page.locator('.global-settings-modal'),tabs=page.getByRole('tablist',{name:'Global settings sections'});
const names=['Warnings','Monitoring & history','Resources','Notifications','The Bridge'];
const bounds=()=>dialog.evaluate(n=>{const {x,y,width,height}=n.getBoundingClientRect();return {x,y,width,height};});
const initial=await bounds();assert.equal(initial.width,1000);assert.equal(initial.height,900);
const stable=async()=>{assert.deepEqual(await bounds(),initial);assert.equal(await page.locator('.modal-backdrop').count(),1);};
assert.equal(await page.getByRole('button',{name:'Configure monitoring',exact:true}).count(),0);
assert.equal(await page.getByRole('button',{name:'Configure resources',exact:true}).count(),0);
await page.locator('#threshold-form [name=cpu]').fill('94');
await tabs.getByRole('tab',{name:'Monitoring & history',exact:true}).click();await page.getByText('Monitoring temporarily unavailable',{exact:true}).waitFor();await stable();
await page.getByRole('button',{name:'Retry',exact:true}).click();await page.locator('#monitoring-form').waitFor();
await page.locator('#monitoring-form [name=poll_seconds]').fill('120');
await tabs.getByRole('tab',{name:'Resources',exact:true}).click();await stable();
while(!releaseResource)await new Promise(r=>setTimeout(r,10));
await tabs.getByRole('tab',{name:'Warnings',exact:true}).click();holdResource=false;releaseResource();
await page.waitForFunction(()=>document.querySelector('#resource-settings-form'));
assert.equal(await page.locator('#threshold-form [name=cpu]').inputValue(),'94');await stable();
await page.getByRole('button',{name:'Save thresholds',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.toast')?.textContent.includes('Thresholds saved'));
assert.equal(thresholds.cpu,94);
await tabs.getByRole('tab',{name:'Monitoring & history',exact:true}).click();assert.equal(await page.locator('#monitoring-form [name=poll_seconds]').inputValue(),'120');
await page.getByRole('button',{name:'Save monitoring policy',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#monitoring-form [name=poll_seconds]')?.disabled===false);
assert.equal(monitoring.poll_seconds,120);
await tabs.getByRole('tab',{name:'Resources',exact:true}).click();await page.locator('#resource-settings-form [name=buffer_total_mib]').fill('64');await stable();
await tabs.getByRole('tab',{name:'Notifications',exact:true}).click();await page.locator('#telegram-form').waitFor();
assert.equal(await page.locator('.telegram-rule').count(),120);await stable();
await page.locator('#telegram-form [name=chat_id]').fill('draft-chat');
await page.getByRole('button',{name:'Simulate test message',exact:true}).click();await page.getByText('Save your changes before sending a test message.',{exact:true}).waitFor();assert.equal(telegramTests,0);
assert.equal(await page.locator('#threshold-form .form-error').textContent(),'');
await tabs.getByRole('tab',{name:'The Bridge',exact:true}).click();await page.locator('#bridge-settings [name=enabled]').check();await page.locator('#bridge-settings [type=submit]').click();
await page.getByRole('button',{name:'Open The Bridge',exact:true}).waitFor();assert.equal(bridgeEnabled,true);await stable();
await tabs.getByRole('tab',{name:'Resources',exact:true}).click();assert.equal(await page.locator('#resource-settings-form [name=buffer_total_mib]').inputValue(),'64');
await page.getByRole('button',{name:'Close dialog'}).click();await page.locator('#discard-guard').waitFor();await page.getByRole('button',{name:'Keep editing',exact:true}).click();
fs.mkdirSync('test-results',{recursive:true});
for(const name of names){await tabs.getByRole('tab',{name,exact:true}).click();await stable();await page.screenshot({path:'test-results/global-settings-'+name.split(' ')[0].toLowerCase()+'.png'});}
await tabs.getByRole('tab',{name:'The Bridge',exact:true}).press('Home');assert.equal(await tabs.getByRole('tab',{name:'Warnings',exact:true}).getAttribute('aria-selected'),'true');
await tabs.getByRole('tab',{name:'Warnings',exact:true}).press('ArrowRight');assert.equal(await tabs.getByRole('tab',{name:'Monitoring & history',exact:true}).getAttribute('aria-selected'),'true');
for(const viewport of [{width:1365,height:720},{width:390,height:844}]){
 await page.setViewportSize(viewport);const expected=await bounds();
 for(const name of names){await tabs.getByRole('tab',{name,exact:true}).click();assert.deepEqual(await bounds(),expected);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);}
}
await tabs.getByRole('tab',{name:'Resources',exact:true}).click();await page.screenshot({path:'test-results/global-settings-mobile.png'});
await page.locator('#settings-panel-resources').evaluate(n=>n.scrollTop=n.scrollHeight);
assert.equal(await page.getByRole('button',{name:'Apply & restart',exact:true}).isVisible(),true);
assert.equal(await tabs.isVisible(),true);
await page.getByRole('button',{name:'Close dialog'}).click();await page.getByRole('button',{name:'Discard changes',exact:true}).click();
await page.evaluate(()=>showResourceSettings());await page.locator('#resource-settings-form').waitFor();assert.equal(await page.locator('#resource-settings-form [name=buffer_total_mib]').inputValue(),'128');
assert.equal(await page.getByRole('tab',{name:'Resources',exact:true}).getAttribute('aria-selected'),'true');
assert.equal(calls['GET /api/monitoring'],2);assert.deepEqual(errors,[]);
console.log('Global settings passed: embedded forms, stable bounds, retained edits, independent saves/errors, lazy loading, keyboard tabs and responsive scrolling.');
}finally{await browser.close();server.close();}})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
