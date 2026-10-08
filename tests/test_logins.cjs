// Isolated browser verification of host authentication history; no live hosts.
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {chromium}=require('playwright');
const now=Date.now()/1000;
const servers=['docker','plain'].map((type,i)=>({id:type,name:type==='docker'?'Atlas':'Mac workstation',server_type:type,
 host:'example.invalid',username:'harbour',port:22,checked:now,connection_status:'up',monitoring_enabled:true,poll_seconds:60,
 warnings:[],updates:0,services:[],metrics:null,thresholds:{},latency_ms:1}));
let requests=[],delayResponse=false,releaseResponse=null,fail=false;
let status={state:'connected',fetched_at:now,sources:{journal:{state:'listening',detail:''}},pending:0,dropped:0,limitations:[]};
const records=Array.from({length:105},(_,i)=>({id:105-i,seq:105-i,username:i===0?'<img src=x onerror=alert(1)>':'example-user',
 occurred_at:now-i,captured_at:now-i,collected_at:now,acknowledged_at:i?now:null,service:'ssh',
 result:i%2?'failure':'success',method:i%2?'password':'publickey',source_ip:i%2?'2001:db8::2':'192.0.2.1',source_port:50122,
 key_fingerprint:i%2?null:'SHA256:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890',key_algorithm:'ED25519',
 event_type:'authentication',source:'journal',evidence:'<script>window.compromised=true</script>',harbour_key:i===2}));
