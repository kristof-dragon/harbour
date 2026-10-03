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
let dashboard={servers,thresholds,jobs:[queued,job]},requests=0,refreshAll=0;
const history={from:now-3600,to:now,hours:1,resolution_seconds:60,poll_seconds:15,points:Array.from({length:61},(_,i)=>({time:now-3600+i*60,cpu:i===30?null:20+i/10,memory:40,disk:42,temperature:48,cpu_peak:30,temperature_peak:52,disks:[],samples:4,attempts:4})),disk_mounts:['/']};
const server=http.createServer((req,res)=>{const file=path.join(process.cwd(),'harbour/static',req.url==='/'?'index.html':req.url.replace('/static/',''));try{res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html');res.end(fs.readFileSync(file));}catch{res.writeHead(404).end();}});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const executable=process.env.HARBOUR_TEST_BROWSER|| (fs.existsSync(chromium.executablePath())?chromium.executablePath():'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome');
 const browser=await chromium.launch({headless:true,executablePath:executable});
 try{
 const page=await browser.newPage({viewport:{width:1280,height:800}}),errors=[];page.on('pageerror',err=>errors.push(err.message));
 await page.route('**/api/**',async route=>{const url=new URL(route.request().url());let data;
 if(url.pathname==='/api/config')data={demo:true,version:'test'};
 else if(url.pathname==='/api/me')data={id:'admin',name:'admin',role:'admin',csrf:'test'};
 else if(url.pathname==='/api/dashboard'){requests++;data=dashboard;}
 else if(url.pathname==='/api/jobs/task')data=job;
 else if(url.pathname.endsWith('/history'))data=history;
 else if(url.pathname==='/api/servers/refresh-all'){refreshAll++;data={jobs:[],errors:[]};}
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
 assert.deepEqual(errors,[]);assert.ok(requests>=4);
 console.log('PASS: changed-value rendering, desktop/mobile scroll, focus/caret, expanded panels, persistent animation, live progress/log scroll, task counts, collapsible filters, refresh-all, chart hover/touch/keyboard/missing values');
 }finally{await browser.close();server.close();}
})().catch(err=>{console.error(err);server.close();process.exitCode=1;});
