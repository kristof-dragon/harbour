const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {chromium}=require('playwright');
const fixture=`<!doctype html><html><head><meta charset="UTF-8"><link rel="stylesheet" href="/static/style.css"><script>const e=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const current=()=>({id:'host'});const icon=()=>'<svg></svg>';</script><script src="/static/dom.js"></script><script src="/static/tables.js"></script></head><body><main id="root"></main></body></html>`;
const server=http.createServer((req,res)=>{if(req.url==='/'){res.end(fixture);return;}const file=path.join(process.cwd(),'harbour',req.url);if(!fs.existsSync(file)){res.writeHead(404).end();return;}res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':'text/css');res.end(fs.readFileSync(file));});
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({headless:true,executablePath:process.env.HARBOUR_TEST_BROWSER||(fs.existsSync(chromium.executablePath())?chromium.executablePath():'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')});try{
const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto('http://127.0.0.1:'+server.address().port);
await page.evaluate(()=>{
window.records=Array.from({length:60},(_,i)=>({id:i,value:[2,10,100,-3][i%4],date:1704067200+i*86400,name:i===59?'needle':'row '+i}));
window.paint=()=>updateHTML(document.querySelector('#root'),`<div>${infoMenu('example','Table details','<p>Explanation</p>')}</div><table data-table="readings"><thead><tr><th>Name</th><th>Value</th><th data-type="date">Time</th></tr></thead><tbody>${records.map(r=>`<tr data-key="row:${r.id}"><td>${e(r.name)}</td><td data-value="${r.value}">${r.value} ms</td><td data-value="${r.date}">${new Date(r.date*1000).toLocaleString()}</td></tr>`).join('')}</tbody></table><table class="volume-settings"><thead><tr><th>Volume</th></tr></thead><tbody><tr><td><input value="untouched"></td></tr></tbody></table>`);paint();
});
assert.equal(await page.locator('[data-table=readings] tbody tr').count(),25);
assert.equal(await page.locator('.volume-settings [data-table-filter]').count(),0);
await page.getByRole('button',{name:'Next',exact:true}).click();assert.match(await page.locator('.data-table-controls').textContent(),/26–50 of 60/);
await page.getByRole('button',{name:'Value',exact:true}).click();assert.equal(await page.locator('[data-table=readings] tbody tr').first().locator('td').nth(1).textContent(),'-3 ms');
await page.getByRole('button',{name:'Value',exact:true}).click();assert.equal(await page.locator('[data-table=readings] tbody tr').first().locator('td').nth(1).textContent(),'100 ms');
await page.getByRole('searchbox',{name:'Filter Name'}).fill('needle');await page.waitForFunction(()=>document.querySelectorAll('[data-table=readings] tbody tr').length===1);
assert.match(await page.locator('[data-table=readings] tbody').textContent(),/needle/);
await page.evaluate(()=>paint());assert.equal(await page.getByRole('searchbox',{name:'Filter Name'}).inputValue(),'needle');assert.equal(await page.locator('[data-table=readings] tbody tr').count(),1);
await page.getByRole('button',{name:'Clear filters'}).click();await page.getByRole('searchbox',{name:'Filter Value'}).fill('>=10');await page.waitForFunction(()=>document.querySelector('.data-table-controls').textContent.includes('of 30'));
await page.getByRole('button',{name:'Clear filters'}).click();await page.getByRole('button',{name:'Time',exact:true}).click();
assert.equal(await page.locator('[data-table=readings] tbody tr').first().getAttribute('data-key'),'row:0');await page.getByRole('button',{name:'Time',exact:true}).click();assert.equal(await page.locator('[data-table=readings] tbody tr').first().getAttribute('data-key'),'row:59');
await page.getByLabel('Rows per page').selectOption('50');assert.equal(await page.locator('[data-table=readings] tbody tr').count(),50);
await page.getByLabel('Page number').fill('2');await page.getByLabel('Page number').press('Tab');assert.equal(await page.locator('[data-table=readings] tbody tr').count(),10);
for(const theme of ['light','dark'])for(const width of [1440,390]){await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);await page.setViewportSize({width,height:900});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);}
await page.getByLabel('Table details',{exact:true}).click();await page.waitForFunction(()=>document.querySelector('.info-menu-content').style.left);const left=await page.locator('.info-menu-content').evaluate(n=>n.style.left);await page.evaluate(()=>paint());assert.equal(await page.locator('.info-menu').evaluate(n=>n.open),true);assert.equal(await page.locator('.info-menu-content').evaluate(n=>n.style.left),left);
assert.deepEqual(errors,[]);console.log('Shared table checks passed: whole-dataset filters, numeric/date ordering, pagination, refresh persistence and excluded forms.');
}finally{await browser.close();server.close();}})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
