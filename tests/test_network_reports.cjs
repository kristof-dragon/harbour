// Printable sample uses documentation addresses and synthetic measurements only.
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path'),{spawnSync}=require('node:child_process');
const {chromium}=require('playwright');
fs.mkdirSync('test-results',{recursive:true});
const generated=spawnSync('.venv/bin/python',['-c',`
import sys,datetime as dt,json
sys.path.insert(0,'tests')
from test_incidents import probe,telemetry
from harbour import report_facts as f,report_html,network_reports as r
start,end=r.bounds('2026-10-13','Europe/London');at=start+9*3600+14*60
rows=[probe(at-10,rtt_ms=4,sample_key='healthy'),probe(at,rtt_ms=220,sample_key='delayed'),probe(at+1,status='timeout',rtt_ms=None,sample_key='timeout'),probe(at+2,rtt_ms=5,sample_key='recovered'),telemetry(at)]
parts=f.compact(rows,[{'id':'example-marker','measured':at,'label':'Latency and timeout observed','automatic':1,'complete':1,'trigger':{'at':at,'rtt_ms':220,'latency_limit_ms':150}}],{'latency_limit_ms':150},[],'a'*32,1)
day={'day':'2026-10-13','timezone':'Europe/London','start':start,'end':end,'generated':end+3*3600,'paths':f.path_totals(parts['minutes']),'timeline':f.timeline(parts['minutes'],parts['markers']),'calls':[],'kinds':{'probe':4,'telemetry':1},'markers':1,'minutes_with_recordings':1,'minutes_in_day':1440,'manifests':[{'archive_id':'a'*32}],'quality':['Synthetic preview: these are illustrative measurements, not findings from sasdc.','Root cause is not established.']}
period={'start_day':'2026-10-13','end_day':'2026-10-13','timezone':'Europe/London','days':[day],'missing_days':[]}
open('test-results/daily-report-preview.html','w').write(report_html.render('Example router · synthetic preview',period))
print(json.dumps({'policy':r.Policy().model_dump(),'state':{'last_success':end+3*3600},'next_run':end+27*3600,'days':[{'day':'2026-10-13','generated':end+3*3600,'bytes':12000}],'stored_bytes':{'daily_reports':12000},'budget_exceeded':False}))
`],{encoding:'utf8'});
assert.equal(generated.status,0,generated.stderr);const listing=JSON.parse(generated.stdout);
const gib=1024**3;
listing.storage={bytes:9*gib,reusable_database_bytes:5*gib,complete:true,pressure:'normal',filesystem:{total:512*gib,used:192*gib,free:320*gib},categories:{database:{bytes:6*gib},recordings:{bytes:3*gib},recovery:{bytes:0},temporary:{bytes:0},other:{bytes:0}},scope:'Synthetic storage preview, including reusable database pages.'};
const now=Date.now()/1000,thresholds={cpu:85,memory:85,disk:85,disk_free_gb:10,temperature:80};
const host={id:'router',name:'Example router',host:'192.0.2.1',port:22,server_type:'openwrt',services:[],warnings:[],updates:0,checked:now,monitoring_enabled:true,poll_seconds:60,connection_status:'up',thresholds,metrics:null};
let saved=null,queued=0;
const server=http.createServer((req,res)=>{try{const pathname=new URL(req.url,'http://localhost').pathname;const file=pathname==='/preview'?path.join(process.cwd(),'test-results/daily-report-preview.html'):path.join(process.cwd(),'harbour/static',pathname==='/'?'index.html':pathname.replace('/static/',''));res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html');res.end(fs.readFileSync(file));}catch{res.writeHead(404).end();}});
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({headless:true,executablePath:process.env.HARBOUR_TEST_BROWSER||(fs.existsSync(chromium.executablePath())?chromium.executablePath():'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')});
try{const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.route('**/api/**',async route=>{const url=new URL(route.request().url()),method=route.request().method();let data;
if(url.pathname==='/api/config')data={demo:false,version:'test'};
else if(url.pathname==='/api/me')data={id:'admin',name:'admin',role:'admin',csrf:'test'};
else if(url.pathname==='/api/dashboard')data={servers:[host],thresholds,jobs:[]};
else if(url.pathname==='/api/session/activity')data={ok:true};
else if(url.pathname.endsWith('/network/reports/policy')&&method==='PUT'){saved=route.request().postDataJSON();listing.policy=saved;data={ok:true};}
else if(url.pathname.endsWith('/network/reports/run')){queued++;data={ok:true};}
else if(url.pathname.endsWith('/network/reports'))data=listing;
else if(url.pathname.endsWith('/network/samples'))data={start:now-120,end:now,samples:[],truncated:false};
else if(url.pathname.endsWith('/network'))data={settings:{enabled:true,interval:1,client_mac:'',targets:['192.0.2.1'],router_probes:true},status:{},events:[],archives:{files:[]},call_tests:{tests:[],schedules:[]}};
else throw Error('Unexpected API '+method+' '+url.pathname);
await route.fulfill({json:data});});
await page.goto('http://127.0.0.1:'+server.address().port);await page.getByRole('button',{name:'Reports',exact:true}).click();await page.locator('#report-range').waitFor();
assert.equal(await page.getByRole('progressbar',{name:'Host filesystem usage'}).getAttribute('value'),'37.5');
assert.match(await page.locator('.report-storage').textContent(),/database space is reusable/);
await page.locator('#report-preset').selectOption('week');assert.equal(await page.locator('#report-range [name=start]').inputValue(),'2026-10-12');assert.equal(await page.locator('#report-range [name=end]').inputValue(),'2026-10-18');
await page.evaluate(()=>loadReports(current(),true));assert.equal(await page.locator('#report-range [name=start]').inputValue(),'2026-10-12');
await page.locator('#report-preset').selectOption('custom');await page.locator('#report-range [name=start]').fill('2026-10-01');await page.locator('#report-range [name=end]').fill('2026-10-13');
await page.evaluate(()=>loadReports(current(),true));assert.equal(await page.locator('#report-range [name=start]').inputValue(),'2026-10-01');assert.equal(await page.locator('#report-range [name=end]').inputValue(),'2026-10-13');
await page.getByRole('button',{name:'Process now',exact:true}).click();await page.waitForFunction(()=>!reportView(current()).busy);assert.equal(queued,1);
await page.getByRole('button',{name:'Processing & retention',exact:true}).click();await page.locator('#report-policy [name=process_at]').fill('04:15');await page.getByRole('button',{name:'Save processing settings',exact:true}).click();await page.waitForSelector('#report-policy',{state:'detached'});assert.equal(saved.process_at,'04:15');assert.equal(saved.timezone,'Europe/London');
await page.screenshot({path:'test-results/reports-desktop.png'});
await page.setViewportSize({width:390,height:844});await page.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth);await page.screenshot({path:'test-results/reports-mobile.png'});
await page.goto('http://127.0.0.1:'+server.address().port+'/preview');await page.setViewportSize({width:1200,height:1000});await page.screenshot({path:'test-results/daily-report-desktop.png',fullPage:true});
assert.match(await page.locator('main').textContent(),/Final daily statistics/);assert.match(await page.locator('main').textContent(),/Supporting evidence/);assert.match(await page.locator('main').textContent(),/synthetic preview/);
await page.emulateMedia({media:'print'});assert.equal(await page.locator('.printbar').isVisible(),false);assert.ok(await page.locator('.evidence').first().isVisible());await page.screenshot({path:'test-results/daily-report-print.png',fullPage:true});
await page.emulateMedia({media:'screen'});await page.setViewportSize({width:390,height:844});await page.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth);
assert.deepEqual(errors,[]);console.log('Reports UI passed: schedule, queue, weekly/custom dates, refresh preservation, mobile and print CSS.');
}finally{await browser.close();server.close();}})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
