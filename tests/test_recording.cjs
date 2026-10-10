// Browser verification with synthetic hosts: no real SSH or service changes.
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {chromium}=require('playwright');
const now=Date.now()/1000,thresholds={cpu:85,memory:85,disk:85,disk_free_gb:10,temperature:80};
const hosts=['local','remote','fallback'].map((mode,i)=>({id:mode,name:['SBC recorder','Plain probe','Recorder unavailable'][i],host:'example.invalid',port:22,username:'harbour',server_type:i?'plain':'docker',
  services:[],warnings:[],updates:0,checked:now,monitoring_enabled:true,poll_seconds:60,record_seconds:60,disk_seconds:300,inventory_seconds:300,
  connection_status:'up',latency_ms:2,thresholds,recording:{mode,state:mode==='local'?'recording':'not_installed'},
  metrics:{cpu:15,cpu_sample_seconds:60,memory:{percent:20,total:2e9,used:4e8},cores:4,uptime:7200,boot_at:now-7200,measured_at:now-30,disk_measured_at:now-150,
    disks:[{mount:'/',total:1e11,used:3e10,free:7e10,percent:30,present:true,monitor:true,warn:true,card:true}],os:'Linux',kernel:'6.1',docker:'29',temperature:{sensors:[]},hardware:[]}}));
let saved;
const server=http.createServer((req,res)=>{try{const file=path.join(process.cwd(),'harbour/static',req.url==='/'?'index.html':req.url.replace('/static/',''));res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html');res.end(fs.readFileSync(file));}catch{res.writeHead(404).end();}});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({headless:true,executablePath:process.env.HARBOUR_TEST_BROWSER||(fs.existsSync(chromium.executablePath())?chromium.executablePath():'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/**',async route=>{
   const url=new URL(route.request().url());let data;
   if(url.pathname==='/api/config')data={demo:false,version:'test'};
   else if(url.pathname==='/api/me')data={id:'admin',name:'admin',role:'admin',csrf:'test'};
   else if(url.pathname==='/api/dashboard')data={servers:hosts,thresholds,jobs:[]};
   else if(url.pathname==='/api/session/activity')data={ok:true};
   else if(url.pathname.endsWith('/history'))data={points:[],hours:6,from:now-21600,to:now,resolution_seconds:60,poll_seconds:60};
   else if(url.pathname.endsWith('/boots'))data={retention_days:90,boots:[{initial:0,boot_at:now-7200,first_sample:now-7150,detected_at:now-7100},{initial:1,boot_at:now-86400,first_sample:now-86000,detected_at:now-85900}]};
   else if(url.pathname.endsWith('/settings')){saved=route.request().postDataJSON();Object.assign(hosts[0],saved,{thresholds});data={ok:true,name:saved.name};}
   else throw Error('Unexpected API '+url.pathname);
   await route.fulfill({json:data});
  });
  await page.goto('http://127.0.0.1:'+server.address().port);
  await page.getByRole('button',{name:'Boot history',exact:true}).click();
  await page.getByText('Reboot observed',{exact:true}).waitFor();
  assert.equal(await page.getByText('First observed boot',{exact:true}).count(),1);
  assert.match(await page.getByRole('dialog').textContent(),/0d 2h 0m/);
  await page.getByRole('button',{name:'Close dialog',exact:true}).click();
  await page.locator('[data-action=server-settings]').click();
  await page.getByLabel('Recorder sample interval (seconds)').fill('30');
  await page.getByLabel('Disk capacity interval (seconds)').fill('600');
  await page.getByLabel('Docker inventory interval (seconds)').fill('900');
  await page.getByRole('button',{name:'Save',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('[name=record_seconds]')?.value==='30');
  for(let i=0;i<100&&!saved;i++)await page.waitForTimeout(20);
  assert.equal(saved.record_seconds,30);assert.equal(saved.disk_seconds,600);assert.equal(saved.inventory_seconds,900);
  await page.getByRole('button',{name:'Install recorder',exact:true}).click();
  await page.getByRole('heading',{name:'Install resource recorder',exact:true}).waitFor();
  assert.match(await page.getByRole('dialog').textContent(),/no enable switch/i);
  assert.equal(await page.locator('a[href="/api/resource-recorder/download"]').count(),1);
  assert.match(await page.getByRole('dialog').textContent(),/--reader 'harbour'/);
  await page.getByRole('button',{name:'Close dialog',exact:true}).click();
  await page.locator('[data-action=server-settings]').click();
  await page.getByRole('button',{name:'Remove recorder',exact:true}).click();
  assert.match(await page.getByRole('dialog').textContent(),/--uninstall/);
  assert.match(await page.getByRole('dialog').textContent(),/retained by default/);
  assert.match(await page.getByRole('dialog').textContent(),/--purge-data/);
  fs.mkdirSync('test-results',{recursive:true});
  await page.screenshot({path:'test-results/recorder-remove-desktop.png'});
  await page.setViewportSize({width:390,height:844});
  await page.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:'test-results/recorder-remove-mobile.png'});
  await page.getByRole('button',{name:'Close dialog',exact:true}).click();
  await page.setViewportSize({width:1440,height:1000});
  for(const id of ['remote','fallback']){
   await page.locator(`[data-key="server:${id}"]`).click();
   assert.match(await page.locator('.overview-connection').textContent(),id==='remote'?/Remote probes/:/recorder unavailable/);
  }
  await page.locator('[data-tab=storage]').click();
  assert.match(await page.locator('#server-view').textContent(),/Capacity measured/);
  await page.locator('[data-action=server-settings]').click();
  await page.getByRole('button',{name:'Remove server',exact:true}).click();
  assert.match(await page.getByRole('dialog').textContent(),/does not.*uninstall collectors/);
  await page.getByRole('button',{name:'Recorder removal instructions',exact:true}).click();
  await page.getByRole('heading',{name:'Remove resource recorder',exact:true}).waitFor();
  assert.equal(errors.length,0,errors.join('\n'));
  console.log('PASS: recorder modes, separate intervals, installation/removal, boot history, off-boarding, cached disk age and mobile layout');
 }finally{await browser.close();server.close();}
})().catch(error=>{console.error(error);server.close();process.exit(1);});
