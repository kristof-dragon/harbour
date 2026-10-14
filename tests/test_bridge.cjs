// Isolated UI checks: no live hosts or credentials.
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {chromium}=require('playwright');
const now=Date.now()/1000;
const servers=[{id:'atlas',name:'Atlas',server_type:'plain',host:'example.invalid',username:'harbour',port:22,checked:now,connection_status:'up',monitoring_enabled:true,poll_seconds:60,warnings:[],updates:0,services:[],metrics:null,thresholds:{},latency_ms:1}];
let enabled=false,settingsWrites=0,annotationWrites=0,identityWrites=0,liveRequests=0,loginRequests=0;
const record={id:12,ref:'session:12',record_type:'session',kind:'session',event_count:2,server_id:'atlas',server_name:'Atlas',username:'dragon',service:'ssh',method:'publickey',result:'success',session_status:'active',started_at:now-50,time:now-50,source_ip:'192.0.2.2',key_fingerprint:'SHA256:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890',credential_verified:true,tags:[],identity_tags:[],notes:'',label:'',match_basis:'credential'};
const coverage=[{id:'atlas',name:'Atlas',state:'connected',verified:true,checked_at:now,mode:'live',sources:{journal:{state:'listening',detail:''}},limitations:[],os_sessions:'unavailable'}];
const server=http.createServer((req,res)=>{try{const file=path.join(process.cwd(),'harbour/static',req.url==='/'?'index.html':req.url.replace('/static/',''));res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html');res.end(fs.readFileSync(file));}catch{res.writeHead(404).end();}});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({headless:true,executablePath:process.env.HARBOUR_TEST_BROWSER||(fs.existsSync(chromium.executablePath())?chromium.executablePath():'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')});
 try{
  const page=await browser.newPage({viewport:{width:1500,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{window.EventSource=class extends EventTarget{constructor(url){super();window.testStream=this;}close(){this.closed=true;}};});
  await page.route('**/api/**',async route=>{
   const url=new URL(route.request().url()),method=route.request().method();let data;
   if(url.pathname==='/api/config')data={demo:false,version:'test'};
   else if(url.pathname==='/api/me')data={id:'admin',name:'admin',role:'admin',csrf:'test'};
   else if(url.pathname==='/api/dashboard')data={servers,jobs:[],thresholds:{},bridge_enabled:enabled};
   else if(url.pathname==='/api/session/activity')data={ok:true};
   else if(url.pathname==='/api/bridge/settings'&&method==='PUT'){enabled=route.request().postDataJSON().enabled;settingsWrites++;data={enabled};}
   else if(url.pathname==='/api/bridge'){liveRequests++;data={active:[record],coverage};}
   else if(url.pathname==='/api/bridge/history')data={records:[record],pagination:{page:1,total:60,size:25,pages:3}};
   else if(url.pathname.endsWith('/annotation')&&method==='PUT'){const body=route.request().postDataJSON();Object.assign(record,body);annotationWrites++;data={ok:true};}
   else if(url.pathname.endsWith('/identity')&&method==='PUT'){const body=route.request().postDataJSON();Object.assign(record,{label:body.label,identity_tags:body.tags,profile_id:'profile1'});identityWrites++;data={ok:true};}
   else if(url.pathname.startsWith('/api/bridge/records/'))data={record,stats:{connections:6,observations:6,servers:2,addresses:3,first_seen:now-3000,last_seen:now},profile_notes:'',evidence:[{occurred_at:now,result:'success',evidence:'<script>window.compromised=true</script>'}]};
   else if(url.pathname==='/api/bridge/identities/profile1'){
    if(method==='PUT'){const body=route.request().postDataJSON();Object.assign(record,{label:body.label,identity_tags:body.tags});data={ok:true};}
    else data={id:'profile1',label:record.label,tags:record.identity_tags,notes:'',match:['credential','dragon',record.key_fingerprint]};
   }
   else if(url.pathname==='/api/bridge/identities')data={profiles:[{id:'profile1',label:record.label,tags:record.identity_tags,basis:'credential',username:'dragon',notes:''}]};
   else if(url.pathname.endsWith('/logins/table')){loginRequests++;data={events:[record],current_sessions:[record],session_presence:{available:true,fetched_at:now,checked_at:now},status:{state:'connected',fetched_at:now,sources:{}},pagination:{page:1,total:1,size:25,pages:1}};}
   else data={};
   await route.fulfill({json:data});
  });
  await page.goto('http://127.0.0.1:'+server.address().port);
  await page.locator('#shell').waitFor();
  assert.equal(await page.locator('[data-action=bridge]').count(),0);
  await page.locator('#sidebar-menu summary').click();
  await page.getByRole('button',{name:'Global settings',exact:true}).click();
  await page.locator('#bridge-settings [name=enabled]').check();
  await page.locator('#bridge-settings button[type=submit]').click();
  await page.waitForFunction(()=>state.data.bridge_enabled);
  assert.equal(settingsWrites,1);
  await page.getByRole('button',{name:'Open The Bridge',exact:true}).click();
  await page.getByText('Currently connected',{exact:true}).waitFor();
  assert.equal(await page.locator('#bridge-root .bridge-connected').count(),1);
  await page.getByRole('button',{name:'Inspect',exact:true}).click();
  await page.locator('#bridge-annotation').waitFor();
  assert.match(await page.locator('.bridge-stats').textContent(),/5 other connections/);
  await page.locator('#bridge-annotation [name=tags]').fill('review, <img src=x onerror=alert(1)>');
  await page.locator('#bridge-annotation [name=notes]').fill('Check this connection');
  await page.getByRole('button',{name:'Save session tags',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#bridge-annotation [name=notes]')?.value==='Check this connection');
  await page.waitForTimeout(100);
  assert.equal(annotationWrites,1);
  await page.locator('#bridge-identity [name=label]').fill('Admin laptop');
  await page.locator('#bridge-identity [name=tags]').fill('known-device');
  await page.getByRole('button',{name:'Save identity',exact:true}).click();
  await page.getByRole('heading',{name:'Admin laptop',exact:true}).waitFor();
  assert.equal(identityWrites,1);
  await page.getByRole('button',{name:'Matching history',exact:true}).click();
  await page.locator('[data-table-key=bridge-history]').waitFor();
  assert.equal(await page.locator('[data-table-filter]').count(),8);
  assert.equal(await page.locator('#bridge-root img').count(),0);
  await page.locator('[data-table-filter=source]').fill('192.0.2');
  await page.waitForTimeout(350);
  assert.equal(await page.locator('[data-table-filter=source]').inputValue(),'192.0.2');
  await page.getByRole('button',{name:'Identities',exact:true}).click();
  await page.getByRole('button',{name:'Edit',exact:true}).click();
  await page.locator('#bridge-profile-form [name=label]').fill('Admin workstation');
  await page.getByRole('button',{name:'Save identity',exact:true}).click();
  await page.getByText('Admin workstation',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Connected',exact:true}).click();
  const before=liveRequests;
  await page.evaluate(()=>testStream.dispatchEvent(new Event('changed')));
  await page.waitForTimeout(500);assert.ok(liveRequests>before);
  for(const theme of ['light','dark']){
   await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
   await page.setViewportSize({width:390,height:844});await page.waitForTimeout(100);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  }
  await page.setViewportSize({width:1500,height:1000});
  await page.screenshot({path:'/tmp/harbour-bridge.png',fullPage:true});
  await page.getByRole('button',{name:'Close dialog',exact:true}).click();
  await page.locator('[data-tab=logins]').click();await page.locator('.logins-view').waitFor();
  const beforeLogins=loginRequests;
  await page.evaluate(()=>testStream.dispatchEvent(new Event('changed')));
  await page.waitForTimeout(500);assert.ok(loginRequests>beforeLogins);
  await page.locator('#sidebar-menu').evaluate(el=>el.open=true);
  await page.getByRole('button',{name:'Global settings',exact:true}).click();
  await page.locator('#bridge-settings [name=enabled]').uncheck();
  await page.locator('#bridge-settings button[type=submit]').click();
  await page.waitForFunction(()=>!state.data.bridge_enabled);
  assert.equal(await page.locator('[data-action=bridge]').count(),0);
  assert.equal(await page.evaluate(()=>testStream.closed),true);
  assert.equal(await page.evaluate(()=>window.compromised),undefined);
  assert.deepEqual(errors,[]);
  console.log('Bridge enablement, labels, independent tags, matching history, tables, live notices, mobile and escaping passed');
 }finally{await browser.close();server.close();}
})().catch(error=>{console.error(error);server.close();process.exitCode=1;});
