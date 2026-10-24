// Synthetic modem UI only; never contacts a real router or modem.
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {chromium}=require('playwright');
const host={id:'router',name:'Example router',host:'192.0.2.2',port:22,username:'root',server_type:'openwrt',services:[],warnings:[],updates:0,checked:Date.now()/1000,monitoring_enabled:true,poll_seconds:60,connection_status:'up',metrics:null};
let role='admin',saved=null,testCalls=0,releaseTest;
let data={settings:{enabled:false,url:'http://192.168.100.1',username:'admin',interval_seconds:60,retention_days:7},password_saved:true,blocked:false,sample:null,stale:false,observer:'example-observer',demo:false,server_monitoring_enabled:true};
const server=http.createServer((req,res)=>{try{const file=path.join(process.cwd(),'harbour/static',req.url==='/'?'index.html':req.url.replace('/static/',''));res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html');res.end(fs.readFileSync(file));}catch{res.writeHead(404).end();}});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({headless:true,executablePath:process.env.HARBOUR_TEST_BROWSER||(fs.existsSync(chromium.executablePath())?chromium.executablePath():'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/**',async route=>{
   const url=new URL(route.request().url()),method=route.request().method();let response={};
   if(url.pathname==='/api/config')response={demo:false,version:'test'};
   else if(url.pathname==='/api/me')response={id:role,name:role,role,csrf:'fixture'};
   else if(url.pathname==='/api/dashboard')response={servers:[host],thresholds:{},jobs:[]};
   else if(url.pathname.endsWith('/modem/history'))response={samples:data.sample?[data.sample]:[],truncated:false};
   else if(url.pathname.endsWith('/modem/test')){
    testCalls++;await new Promise(resolve=>releaseTest=resolve);
    data.sample={at:Date.now()/1000,received_at:Date.now()/1000,state:'partial',configuration_revision:'fixture-revision',collection_ms:20,sections:{downstream:{state:'ok',fields:[{field:'channel/power',value:'123'},{field:'channel/note',value:'<img src=x onerror="window.modemInjected=true">'}]},upstream:{state:'unavailable',error:'No usable XML',fields:[]}}};response=data;
   }else if(url.pathname.endsWith('/modem')){
    if(method==='PUT'){saved=route.request().postDataJSON();const {password,clear_password,...settings}=saved;data={...data,settings};}
    response=data;
   }else if(url.pathname.includes('/network')){await route.fulfill({status:503,json:{detail:'Synthetic network data unavailable'}});return;}
   await route.fulfill({json:response});
  });
  await page.goto('http://127.0.0.1:'+server.address().port);
  await page.locator('[data-tab=modem]').click();
  await page.getByText('Observer: example-observer',{exact:true}).waitFor();
  await page.locator('[data-action=modem-settings]').click();
  const form=page.locator('#modem-settings-form'),password=form.locator('[name=password]');
  assert.equal(await password.inputValue(),'');
  assert.equal(await password.getAttribute('type'),'password');
  await form.locator('[name=interval_seconds]').fill('90');
  await form.locator('[type=submit]').click();
  await form.waitFor({state:'detached'});
  assert.equal(saved.password,'');assert.equal(saved.interval_seconds,90);
  await page.locator('[data-action=modem-test]').click();
  await page.waitForFunction(()=>document.querySelector('[data-action=modem-test]').disabled);
  assert.equal(testCalls,1);releaseTest();
  await page.getByRole('heading',{name:'Partial reading',exact:true}).waitFor();
  await page.locator('.modem-sections details').first().locator('summary').click();
  assert.equal(await page.locator('.modem-sections img').count(),0);
  assert.equal(await page.evaluate(()=>window.modemInjected),undefined);
  assert.match(await page.locator('.modem-view').textContent(),/Collection gap/);
  await page.screenshot({path:'/tmp/harbour-modem-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await page.locator('[data-action=modem-settings]').click();
  assert.equal(await password.inputValue(),'');
  await password.fill('synthetic-sticker-password');
  await page.screenshot({path:'/tmp/harbour-modem-mobile.png',fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  assert.equal(await page.evaluate(()=>Object.values(localStorage).some(v=>v.includes('synthetic-sticker-password'))),false);
  await page.evaluate(()=>closeOverlay(true));
  role='user';await page.reload();await page.locator('[data-tab=modem]').click();
  await page.getByText('Observer: example-observer',{exact:true}).waitFor();
  assert.equal(await page.locator('[data-action=modem-settings],[data-action=modem-test]').count(),0);
  assert.deepEqual(errors,[]);
  console.log('Modem browser checks passed: credentials, save/test, escaped evidence, mobile layout, viewer permissions.');
 }finally{await browser.close();server.close();}
})().catch(error=>{console.error(error);process.exitCode=1;server.close();});
