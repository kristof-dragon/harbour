// Synthetic counters and documentation addresses. No live infrastructure is changed.
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {chromium}=require('playwright');
const now=Date.now()/1000,thresholds={cpu:85,memory:85,disk:85,disk_free_gb:10,temperature:80};
const host={id:'host',name:'Example host',host:'192.0.2.1',port:22,server_type:'plain',services:[],warnings:[],updates:0,checked:now,monitoring_enabled:true,poll_seconds:60,connection_status:'up',thresholds,metrics:{cpu:20,cores:4,memory:{total:8e9,used:3e9,percent:37.5},disks:[],temperature:{sensors:[]},hardware:[],os:'Linux',kernel:'6.1',uptime:1000}};
const component={scope:'cgroup',identity:'service',memory_bytes:100e6,cache_bytes:20e6,read_per_second:1024,write_per_second:2048,recent_read_per_second:1024,recent_write_per_second:2048,recent_window_seconds:30,app_bytes:70e6,kernel_bytes:10e6,window_seconds:3600,detail:'Service and children',fresh:true};
let data={measured_at:now,total:{memory_bytes:150e6,cache_bytes:30e6,read_per_second:1536,write_per_second:3072,complete:true},components:[{...component,name:'Harbour web and workers'},{...component,name:'Network recorder',memory_bytes:50e6,write_per_second:1024,recent_write_per_second:1024}],servers:Array.from({length:30},(_,i)=>({id:'server'+i,name:'Server '+i,server_type:i===0?'openwrt':'plain',incoming:{per_second:10+i,window_seconds:3600},network:i===0?{write_per_second:100,window_seconds:3600}:null,collectors:[{...component,name:'Resource recorder'},{scope:'unavailable',name:'Login collector',fresh:false,detail:'Not installed'}]})),storage:{bytes:1024e6,net_change_bytes:1024,since:now-3600},history:{days:[{day:Math.floor(now/86400)*86400,read_bytes:1024,write_bytes:2048,read_seconds:3600,write_seconds:3600}],retention_days:90},method:'Daily measurements are checkpointed.',limits:['Logical server volume is not physical disk I/O.']};
let requests=0,delay=false,release;
const server=http.createServer((req,res)=>{try{const file=path.join(process.cwd(),'harbour/static',req.url==='/'?'index.html':req.url.replace('/static/',''));res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html');res.end(fs.readFileSync(file));}catch{res.writeHead(404).end();}});
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({headless:true,executablePath:process.env.HARBOUR_TEST_BROWSER||(fs.existsSync(chromium.executablePath())?chromium.executablePath():'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')});try{
const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.route('**/api/**',async route=>{const url=new URL(route.request().url());let value;
 if(url.pathname==='/api/config')value={demo:false,version:'test'};
 else if(url.pathname==='/api/me')value={id:'admin',name:'admin',role:'admin',csrf:'test'};
 else if(url.pathname==='/api/dashboard')value={servers:[host],thresholds,jobs:[]};
 else if(url.pathname==='/api/session/activity')value={ok:true};
 else if(url.pathname.endsWith('/history'))value={points:[],hours:6,from:now-21600,to:now,resolution_seconds:60,poll_seconds:60};
 else if(url.pathname==='/api/footprint'){requests++;value=structuredClone(data);if(delay)await new Promise(r=>release=r);}
 else throw Error('Unexpected API '+url.pathname);
 await route.fulfill({json:value});
});
await page.goto('http://127.0.0.1:'+server.address().port);
await page.locator('#sidebar-menu>summary').click();await page.getByRole('button',{name:'Harbour footprint',exact:true}).click();
await page.locator('[data-table=footprint-components]').waitFor();
assert.equal(await page.locator('.modal-backdrop').count(),1);
assert.match(await page.locator('.footprint-metrics').textContent(),/Deployment memory.*Application memory.*File cache.*Recorder buffers.*Disk reads.*Disk writes.*Recorded writes today/);
assert.match(await page.locator('#footprint').textContent(),/Logical data volume/);
assert.match(await page.locator('#footprint').textContent(),/Saved across restarts/);
assert.doesNotMatch(await page.locator('#footprint').textContent(),/Projected writes/);
assert.equal(await page.locator('[data-table=footprint-servers] tbody tr').count(),25);
const servers=page.locator('[data-table-key$=footprint-servers]');
await servers.getByRole('button',{name:'Next',exact:true}).click();assert.equal(await servers.locator('tbody tr').count(),5);
await servers.getByRole('searchbox',{name:'Filter Monitored server'}).fill('Server 29');
await page.waitForFunction(()=>document.querySelector('[data-table=footprint-servers] tbody').textContent.includes('Server 29')&&document.querySelectorAll('[data-table=footprint-servers] tbody tr').length===1);
await page.evaluate(()=>loadFootprint(document.querySelector('#footprint')));
assert.equal(await servers.getByRole('searchbox',{name:'Filter Monitored server'}).inputValue(),'Server 29');
assert.equal(await servers.locator('tbody tr').count(),1);
const components=page.locator('[data-table-key$=footprint-components]');
await components.getByRole('button',{name:'Writes',exact:true}).click();
assert.match(await components.locator('tbody tr').first().textContent(),/Network recorder/);
assert.match(await page.locator('[data-table=footprint-collectors] tbody').textContent(),/Not available \/ stale/);
await page.evaluate(()=>document.querySelector('.modal').scrollTop=0);
fs.mkdirSync('test-results',{recursive:true});await page.screenshot({path:'test-results/footprint-desktop.png'});
for(const theme of ['light','dark']){await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);await page.setViewportSize({width:390,height:844});await page.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth);assert.equal(await page.locator('.modal').evaluate(n=>n.getBoundingClientRect().right<=innerWidth),true);}
await page.screenshot({path:'test-results/footprint-mobile.png'});
// A slower response must not replace a newer refresh.
delay=true;await page.evaluate(()=>{void loadFootprint(document.querySelector('#footprint'));});while(!release)await new Promise(r=>setTimeout(r,10));
delay=false;data={...data,stale:true,total:{...data.total,memory_bytes:null,read_per_second:null,write_per_second:null,complete:false}};
await page.evaluate(()=>loadFootprint(document.querySelector('#footprint')));release();await page.waitForTimeout(100);
assert.match(await page.locator('#footprint').textContent(),/Measurements are stale/);
assert.match(await page.locator('.footprint-metrics').textContent(),/Unavailable/);
await page.getByRole('button',{name:'Close dialog'}).click();assert.equal(await page.evaluate(()=>footprintTimer),null);
assert.deepEqual(errors,[]);console.log('Footprint UI passed: separated scopes, missing data, pagination/filter/sort, refresh races and mobile layout.');
}finally{await browser.close();server.close();}})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
