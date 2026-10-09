// Real chunked HTTP streams, synthetic hosts and sudo; no host changes.
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {chromium}=require('playwright');
const now=Date.now()/1000,thresholds={cpu:85,memory:85,disk:85,disk_free_gb:10,temperature:80};
const host={id:'plain',name:'Test host',host:'example.invalid',port:22,username:'harbour',server_type:'plain',services:[],warnings:[],updates:0,checked:now,monitoring_enabled:true,poll_seconds:60,record_seconds:30,connection_status:'up',thresholds,recording:{mode:'remote'},metrics:null};
let active=null,requests=[],passwords=[];
const send=event=>active?.res.write(JSON.stringify(event)+'\n');
const finish=ok=>{send({kind:'result',ok,message:ok?'Setup completed.':'Installer failed.'});active.res.end();active=null;};
const server=http.createServer(async(req,res)=>{
 try{
  const url=new URL(req.url,'http://localhost');let body='';for await(const chunk of req)body+=chunk;
  const data=body?JSON.parse(body):null;
  const json=value=>{res.setHeader('Content-Type','application/json');res.end(JSON.stringify(value));};
  if(url.pathname==='/api/config')return json({demo:false,version:'test'});
  if(url.pathname==='/api/me')return json({id:'admin',name:'admin',role:'admin',csrf:'test'});
  if(url.pathname==='/api/dashboard')return json({servers:[host],thresholds,jobs:[]});
  if(url.pathname==='/api/session/activity')return json({ok:true});
  if(req.method==='GET'&&url.pathname.endsWith('/logins'))return json({events:[],status:{state:'not_installed'},retention_days:90});
  if(url.pathname.includes('/collector/')){
   assert.equal(req.headers['x-csrf-token'],'test');requests.push([url.pathname,data.action]);
   res.setHeader('Content-Type','application/x-ndjson');res.setHeader('X-Accel-Buffering','no');
   active={res,mode:data.action,attempt:0,id:'setup-'+requests.length};
   send({kind:'started',id:active.id});send({kind:'progress',received:100,total:100});
   send({kind:'output',text:'Uploaded: /home/harbour/collector.zip\n<script>window.compromised=true</script>\n'});
   if(data.action==='install')send({kind:'password',prompt:'first',message:'One-time sudo password on Test host.'});
   return;
  }
  if(url.pathname.endsWith('/password')){
   passwords.push(data.password);json({ok:true});
   if(++active.attempt===1){send({kind:'output',text:'Sorry, try again.\n'});send({kind:'password',prompt:'second',message:'Please try the sudo password again.'});}
   else {send({kind:'output',text:'Installing service…\n'});finish(true);}
   return;
  }
  if(url.pathname.endsWith('/cancel')){if(active&&url.pathname.includes('/'+active.id+'/')){active.res.end();active=null;}return json({ok:true});}
  if(url.pathname.startsWith('/api/'))throw Error('Unexpected API '+url.pathname);
  const file=path.join(process.cwd(),'harbour/static',url.pathname==='/'?'index.html':url.pathname.replace('/static/',''));
  res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html');res.end(fs.readFileSync(file));
 }catch(error){res.writeHead(500);res.end(error.message);}
});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({headless:true,executablePath:process.env.HARBOUR_TEST_BROWSER||(fs.existsSync(chromium.executablePath())?chromium.executablePath():'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto('http://127.0.0.1:'+server.address().port);
  await page.locator('[data-action=server-settings]').click();
  await page.getByRole('button',{name:'Install recorder',exact:true}).click();
  for(const name of ['Push to host','Push & extract','Push & install'])assert.equal(await page.getByRole('button',{name,exact:true}).count(),1);
  await page.getByRole('button',{name:'Push to host',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.collector-output')?.textContent.includes('Uploaded:'));
  assert.equal(active.mode,'push'); // Output is visible before the stream completes.
  assert.equal(await page.getByRole('button',{name:'Push & install',exact:true}).isDisabled(),true);
  assert.equal(await page.locator('.collector-output script').count(),0);
  assert.equal(await page.evaluate(()=>window.compromised),undefined);
  finish(true);await page.getByText('Setup completed.',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Push & extract',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.collector-output')?.textContent.includes('Uploaded:'));
  assert.equal(active.mode,'extract');finish(true);
  await page.getByText('Setup completed.',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Push & install',exact:true}).click();
  await page.getByLabel('Sudo password',{exact:true}).fill('wrong password');
  await page.getByRole('button',{name:'Continue installation',exact:true}).click();
  await page.getByText('Please try the sudo password again.',{exact:true}).waitFor();
  assert.equal(await page.getByLabel('Sudo password',{exact:true}).inputValue(),'');
  await page.getByLabel('Sudo password',{exact:true}).fill('  correct £  ');
  await page.getByRole('button',{name:'Continue installation',exact:true}).click();
  await page.getByText('Setup completed.',{exact:true}).waitFor();
  assert.deepEqual(passwords,['wrong password','  correct £  ']);
  assert.equal(await page.locator('.collector-sudo-form').count(),0);
  assert.doesNotMatch(await page.locator('.collector-output').textContent(),/correct|wrong password/);
  assert.equal(requests.length,3);
  fs.mkdirSync('test-results',{recursive:true});
  await page.screenshot({path:'test-results/collector-setup-desktop.png'});
  await page.setViewportSize({width:390,height:844});
  await page.getByRole('button',{name:'Push & install',exact:true}).click();
  await page.getByLabel('Sudo password',{exact:true}).fill('not submitted');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:'test-results/collector-setup-mobile.png'});
  await page.getByRole('button',{name:'Stop setup',exact:true}).click();
  await page.getByText('Setup stopped. Completed steps remain on the host.',{exact:true}).waitFor();
  assert.equal(await page.locator('.collector-sudo-form').count(),0);
  assert.equal(passwords.length,2);
  await page.getByRole('button',{name:'Push to host',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.collector-output')?.textContent.includes('Uploaded:'));
  await page.getByRole('button',{name:'Close dialog',exact:true}).click();
  assert.equal(await page.locator('[role=dialog]').count(),0);
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('[data-tab=logins]').click();
  await page.getByRole('button',{name:'Collector setup',exact:true}).click();
  await page.getByRole('button',{name:'Push & extract',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.collector-output')?.textContent.includes('Uploaded:'));
  assert.equal(requests.at(-1)[0],'/api/servers/plain/collector/logins');
  finish(false);await page.getByText('Installer failed.',{exact:true}).waitFor();
  assert.equal(await page.locator('.collector-status.form-error').count(),1);
  assert.equal(errors.length,0,errors.join('\n'));
  console.log('PASS: live chunks, all three actions, sudo retry/secrecy, cancellation, dialog close, both collectors, failure output and mobile layout');
 }finally{await browser.close();server.close();}
})().catch(error=>{console.error(error);server.close();process.exit(1);});
