// Real-browser regression checks; isolated synthetic API, no SSH or Docker actions.
// Run with Playwright available in NODE_PATH (or installed locally).
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {chromium}=require('playwright');
const now=Math.floor(Date.now()/1000),thresholds={cpu:85,memory:85,disk:85,disk_free_gb:10,temperature:80};
const service={id:'web',name:'web',container:'stack-web-1',project:'stack',image:'example/web:latest',version:'1.0',image_id:'sha256:abc',health:'healthy',state:'running',ports:[],mounts:[],working_dir:'/stack',config_files:['/stack/compose.yaml'],update:{status:'available',version:'1.1'},restart_policy:'always'};
const metrics={cpu:24.2,cores:4,memory:{percent:42,total:8e9,used:3.36e9},disks:[{mount:'/',total:100e9,used:42e9,free:58e9,percent:42,monitor:true,present:true,card:true,warn:true}],os:'Linux',kernel:'6.1',docker:'29',uptime:3600,temperature:{package:48,package_label:'CPU package',sensors:[{label:'CPU package',celsius:48,kind:'cpu_package'}]}};
const servers=Array.from({length:24},(_,i)=>({id:'host-'+i,name:'Host '+i,host:'192.0.2.'+(i+1),port:22,metrics:structuredClone(metrics),services:[structuredClone(service)],warnings:[],updates:1,checked:now,update_checked:now,monitoring_enabled:true,connection_status:'up',poll_seconds:15,latency_ms:2,thresholds,server_type:'docker'}));
let job={id:'task',server_id:'host-0',server_name:'Host 0',actor:'admin',action:'pull_up',status:'running',created:now,progress:{completed:0,total:2,label:'Pulling web',phase:'executing',started:now,heartbeat:now},target_names:['stack / web'],output:Array.from({length:100},(_,i)=>'Download layer '+i).join('\n')};
let queued={...structuredClone(job),id:'queued',status:'queued',queue_position:1,waiting_for:'pull_up',progress:{}};
let dashboard={servers,thresholds,jobs:[queued,job]},requests=0,refreshAll=0,lastPlan=null;
const history={from:now-3600,to:now,hours:1,resolution_seconds:60,poll_seconds:15,points:Array.from({length:61},(_,i)=>({time:now-3600+i*60,cpu:i===30?null:20+i/10,memory:40,disk:42,temperature:48,cpu_peak:30,temperature_peak:52,disks:[],samples:4,attempts:4})),disk_mounts:['/']};
let telegram={enabled:false,chat_id:'',token_saved:false,demo:true,last_sent:null,last_error:'',next_attempt:0,rules:[],servers:servers.map(s=>({id:s.id,name:s.name,monitoring_enabled:true}))},telegramTests=0;
const server=http.createServer((req,res)=>{const file=path.join(process.cwd(),'harbour/static',req.url==='/'?'index.html':req.url.replace('/static/',''));try{res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html');res.end(fs.readFileSync(file));}catch{res.writeHead(404).end();}});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const executable=process.env.HARBOUR_TEST_BROWSER|| (fs.existsSync(chromium.executablePath())?chromium.executablePath():'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome');
 const browser=await chromium.launch({headless:true,executablePath:executable});
 try{
 const page=await browser.newPage({viewport:{width:1280,height:800}}),errors=[];page.on('pageerror',err=>errors.push(err.message));
 await page.route('**/api/**',async route=>{const url=new URL(route.request().url());let data;
 if(url.pathname==='/api/config')data={demo:true,version:'test'};
 else if(url.pathname==='/api/notifications'){if(route.request().method()==='PUT'){const body=route.request().postDataJSON();assert.equal(body.rules.length,servers.length*4);telegram={...telegram,...body,token_saved:!!body.bot_token||telegram.token_saved};delete telegram.bot_token;}data=telegram;}
 else if(url.pathname==='/api/notifications/test'){telegramTests++;data={ok:true,simulated:true};}
 else if(url.pathname==='/api/me')data={id:'admin',name:'admin',role:'admin',csrf:'test'};
 else if(url.pathname==='/api/dashboard'){requests++;data=dashboard;}
 else if(url.pathname==='/api/jobs/task')data=job;
 else if(url.pathname.endsWith('/history'))data=history;
 else if(url.pathname==='/api/servers/refresh-all'){refreshAll++;data={jobs:[],errors:[]};}
 else if(url.pathname.endsWith('/plan')){lastPlan=route.request().postDataJSON();data={token:'test-plan',commands:[]};}
 else if(url.pathname==='/api/dismiss-all'){
  let dismissed=0;
  for(const s of dashboard.servers){for(const w of s.warnings){if(!w.dismissed)dismissed++;w.dismissed=true;}for(const c of s.services){if(c.update?.status==='available'&&!c.dismissed){dismissed++;c.dismissed=true;}}s.updates=0;}
  data={ok:true,dismissed};
 }
 else throw Error('Unexpected request '+url.pathname);
 await route.fulfill({json:data});});
 await page.goto('http://127.0.0.1:'+server.address().port);await page.locator('.server-item').first().waitFor();await page.locator('[data-card-chart=cpu] svg').waitFor();
 assert.equal(await page.locator('[data-key="server:host-0"] .task-badge b').textContent(),'1');
 assert.match(await page.locator('[data-key="server:host-0"] .task-badge').getAttribute('aria-label'),/Pull & Apply running/);
 // Keep native controls, open details, chart objects, text caret and scroll during real data changes.
 await page.locator('details.group>summary').click();await page.locator('.sensor-details>summary').click();await page.locator('#sidebar-menu>summary').click();
 await page.locator('#service-search').fill('web');
 await page.evaluate(()=>{const search=document.querySelector('#service-search');search.setSelectionRange(1,2);const list=document.querySelector('.server-list');list.scrollTop=450;window.proof={list,scroll:list.scrollTop,search,group:document.querySelector('details.group'),sensor:document.querySelector('.sensor-details'),chart:document.querySelector('[data-card-chart=cpu] svg'),menu:document.querySelector('#sidebar-menu'),spin:document.querySelector('.task-running .icon')};window.spinAnimation=proof.spin.getAnimations()[0];window.spinStart=spinAnimation.startTime;});
 dashboard.servers[0].metrics.cpu=37.1;job.progress.heartbeat++;await page.evaluate(()=>load());
 assert.deepEqual(await page.evaluate(()=>({list:proof.list===document.querySelector('.server-list'),scroll:proof.list.scrollTop===proof.scroll,search:proof.search===document.activeElement,caret:proof.search.selectionStart===1&&proof.search.selectionEnd===2,group:proof.group.open,sensor:proof.sensor.open,chart:proof.chart===document.querySelector('[data-card-chart=cpu] svg'),menu:proof.menu.open,animation:spinAnimation===proof.spin.getAnimations()[0]&&spinStart===spinAnimation.startTime})),{list:true,scroll:true,search:true,caret:true,group:true,sensor:true,chart:true,menu:true,animation:true});
 assert.match(await page.locator('[data-key="metric:cpu"] .metric-value').textContent(),/37.1/);
 // An unchanged render must produce zero mutations in the dashboard.
 assert.equal(await page.evaluate(()=>{const observer=new MutationObserver(()=>{});observer.observe(document.querySelector('#shell'),{subtree:true,attributes:true,childList:true,characterData:true});renderSidebar();renderTop();renderMain();const n=observer.takeRecords().length;observer.disconnect();return n;}),0);
 // Mobile menu/list retain their exact scrollers through background polls.
 await page.setViewportSize({width:390,height:844});await page.getByRole('button',{name:'Toggle server list'}).click();
 await page.evaluate(()=>{const list=document.querySelector('.server-list');list.scrollTop=300;window.mobileProof={list,scroll:list.scrollTop,menu:document.querySelector('#sidebar-menu'),top:document.querySelector('#sidebar-menu').scrollTop};});
 dashboard.servers[0].metrics.cpu=39;await page.evaluate(()=>load());
 assert.equal(await page.evaluate(()=>mobileProof.list===document.querySelector('.server-list')&&mobileProof.list.scrollTop===mobileProof.scroll&&document.querySelector('#sidebar').classList.contains('mobile-open')&&mobileProof.menu.open),true);
 assert.deepEqual(await page.evaluate(()=>[...document.querySelectorAll('#shell *')].filter(el=>el.getBoundingClientRect().right>innerWidth+1&&getComputedStyle(el).position!=='fixed'&&!el.closest('.service-table')).map(el=>[el.tagName,el.className,Math.round(el.getBoundingClientRect().right)]).slice(0,20)),[]);
 await page.locator('#server-filters-panel>summary').click();dashboard.servers[0].metrics.cpu=40;await page.evaluate(()=>load());assert.equal(await page.locator('#server-filters-panel').getAttribute('open'),null);
 await page.getByRole('button',{name:'Close server list',exact:true}).click();await page.reload();assert.equal(await page.locator('#server-filters-panel').getAttribute('open'),null);
 await page.setViewportSize({width:1280,height:800});await page.getByRole('button',{name:'Refresh all servers',exact:true}).click();assert.equal(refreshAll,1);
 // Live bar animation is the same object across one-second job polls, even with new log output.
 await page.evaluate(()=>openJob('task'));await page.waitForTimeout(100);await page.evaluate(()=>{window.bar=document.querySelector('#job-progress .job-indeterminate i');window.animation=bar.getAnimations()[0];window.animationStart=animation.startTime;window.output=document.querySelector('#job-output');output.scrollTop=0;});
 job.output+='\nAnother layer';await page.waitForTimeout(2200);
 assert.deepEqual(await page.evaluate(()=>({node:bar===document.querySelector('#job-progress .job-indeterminate i'),animation:animation===bar.getAnimations()[0],start:animationStart===animation.startTime,scroll:output.scrollTop})),{node:true,animation:true,start:true,scroll:0});
 assert.match(await page.locator('#job-progress').textContent(),/Elapsed/);
 job.progress.completed=1;await page.waitForTimeout(1100);assert.equal(await page.locator('#job-progress progress').getAttribute('value'),'1');
 job.status='succeeded';job.progress.completed=2;job.finished=now+8;await page.waitForTimeout(1100);assert.equal(await page.locator('#job-progress progress').getAttribute('value'),'2');
 await page.getByRole('button',{name:'Close dialog',exact:true}).click();
 // Hover, absent readings, multiple series, keyboard and touch inspection.
 await page.getByRole('button',{name:'Explore history',exact:true}).click();await page.locator('[data-chart-keys]').waitFor();
 await page.evaluate(()=>{historyState.mode='combined';renderHistory();const svg=document.querySelector('[data-chart-keys]');const p=new DOMPoint(55+785*.25,120).matrixTransform(svg.getScreenCTM());svg.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,clientX:p.x,clientY:p.y}));});
 assert.equal(await page.locator('.chart-tooltip').isVisible(),true);assert.match(await page.locator('.chart-tooltip').textContent(),/CPU.*21.5%/);assert.match(await page.locator('.chart-tooltip').textContent(),/CPU \/ SoC.*48.0°C/);
 assert.equal(await page.locator('.chart-cursor').getAttribute('visibility'),'visible');
 await page.evaluate(()=>showChartReading(document.querySelector('[data-chart-keys]'),historyState.data.points[30].time));assert.match(await page.locator('.chart-tooltip').textContent(),/No sample/);
 await page.locator('[data-chart-keys]').focus();await page.keyboard.press('End');assert.match(await page.locator('.chart-tooltip').textContent(),/26.0%/);await page.keyboard.press('Escape');assert.equal(await page.locator('.chart-tooltip').isVisible(),false);assert.equal(await page.locator('.history-modal').isVisible(),true);
 await page.evaluate(()=>{const svg=document.querySelector('[data-chart-keys]'),p=new DOMPoint(450,120).matrixTransform(svg.getScreenCTM());svg.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerType:'touch',clientX:p.x,clientY:p.y}));});assert.equal(await page.locator('.chart-tooltip').isVisible(),true);
 assert.equal(await page.evaluate(()=>{const svg=document.querySelector('[data-chart-keys]'),bounds=document.querySelector('.history-content').getBoundingClientRect();return new DOMPoint(400,301).matrixTransform(svg.getScreenCTM()).y<=bounds.bottom;}),true);
 if(process.env.HARBOUR_TEST_CAPTURE){fs.mkdirSync('test-results',{recursive:true});await page.evaluate(()=>{historyState.hours=1;renderHistory();showChartReading(document.querySelector('[data-chart-keys]'),historyState.data.points[15].time);});await page.screenshot({path:'test-results/history-tooltip-v017.png'});}
 await page.getByRole('button',{name:'Close dialog',exact:true}).click();
 // Live status changes keep the icon nodes and match between container and Activity views.
 const c=dashboard.servers[0].services[0];job.targets='["old-container-id"]';job.target_names=['stack / '+c.container];
 for(const theme of ['light','dark']){
  await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
  for(const [runtime,health,tone,label] of [['running','healthy','green','healthy'],['running','unhealthy','red','unhealthy'],['running','starting','yellow','Starting'],['exited','unhealthy','grey','Stopped'],['running',null,'green','no health check']]){
   c.state=runtime;c.health=health;await page.evaluate(()=>load());
   await page.evaluate(()=>{state.tab='containers';renderMain();});
   const group=page.locator('details.group>summary .status-icon');
   if(await page.locator('details.group').getAttribute('open')===null)await page.locator('details.group>summary').click();
   const container=page.locator('.service-summary .status-icon');
   assert.match(await group.getAttribute('class'),new RegExp('status-'+tone));assert.match(await container.getAttribute('aria-label'),new RegExp(label));
   const colour=await container.evaluate(el=>getComputedStyle(el).color);
   await page.evaluate(()=>{state.tab='activity';renderMain();});
   const activity=page.locator('[data-key="job:task"] .status-icon');
   assert.match(await activity.getAttribute('class'),new RegExp('status-'+tone));assert.equal(await activity.evaluate(el=>getComputedStyle(el).color),colour);
   assert.equal(await page.locator('[data-key="job:task"]>.tag').textContent(),'succeeded');
  }
 }
 // A mixed stack prioritises unhealthy / starting services; absent targets are never guessed healthy.
 c.state='running';c.health='healthy';
 dashboard.servers[0].services.push({...structuredClone(c),id:'starting',name:'starting',container:'stack-starting-1',health:'starting'});
 await page.evaluate(()=>load());await page.evaluate(()=>{state.tab='containers';state.search='';renderMain();});
 assert.match(await page.locator('details.group>summary .status-icon').getAttribute('class'),/status-yellow/);
 dashboard.servers[0].services[1].health='unhealthy';await page.evaluate(()=>load());
 assert.match(await page.locator('details.group>summary .status-icon').getAttribute('class'),/status-red/);
 dashboard.servers[0].services.splice(1);c.project=null;job.target_names=['removed-container'];
 await page.evaluate(()=>load());await page.evaluate(()=>{state.tab='activity';renderMain();});
 assert.match(await page.locator('[data-key="job:task"] .status-icon').getAttribute('class'),/status-grey/);
 // Only confirmed newer versions get update badges and the Available version line.
 c.project='stack';c.version='26.09.2';
 for(const [status,version,updates,label] of [['current','26.09.2',0,'Current'],['available','26.10.0',1,'Update'],['current','26.08.0',0,'Current'],['unverified',null,0,'Version unverified']]){
  c.update={status,version,digest:'sha256:candidate',reason:'Version comparison'};dashboard.servers[0].updates=updates;
  await page.evaluate(()=>load());await page.evaluate(()=>{state.tab='containers';renderMain();});
  if(await page.locator('details.group').getAttribute('open')===null)await page.locator('details.group>summary').click();
  assert.equal(await page.locator('.service-summary>.tag').textContent(),status==='available'?' Update':label);
  assert.equal(await page.locator('.available-version').count(),updates);
  assert.match(await page.locator('.running-version').textContent(),/26\.09\.2/);
  assert.equal(await page.locator('details.group>summary .group-updates').textContent(),updates?' 1':status==='unverified'?'Not verified':'No updates');
  assert.equal(await page.locator('[data-key="server:host-0"] .count-badge.update').count(),updates);
 }
 c.project=null;c.update={status:'available',version:'26.10.0',digest:'sha256:candidate'};dashboard.servers[0].updates=1;
 // Header filters combine with search and constrain bulk actions to matching services.
 const originalServices=dashboard.servers[0].services;
 dashboard.servers[0].services=[['web','stack','running',true],['worker','stack','exited',true],['db','stack','running',false],['boot','stack','restarting',true],['idle',null,'exited',false],['paused',null,'paused',false]].map(([id,project,runtime,update])=>({...structuredClone(c),id,name:id,container:id,project,image:'example/'+id+':latest',state:runtime,update:{status:update?'available':'current',version:update?'26.10.0':'26.09.2'}}));
 await page.evaluate(()=>load());await page.evaluate(()=>{state.tab='containers';state.search='';state.selected.clear();renderMain();});
 assert.equal(await page.locator('.group-name .group-services').textContent(),'4 services');
 assert.equal(await page.locator('.group-name .group-updates').getAttribute('aria-label'),'3 updates available');
 assert.equal(await page.locator('.group-summary>.tag,.group-summary>.muted').count(),0);
 assert.equal(await page.locator('.table-heading .sliding-control').count(),2);
 const statusFilter=page.locator('#container-filter-status'),updateFilter=page.locator('#container-filter-updates');
 const visibleIds=()=>page.locator('.service').evaluateAll(nodes=>nodes.map(n=>n.dataset.open.slice(8)));
 await page.getByRole('checkbox',{name:'Select stack stack',exact:true}).check();
 await statusFilter.getByRole('button',{name:'Running',exact:true}).click();
 assert.deepEqual(await visibleIds(),['web','db','boot']);
 assert.deepEqual(await page.evaluate(()=>[...state.selected]),['web','db','boot']);
 assert.equal(await page.locator('.group-services').textContent(),'3 of 4 services');
 await updateFilter.getByRole('button',{name:'Updates',exact:true}).click();
 assert.deepEqual(await visibleIds(),['web','boot']);
 await page.locator('[data-action=bulk][data-kind=pull_up]').click();
 assert.deepEqual(lastPlan,{action:'pull_up',targets:['web','boot']});
 await page.getByRole('button',{name:'Cancel',exact:true}).click();
 await page.locator('#service-search').fill('web');assert.deepEqual(await visibleIds(),['web']);
 assert.deepEqual(await page.evaluate(()=>[...state.selected]),['web']);
 await page.locator('#service-search').fill('');
 dashboard.servers[0].metrics.cpu++;await page.evaluate(()=>load());
 assert.equal(await statusFilter.getByRole('button',{name:'Running',exact:true}).getAttribute('aria-pressed'),'true');
 assert.deepEqual(await visibleIds(),['web','boot']);
 await statusFilter.getByRole('button',{name:'Stopped',exact:true}).click();assert.deepEqual(await visibleIds(),['worker']);
 await page.getByRole('checkbox',{name:'Select all visible services',exact:true}).check();
 assert.deepEqual(await page.evaluate(()=>[...state.selected]),['worker']);
 await updateFilter.getByRole('button',{name:'No updates',exact:true}).click();assert.deepEqual(await visibleIds(),['idle','paused']);
 assert.deepEqual(await page.evaluate(()=>[...state.selected]),[]);
 await updateFilter.getByRole('button',{name:'No updates',exact:true}).click();
 await statusFilter.getByRole('button',{name:'Stopped',exact:true}).click();assert.equal((await visibleIds()).length,6);
 await page.getByRole('checkbox',{name:'Select all visible services',exact:true}).check();
 assert.deepEqual(await page.evaluate(()=>[...state.selected]),['group:stack','idle','paused']);
 await page.getByRole('checkbox',{name:'Select all visible services',exact:true}).uncheck();
 await page.locator('#service-search').fill('absent');assert.equal(await page.getByText('No matching containers',{exact:true}).isVisible(),true);
 await page.locator('#service-search').fill('');
 if(process.env.HARBOUR_TEST_CAPTURE){
  fs.mkdirSync('test-results',{recursive:true});
  await page.evaluate(()=>document.querySelector('#toasts').style.visibility='hidden');
  for(const theme of ['dark','light']){await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);await page.locator('.service-table').screenshot({path:`test-results/stack-chips-${theme}-v0112.png`});}
 }
 await page.setViewportSize({width:390,height:844});
 await page.evaluate(()=>setMobilePanel(false));await page.waitForTimeout(300);
 await statusFilter.getByRole('button',{name:'Running',exact:true}).click();
 assert.deepEqual(await visibleIds(),['web','db','boot']);
 assert.equal(await page.locator('.container-filters').evaluate(el=>el.getBoundingClientRect().right<=innerWidth),true);
 if(process.env.HARBOUR_TEST_CAPTURE)await page.locator('.service-table').screenshot({path:'test-results/stack-chips-mobile-v0112.png'});
 await statusFilter.getByRole('button',{name:'Running',exact:true}).click();
 await page.setViewportSize({width:1280,height:800});dashboard.servers[0].services=originalServices;await page.evaluate(()=>load());
 // Dismiss all empties notices but leaves resource warnings and server indicators visible.
 dashboard.servers[0].warnings=[{id:'cpu',title:'CPU usage is high',detail:'99% used',dismissed:false}];
 await page.evaluate(()=>load());await page.getByRole('button',{name:'Open notifications',exact:true}).click();
 assert.ok(await page.locator('.notice').count()>0);await page.getByRole('button',{name:'Dismiss all',exact:true}).click();
 await page.getByText('Nothing new',{exact:true}).waitFor();assert.equal(await page.locator('.notice').count(),0);
 assert.equal(await page.getByRole('button',{name:'Dismiss all',exact:true}).isDisabled(),true);
 assert.equal(await page.locator('[data-key="server:host-0"] .count-badge.warn').textContent(),'1');
 assert.equal(await page.locator('[data-action=warnings] b').textContent(),'0');
 await page.getByRole('button',{name:'Close notifications',exact:true}).click();
 await page.setViewportSize({width:390,height:844});await page.evaluate(()=>{state.tab='containers';renderMain();});
 assert.equal(await page.locator('.standalone .status-icon').isVisible(),true);
 assert.equal(await page.locator('.standalone .status-icon').evaluate(el=>el.getBoundingClientRect().right<=innerWidth),true);
 if(process.env.HARBOUR_TEST_CAPTURE){
  dashboard.servers[0].services=[['healthy','running','healthy'],['stopped','exited','unhealthy'],['unhealthy','running','unhealthy'],['starting','running','starting']].map(([id,runtime,health])=>({...structuredClone(c),id,name:id,container:id,state:runtime,health}));
  await page.evaluate(()=>{setMobilePanel(false);document.querySelector('#toasts').style.visibility='hidden';});await page.evaluate(()=>load());
  await page.locator('.service-table').scrollIntoViewIfNeeded();await page.screenshot({path:'test-results/container-status-mobile-v0110.png'});
  await page.setViewportSize({width:1280,height:900});
  for(const theme of ['dark','light']){await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);await page.locator('.service-table').scrollIntoViewIfNeeded();await page.screenshot({path:`test-results/container-status-${theme}-v0110.png`});}
  dashboard.jobs=dashboard.servers[0].services.map((c,i)=>({...job,id:'colour-'+i,targets:JSON.stringify([c.id]),target_names:[c.container]}));
  await page.evaluate(()=>load());await page.evaluate(()=>{state.tab='activity';renderMain();});await page.locator('.activity-row').first().scrollIntoViewIfNeeded();await page.screenshot({path:'test-results/activity-status-v0110.png'});
 }
 // Telegram matrix: independent timers, saved credential handling, dirty guard and mobile layout.
 await page.setViewportSize({width:1280,height:900});
 await page.evaluate(()=>{setMobilePanel(false);document.querySelector('#sidebar-menu').open=true;});
 await page.getByRole('button',{name:'Notifications',exact:true}).click();
 await page.locator('#telegram-form').waitFor();
 assert.equal(await page.locator('.telegram-rule').count(),96);
 const cpu=page.getByRole('checkbox',{name:'Host 0 CPU notifications',exact:true});
 const delay=page.getByRole('spinbutton',{name:'Host 0 CPU trigger delay in minutes',exact:true});
 const repeat=page.getByRole('spinbutton',{name:'Host 0 CPU repeat interval in minutes',exact:true});
 assert.equal(await delay.isDisabled(),true);await cpu.check();await delay.fill('2');await repeat.fill('0');
 await page.getByRole('checkbox',{name:'Enable Telegram',exact:true}).check();
 await page.locator('[name=bot_token]').fill('123456789:synthetic_token_only_1234567890');await page.locator('[name=chat_id]').fill('-100123456789');
 await page.getByRole('button',{name:'Simulate test message',exact:true}).click();
 await page.getByText('Save your changes before sending a test message.',{exact:true}).waitFor();assert.equal(telegramTests,0);
 await page.getByRole('button',{name:'Close dialog',exact:true}).click();await page.getByRole('button',{name:'Keep editing',exact:true}).click();
 await page.getByRole('button',{name:'Save',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('[name=bot_token]').value==='');
 assert.equal(await page.locator('#telegram-form').isVisible(),true);
 assert.deepEqual(telegram.rules.find(r=>r.server_id==='host-0'&&r.kind==='cpu'),{server_id:'host-0',kind:'cpu',enabled:true,delay_seconds:120,repeat_seconds:0});
 assert.equal(await page.locator('[name=clear_token]').isDisabled(),false);
 await page.getByRole('button',{name:'Simulate test message',exact:true}).click();
 await page.getByText('Test simulated. No message was sent.',{exact:true}).waitFor();assert.equal(telegramTests,1);
 await page.evaluate(()=>load());assert.equal(await repeat.inputValue(),'0');
 if(process.env.HARBOUR_TEST_CAPTURE){for(const theme of ['dark','light']){await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);await page.locator('.telegram-modal').screenshot({path:`test-results/telegram-${theme}-v0113.png`});}}
 await page.setViewportSize({width:390,height:844});await page.waitForTimeout(300);
 assert.equal(await page.locator('.telegram-modal').evaluate(el=>el.getBoundingClientRect().right<=innerWidth),true);
 assert.equal(await page.locator('.telegram-table-wrap').evaluate(el=>el.scrollWidth>el.clientWidth),true);
 if(process.env.HARBOUR_TEST_CAPTURE)await page.locator('.telegram-modal').screenshot({path:'test-results/telegram-mobile-v0113.png'});
 await page.getByRole('button',{name:'Close dialog',exact:true}).click();assert.equal(await page.locator('#discard-guard').count(),0);
 // Load: current values, three-line overview, all explorer layouts and mobile inspection.
 Object.assign(dashboard.servers[0].metrics,{load1:1.75,load5:1.25,load15:.46});
 for(let i=0;i<history.points.length;i++){
  Object.assign(history.points[i],i<10||i===30?{load1:null,load5:null,load15:null}:{load1:1.15+i/100,load5:.65+i/100,load15:.4+i/1000,load1_peak:2.5,load5_peak:2,load15_peak:.8});
 }
 await page.setViewportSize({width:1280,height:900});
 await page.evaluate(async()=>{state.server='host-0';state.tab='containers';cardCache.clear();await load();await loadCardHistory(current(),true);});
 const loadCard=page.locator('[data-key="metric:load"]');
 assert.deepEqual(await loadCard.locator('.load-values b').allTextContents(),['1.75','1.25','0.46']);
 assert.equal(await loadCard.locator('polyline').count(),6,'Three lines each side of the long gap');
 if(process.env.HARBOUR_TEST_CAPTURE){await loadCard.scrollIntoViewIfNeeded();await page.screenshot({path:'test-results/load-overview.png'});}
 await page.getByRole('button',{name:'Explore Load average history',exact:true}).click();
 const loadPlot=page.locator('[data-chart-keys="load1,load5,load15"]');
 await loadPlot.waitFor();
 assert.equal(await loadPlot.locator('polyline').count(),6);
 assert.doesNotMatch(await loadPlot.textContent(),/%/);
 await loadPlot.focus();await page.keyboard.press('End');
 assert.match(await page.locator('.chart-tooltip').textContent(),/Load · 1 min1.75.*Load · 5 min1.25.*Load · 15 min0.46/);
 await page.keyboard.press('Home');assert.match(await page.locator('.chart-tooltip').textContent(),/No sample/);
 await page.locator('#history-stat').selectOption('peak');await loadPlot.focus();await page.keyboard.press('End');
 assert.match(await page.locator('.chart-tooltip').textContent(),/Load · 1 min2.50.*Load · 5 min2.00.*Load · 15 min0.80/);
 await page.locator('#history-stat').selectOption('average');
 for(const theme of ['dark','light']){
  await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
  if(process.env.HARBOUR_TEST_CAPTURE)await page.locator('.history-modal').screenshot({path:`test-results/load-history-${theme}.png`});
 }
 await page.getByRole('button',{name:'Combined',exact:true}).click();
 assert.equal(await page.locator('[data-chart-keys]').count(),2,'Load has a separate count axis in Combined');
 assert.equal(await page.locator('.load-legend span').count(),3);
 await page.getByRole('button',{name:'Side by side',exact:true}).click();
 assert.equal(await page.locator('.history-grid article').count(),5);
 await page.getByRole('button',{name:'Table',exact:true}).click();
 for(const label of ['Load · 1 min','Load · 5 min','Load · 15 min'])assert.equal(await page.getByRole('columnheader',{name:label,exact:true}).count(),1);
 await page.getByRole('button',{name:'Resource tabs',exact:true}).click();
 await page.getByRole('button',{name:'Load average',exact:true}).click();
 await page.setViewportSize({width:390,height:844});
 assert.equal(await loadPlot.evaluate(el=>el.getBoundingClientRect().right<=innerWidth),true);
 await loadPlot.focus();await page.keyboard.press('End');
 assert.equal(await page.locator('.chart-tooltip').evaluate(el=>el.getBoundingClientRect().right<=innerWidth),true);
 if(process.env.HARBOUR_TEST_CAPTURE)await page.locator('.history-modal').screenshot({path:'test-results/load-history-mobile.png'});
 await page.getByRole('button',{name:'Close dialog',exact:true}).click();
 assert.equal(await loadCard.evaluate(el=>el.getBoundingClientRect().right<=innerWidth),true);
 console.log('PASS: load overview and history, three series, averages/peaks, legacy gaps, all layouts, desktop/mobile and keyboard tooltips');
 // First CPU reading has no percentage; later cards describe the actual interval.
 dashboard.servers[0].metrics.cpu=null;dashboard.servers[0].metrics.cpu_sample_seconds=null;
 await page.evaluate(()=>load());
 const cpuCard=page.locator('[data-key="metric:cpu"]');
 assert.equal(await cpuCard.locator('.metric-value').textContent(),'—');
 assert.match(await cpuCard.textContent(),/Waiting for next poll/);
 assert.match(await page.locator('[data-key="server:host-0"] .resource-chip').first().textContent(),/—/);
 dashboard.servers[0].metrics.cpu=25;dashboard.servers[0].metrics.cpu_sample_seconds=180;
 await page.evaluate(()=>load());
 assert.equal(await cpuCard.locator('.metric-value').textContent(),'25.0%');
 assert.match(await cpuCard.textContent(),/3m 0s average/);
 dashboard.servers[0].metrics.cpu=0;dashboard.servers[0].metrics.cpu_sample_seconds=60;
 await page.evaluate(()=>load());
 assert.equal(await cpuCard.locator('.metric-value').textContent(),'0.0%');
 assert.match(await cpuCard.textContent(),/1m 0s average/);
 console.log('PASS: CPU baseline waiting state, actual interval label and genuine zero usage');
 // macOS uses the same resource cards for plain and Docker hosts; temperature
 // stays unavailable and resource history remains accessible for either type.
 dashboard.servers[0].metrics.os='macOS 15.7.1';dashboard.servers[0].metrics.kernel='24.6.0';
 dashboard.servers[0].metrics.temperature={package:null,sensors:[],package_count:0};
 for(const type of ['plain','docker']){
  dashboard.servers[0].server_type=type;await page.evaluate(()=>load());
  assert.match(await page.locator('.host-facts').textContent(),/macOS 15.7.1/);
  assert.equal(await page.locator('[data-key="metric:temperature"] .metric-value').textContent(),'—');
  assert.match(await page.locator('[data-key="metric:load"]').textContent(),/system load/);
  assert.equal(await page.getByRole('button',{name:'Explore history',exact:true}).count(),1);
  assert.equal(await page.locator('#service-search').count(),type==='docker'?1:0);
 }
 await page.evaluate(()=>onboard());
 assert.match(await page.locator('#onboard-form').textContent(),/Monitor Linux or macOS using either server type/);
 assert.match(await page.locator('#onboard-form').textContent(),/Remote Login/);
 assert.equal(await page.locator('[name="server_type"] option').count(),2);
 await page.locator('[name="username"]').fill('Mac.User');
 assert.equal(await page.locator('[name="username"]').evaluate(el=>el.checkValidity()),true);
 await page.locator('[name="username"]').fill('user;id');
 assert.equal(await page.locator('[name="username"]').evaluate(el=>el.checkValidity()),false);
 await page.locator('[name="username"]').fill('harbour');
 assert.equal(await page.locator('.modal').evaluate(el=>el.getBoundingClientRect().right<=innerWidth),true);
 await page.getByRole('button',{name:'Close dialog',exact:true}).click();
 console.log('PASS: macOS plain and Docker cards, unavailable temperature, history and mobile onboarding');
 assert.deepEqual(errors,[]);assert.ok(requests>=4);
 console.log('PASS: changed-value rendering, desktop/mobile scroll, focus/caret, expanded panels, persistent animation, live progress/log scroll, task counts, collapsible filters, refresh-all, chart hover/touch/keyboard/missing values, container and Activity status colours, bulk notification dismissal, inline stack chips, combined service filters and matching-only bulk actions');
 }finally{await browser.close();server.close();}
})().catch(err=>{console.error(err);server.close();process.exitCode=1;});
