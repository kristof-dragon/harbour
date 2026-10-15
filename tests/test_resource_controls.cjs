// Synthetic counters and documentation addresses. No live infrastructure is changed.
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {chromium}=require('playwright');
const now=Date.now()/1000,thresholds={cpu:85,memory:85,disk:85,disk_free_gb:10,temperature:80};
const host={id:'host',name:'Example host',host:'192.0.2.1',port:22,server_type:'plain',services:[],warnings:[],updates:0,checked:now,monitoring_enabled:true,poll_seconds:60,connection_status:'up',thresholds,metrics:{cpu:20,cores:4,memory:{total:8e9,used:3e9,percent:37.5},disks:[],temperature:{sensors:[]},hardware:[],os:'Linux',kernel:'6.1',uptime:1000}};
const profiles={standard:{web_memory_mib:768,recorder_memory_mib:768,processing_mib:64,buffer_total_mib:128,write_interval_seconds:1},'sbc-1g':{web_memory_mib:320,recorder_memory_mib:384,processing_mib:16,buffer_total_mib:16,write_interval_seconds:10}};
let cfg={profile:'standard',...profiles.standard};
let data={available:true,saved:cfg,active:cfg,applied:true,host_memory_mib:8192,revision:'a'.repeat(64),profiles,operation:null};
let posts=[],reject=false,unavailable=false;
const server=http.createServer((req,res)=>{try{const file=path.join(process.cwd(),'harbour/static',req.url==='/'?'index.html':req.url.replace('/static/',''));res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html');res.end(fs.readFileSync(file));}catch{res.writeHead(404).end();}});
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({headless:true,executablePath:process.env.HARBOUR_TEST_BROWSER||(fs.existsSync(chromium.executablePath())?chromium.executablePath():'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')});try{
const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.route('**/api/**',async route=>{const url=new URL(route.request().url());let value;
 if(url.pathname==='/api/config')value={demo:false,version:'test'};
 else if(url.pathname==='/api/me')value={id:'admin',name:'admin',role:'admin',csrf:'test'};
 else if(url.pathname==='/api/dashboard')value={servers:[host],thresholds,jobs:[],bridge_enabled:false};
 else if(url.pathname==='/api/session/activity')value={ok:true};
 else if(url.pathname.endsWith('/history'))value={points:[],hours:6,from:now-21600,to:now,resolution_seconds:60,poll_seconds:60};
 else if(url.pathname==='/api/resource-settings'){
  if(route.request().method()==='POST'){
   const body=route.request().postDataJSON();posts.push(body);
   if(reject){await route.fulfill({status:422,json:{detail:'Each memory limit must be at least eight times the evidence processing allowance.'}});return;}
   data={...data,operation:{status:'applying',request_id:body.request_id,message:'Restarting Harbour…'},saved:body.settings};value={operation:data.operation};
  }else value=unavailable?{available:false,reason:'Resource controls are unavailable on this deployment.',profiles}:data;
 }else throw Error('Unexpected API '+url.pathname);
 await route.fulfill({json:value});
});
await page.goto('http://127.0.0.1:'+server.address().port);
await page.locator('#sidebar-menu>summary').click();await page.getByRole('button',{name:'Global settings',exact:true}).click();
await page.getByRole('tab',{name:'Resources',exact:true}).click();
const form=page.locator('#resource-settings-form');await form.waitFor();
assert.equal(await form.locator('[name=web_memory_mib]').inputValue(),'768');
await form.locator('[name=profile]').selectOption('sbc-1g');
assert.equal(await form.locator('[name=web_memory_mib]').inputValue(),'320');
assert.equal(await form.locator('[name=write_interval_seconds]').inputValue(),'10');
assert.match(await page.locator('#resource-profile-note').textContent(),/2 seconds.*10 seconds/);
assert.match(await page.locator('#resource-total').textContent(),/704 MiB/);
await form.locator('[name=write_interval_seconds]').fill('15');
// Dirty-form guard also applies to this newly added panel.
await page.getByRole('button',{name:'Close dialog'}).click();await page.locator('#discard-guard').waitFor();
await page.evaluate(()=>finishDiscard(false));
reject=true;await form.getByRole('button',{name:'Apply & restart',exact:true}).click();
await page.waitForFunction(()=>document.querySelector('#resource-settings-form .form-error').textContent.includes('eight times'));
assert.equal(await form.locator('[name=web_memory_mib]').isDisabled(),false);
reject=false;await form.getByRole('button',{name:'Apply & restart',exact:true}).click();
await page.waitForFunction(()=>document.querySelector('#resource-settings-form fieldset').disabled);
assert.equal(posts.length,2);assert.equal(posts[1].settings.write_interval_seconds,15);
assert.equal(posts[1].settings.web_memory_mib,320);assert.equal(posts[1].revision,'a'.repeat(64));
assert.match(posts[1].request_id,/^[a-f0-9]{36}$/);
// One unavailable poll is a reconnect, never a false success.
unavailable=true;await page.waitForFunction(()=>document.querySelector('#resource-operation').textContent.includes('reconnect'));
assert.equal(await form.locator('[name=web_memory_mib]').isDisabled(),true);
unavailable=false;data={...data,active:data.saved,revision:'b'.repeat(64),operation:{...data.operation,status:'succeeded',message:'Applied. Both services are running with the requested limits.'}};
await page.waitForFunction(()=>document.querySelector('#resource-operation').textContent.startsWith('Applied.'),{},{timeout:10000});
assert.equal(await form.locator('[name=web_memory_mib]').isDisabled(),false);
assert.match(await page.locator('#resource-active').textContent(),/320 MiB.*384 MiB.*15 s/);
fs.mkdirSync('test-results',{recursive:true});await page.screenshot({path:'test-results/resource-controls-desktop.png'});
for(const theme of ['light','dark']){await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);await page.setViewportSize({width:390,height:844});await page.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth);assert.equal(await page.locator('.modal').evaluate(n=>n.getBoundingClientRect().right<=innerWidth),true);}
await page.screenshot({path:'test-results/resource-controls-mobile.png'});
await page.getByRole('button',{name:'Close dialog'}).click();assert.equal(await page.evaluate(()=>resourcePoll),null);
unavailable=true;await page.evaluate(()=>showResourceSettings());await page.getByText('Resource controls are unavailable on this deployment.',{exact:true}).waitFor();assert.equal(await page.locator('#resource-settings-form').count(),0);
assert.deepEqual(errors,[]);console.log('Resource controls UI passed: presets, overrides, validation, dirty guard, restart/reconnect, verified status and mobile.');
}finally{await browser.close();server.close();}})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
