// Synthetic warning history; no live servers, credentials or notifications.
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {chromium}=require('playwright');
const now=Date.now()/1000,thresholds={cpu:85,memory:85,disk:85,disk_free_gb:10,temperature:80};
const host={id:'host',name:'Test server',host:'192.0.2.1',port:22,server_type:'plain',services:[],warnings:[{id:'cpu',title:'CPU usage is high',detail:'95%',event_id:60,dismissed:false}],updates:0,checked:now,monitoring_enabled:true,poll_seconds:60,connection_status:'up',thresholds,metrics:{cpu:95,cores:4,memory:{percent:30,total:8e9,used:2.4e9},disks:[],temperature:{sensors:[]},os:'Test Linux',kernel:'test',uptime:1000}};
const events=Array.from({length:60},(_,i)=>({id:i+1,server_id:'host',server_name:'Test server',warning_key:i===59?'cpu':'temperature:'+i,source:'resource',title:i===59?'CPU usage is high':i===58?'NVMe <img src=x onerror="window.injected=true">':'NVMe sensor '+i,detail:'Temperature exceeded its configured limit',definition:{unit:i===59?'%':'°C',low:null,high:i===59?85:80,side:'high'},started_at:now-3600-i*60,last_seen:now-3500-i*60,detected_at:now-3400-i*60,closed_at:i===59?null:now-3000-i*60,close_reason:i===59?null:'recovered',initial_values:{value:82},latest_values:{value:90},extreme_values:{value:96},observation_gap:i===58?1:0,status:i===59?'active':'resolved'}));
let user='alice',failNext=false,delayNext=0;const acknowledgements={alice:new Map(),bob:new Map()};
const summary=()=>({unacknowledged:events.filter(e=>!acknowledgements[user].has(e.id)).length,latest_id:60,started_at:now-86400});
const server=http.createServer((req,res)=>{const file=path.join(process.cwd(),'harbour/static',req.url==='/'?'index.html':req.url.replace('/static/',''));try{res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html');res.end(fs.readFileSync(file));}catch{res.writeHead(404).end();}});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const executable=process.env.HARBOUR_TEST_BROWSER||(fs.existsSync(chromium.executablePath())?chromium.executablePath():'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome');
 const browser=await chromium.launch({headless:true,executablePath:executable});
 try{
  const page=await browser.newPage({viewport:{width:1360,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/api/**',async route=>{
   const request=route.request(),url=new URL(request.url()),p=url.searchParams;let data;
   if(url.pathname==='/api/config')data={demo:true,version:'test'};
   else if(url.pathname==='/api/me')data={id:user,name:user,role:'user',csrf:'test'};
   else if(url.pathname==='/api/session/activity')data={ok:true};
   else if(url.pathname==='/api/dashboard')data={servers:[{...host,warnings:host.warnings.map(w=>({...w,dismissed:acknowledgements[user].has(w.event_id)}))}],thresholds,jobs:[],warning_events:summary()};
   else if(url.pathname.endsWith('/history'))data={from:now-3600,to:now,hours:1,resolution_seconds:60,poll_seconds:60,points:[],disk_mounts:[]};
   else if(url.pathname==='/api/warning-events'){
    if(failNext){failNext=false;await route.fulfill({status:503,json:{detail:'History temporarily unavailable'}});return;}
    let rows=events.map(e=>({...e,acknowledged_at:acknowledgements[user].get(e.id)||null})).sort((a,b)=>b.id-a.id);
    if(p.get('acknowledgement')==='unacknowledged')rows=rows.filter(e=>!e.acknowledged_at);
    if(p.get('acknowledgement')==='acknowledged')rows=rows.filter(e=>e.acknowledged_at);
    if(p.get('status')&&p.get('status')!=='all')rows=rows.filter(e=>e.status===p.get('status'));
    if(p.get('search'))rows=rows.filter(e=>e.title.includes(p.get('search')));
    if(p.get('before'))rows=rows.filter(e=>e.id<Number(p.get('before')));
    const slice=rows.slice(0,50);data={events:slice,next_before:rows.length>50?slice.at(-1).id:null,retention_days:90,...summary()};
    const delay=delayNext;delayNext=0;if(delay)await new Promise(resolve=>setTimeout(resolve,delay));
   }else if(url.pathname.endsWith('/acknowledgement')){
    const id=Number(url.pathname.split('/')[3]);assert.equal(request.method(),'PUT');
    request.postDataJSON().acknowledged?acknowledgements[user].set(id,now):acknowledgements[user].delete(id);data={ok:true};
   }else if(url.pathname==='/api/dismiss-all'){let dismissed=0;for(const e of events)if(!acknowledgements[user].has(e.id)){acknowledgements[user].set(e.id,now);dismissed++;}data={ok:true,dismissed};}
   else if(url.pathname==='/api/dismissals'){acknowledgements[user].clear();data={ok:true};}
   else throw Error('Unexpected API request '+url.pathname);
   await route.fulfill({json:data});
  });
  await page.goto('http://127.0.0.1:'+server.address().port);await page.locator('.server-item').waitFor();
  const badge=page.locator('[data-action=warnings] b');assert.equal(await badge.textContent(),'60');
  await page.locator('[data-action=warnings]').click();await page.locator('[data-key="warning-event:59"]').waitFor();
  assert.equal(await page.locator('.warning-event img').count(),0);
  const resolved=page.locator('[data-key="warning-event:59"]');assert.match(await resolved.textContent(),/Resolved/);assert.match(await resolved.textContent(),/Peak: 96 °C/);
  await resolved.locator('summary').click();assert.match(await resolved.textContent(),/High: 80 °C/);assert.match(await resolved.textContent(),/interrupted/);
  await resolved.locator('[data-action=warning-ack]').click();await resolved.waitFor({state:'detached'});assert.equal(await badge.textContent(),'59');
  await page.locator('[data-key="warning-event:60"] [data-action=warning-ack]').click();await page.locator('[data-key="warning-event:60"]').waitFor({state:'detached'});
  assert.equal(await page.locator('.server-item.has-warning').count(),1);
  await page.locator('[data-kind=history]').click();await page.locator('#warning-history-filter').waitFor();await page.locator('[data-key="warning-event:59"]').waitFor();
  await page.locator('[name=ack]').selectOption('acknowledged');await page.getByRole('button',{name:'Apply filters',exact:true}).click();await page.waitForFunction(()=>!warningHistory.pending);
  assert.equal(await page.locator('.warning-event').count(),2);
  await page.locator('[data-key="warning-event:59"] [data-action=warning-ack]').click();await page.locator('[data-key="warning-event:59"]').waitFor({state:'detached'});
  await page.locator('[name=ack]').selectOption('all');await page.getByRole('button',{name:'Apply filters',exact:true}).click();await page.waitForFunction(()=>!warningHistory.pending);
  await page.getByRole('button',{name:'Older',exact:true}).click();await page.waitForFunction(()=>!warningHistory.pending);assert.equal(await page.locator('.warning-event').count(),10);
  await page.getByRole('button',{name:'Newer',exact:true}).click();await page.waitForFunction(()=>!warningHistory.pending);assert.equal(await page.locator('.warning-event').count(),50);
  delayNext=400;await page.locator('[data-action=warning-refresh]').click();await page.locator('[name=search]').fill('NVMe sensor 3');await page.waitForFunction(()=>!warningHistory.pending);assert.equal(await page.locator('[name=search]').inputValue(),'NVMe sensor 3');
  await page.locator('[name=status]').selectOption('resolved');await page.getByRole('button',{name:'Apply filters',exact:true}).click();await page.waitForFunction(()=>!warningHistory.pending);
  assert.ok(await page.locator('.warning-event').count()>0);assert.ok((await page.locator('.warning-event .notice-title').allTextContents()).every(t=>t.includes('NVMe sensor 3')));
  failNext=true;await page.locator('[data-action=warning-refresh]').click();await page.getByRole('alert').filter({hasText:'History temporarily unavailable'}).waitFor();assert.ok(await page.locator('.warning-event').count()>0);
  await page.locator('[data-action=warning-refresh]').click();await page.waitForFunction(()=>!warningHistory.pending&&!warningHistory.error);
  await page.screenshot({path:'test-results/warning-history-desktop.png'});
  await page.setViewportSize({width:390,height:844});await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  assert.equal(await page.getByRole('heading',{name:'Notifications',exact:true}).count(),1);
  await page.screenshot({path:'test-results/warning-history-mobile.png'});
  assert.equal(await page.locator('.warning-history-drawer').evaluate(el=>el.scrollWidth<=el.clientWidth),true);
  await page.getByRole('button',{name:'Close notifications',exact:true}).click();await page.reload();await page.locator('.server-item').waitFor();assert.equal(await badge.textContent(),'59');
  user='bob';await page.reload();await page.locator('.server-item').waitFor();assert.equal(await badge.textContent(),'60');
  await page.locator('[data-action=warnings]').click();await page.locator('.warning-event').first().waitFor();
  await page.getByRole('button',{name:'Acknowledge warnings & dismiss updates',exact:true}).click();await page.getByText('Nothing new',{exact:true}).waitFor();assert.equal(await badge.textContent(),'0');
  await page.locator('[data-kind=history]').click();await page.locator('.warning-event').first().waitFor();assert.equal(await page.locator('.warning-event').count(),50);
  assert.deepEqual(errors,[]);
  console.log('PASS: resolved warning inbox, peaks and limits, safe labels, per-user acknowledgement and reload, persistent active indicators, searchable history, pagination, focus preservation, error recovery, bulk acknowledgement and mobile fit');
 }finally{await browser.close();server.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