const server=http.createServer((req,res)=>{try{const file=path.join(process.cwd(),'harbour/static',req.url==='/'?'index.html':req.url.replace('/static/',''));res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html');res.end(fs.readFileSync(file));}catch{res.writeHead(404).end();}});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({headless:true,executablePath:process.env.HARBOUR_TEST_BROWSER||(fs.existsSync(chromium.executablePath())?chromium.executablePath():'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')});
 try{
  const page=await browser.newPage({viewport:{width:1500,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/api/**',async route=>{
   const url=new URL(route.request().url());let data;
   if(url.pathname==='/api/config')data={demo:false,version:'test'};
   else if(url.pathname==='/api/me')data={id:'admin',name:'admin',role:'admin',csrf:'test'};
   else if(url.pathname==='/api/dashboard')data={servers,jobs:[],thresholds:{}};
   else if(url.pathname==='/api/session/activity')data={ok:true};
   else if(url.pathname.endsWith('/logins')){
    requests.push(url);if(delayResponse){delayResponse=false;await new Promise(r=>releaseResponse=r);}
    if(fail){await route.fulfill({status:503,json:{detail:'Temporary connection failure'}});return;}
    let found=url.pathname.includes('/plain/')?[]:records.filter(e=>(!Number(url.searchParams.get('before'))||e.id<Number(url.searchParams.get('before')))
     &&(url.searchParams.get('outcome')==='all'||e.result===url.searchParams.get('outcome'))
     &&(!url.searchParams.get('search')||JSON.stringify(e).includes(url.searchParams.get('search')))
     &&(url.searchParams.get('hide_harbour')!=='true'||!e.harbour_key));
    data={events:found.slice(0,100),next_before:found.length>100?found[99].id:null,status,paused:!servers.find(s=>url.pathname.includes('/'+s.id+'/')).monitoring_enabled,retention_days:90};
   }else throw Error('Unexpected API '+url.pathname);
   await route.fulfill({json:data});
  });
  await page.goto('http://127.0.0.1:'+server.address().port);
  await page.locator('[data-tab=logins]').click();await page.locator('.login-event').first().waitFor();
  assert.equal(await page.locator('.login-event').count(),100);
  assert.match(await page.locator('.login-event').first().textContent(),/<img src=x/);
  assert.equal(await page.locator('.login-event img,.login-event script').count(),0);
  await page.locator('.login-event summary').first().click();
  assert.match(await page.locator('.login-event-detail').first().textContent(),/Pending next probe/);
  await page.evaluate(()=>{window.eventNode=document.querySelector('.login-event');loginView('docker').loaded=0;return loadLogins(current(),true);});
  assert.equal(await page.evaluate(()=>eventNode===document.querySelector('.login-event')&&eventNode.open),true);
  assert.equal(await page.evaluate(()=>window.compromised),undefined);
  await page.getByRole('button',{name:'Older',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.login-event').length===5);
  await page.getByRole('button',{name:'Newer',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.login-event').length===100);
  await page.locator('#login-filter select').selectOption('failure');
  await page.getByRole('button',{name:'Filter',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.login-event').length===52);
  await page.locator('#login-filter select').selectOption('all');
  await page.locator('#login-filter input[name=search]').fill('192.0.2.1');
  await page.locator('#login-filter input[name=hide]').check();
  await page.getByRole('button',{name:'Filter',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.login-event').length===52);
  assert.equal(requests.at(-1).searchParams.get('hide_harbour'),'true');
  // An old host response must not replace the newly selected host's events.
  delayResponse=true;
  await page.evaluate(()=>{loginView('docker').loaded=0;loadLogins(current(),true);});
  await page.waitForTimeout(50);
  await page.locator('[data-key="server:plain"]').click();
  await page.waitForFunction(()=>document.querySelector('.logins-view')?.dataset.key==='logins:plain');
  releaseResponse();await page.waitForTimeout(100);
  assert.equal(await page.locator('.login-event').count(),0);
  assert.equal(await page.locator('[data-tab=containers]').count(),0);
  status={state:'not_installed',fetched_at:now,detail:'Install the collector'};
  await page.getByRole('button',{name:'Refresh',exact:true}).click();
  await page.getByText('Collector not installed',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Collector setup',exact:true}).click();
  assert.equal(await page.locator('a[href="/api/login-collector/download"]').count(),1);
  await page.getByRole('button',{name:'Close dialog',exact:true}).click();
  status={state:'connected',fetched_at:now,pending:123,dropped:4,last_drop_at:now,sources:{'unified-log':{state:'partial',detail:'SSH log coverage only'}},limitations:['Native authentication events are not enabled.']};
  await page.getByRole('button',{name:'Refresh',exact:true}).click();
  await page.getByText('Partial coverage',{exact:true}).waitFor();
  assert.match(await page.locator('.login-status').textContent(),/4 unacknowledged events lost/);
  servers[1].monitoring_enabled=false;await page.evaluate(()=>{loginView('plain').loaded=0;return loadLogins(current(),true);});
  await page.getByText('Collection transfer paused',{exact:true}).waitFor();
  for(const theme of ['dark','light']){
   await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
   await page.setViewportSize({width:390,height:844});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  }
  await page.setViewportSize({width:1500,height:1000});
  await page.locator('[data-key="server:docker"]').click();
  fail=true;await page.getByRole('button',{name:'Refresh',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'Temporary connection failure'}).waitFor();
  assert.equal(errors.length,0,errors.join('\n'));
  if(process.env.HARBOUR_TEST_CAPTURE){fail=false;status={state:'connected',fetched_at:now,sources:{journal:{state:'listening'}},pending:0,dropped:0,limitations:[]};await page.evaluate(()=>{const v=loginView('docker');v.search='';v.hide=false;v.outcome='all';resetLoginView(v);renderMain();});await page.locator('.login-event').first().waitFor();await page.locator('.login-event summary').first().click();fs.mkdirSync('test-results',{recursive:true});await page.screenshot({path:'test-results/logins-desktop.png'});await page.setViewportSize({width:390,height:844});await page.evaluate(()=>setMobilePanel(false));await page.screenshot({path:'test-results/logins-mobile.png'});}
  console.log('Logins browser checks passed: both host types, filters, pagination, safe evidence, live updates, stale responses, coverage, errors, desktop/mobile.');
 }finally{await browser.close();server.close();}
})().catch(err=>{console.error(err);server.close();process.exit(1);});
