'use strict';
let collectorTransfer=null;
function collectorProgress(label='Collector setup output'){
  return `<div class="collector-progress" hidden><p class="collector-status" role="status"></p><div class="collector-actions"><a class="collector-download" hidden>Download output</a>${button('collector-cancel','Stop setup','','ghost small')}</div><pre class="collector-output" tabindex="0" aria-label="${e(label)}"></pre><div class="collector-password"></div><p class="hint collector-retention">Install output is saved on Harbour indefinitely. Closing this dialog stops an active setup; completed steps remain on the host.</p></div>`;
}
function collectorSetupControls(s,kind){
  return `<section class="collector-setup" data-server="${e(s.id)}" data-kind="${kind}" aria-label="Push collector to host">
    <p>Send the bundle to <b>${e(s.username)}</b>’s home directory on <b>${e(s.name)}</b>. Each push creates a new file; extraction creates its own folder.</p>
    <div class="collector-actions">${[['push','Push to host'],['extract','Push & extract'],['install','Push & install']].map(([action,label])=>button('collector-push',label,'',action==='install'?'primary':'',`data-mode="${action}" ${state.demo?'disabled':''}`)).join('')}</div>
    <p class="hint">Push and extraction do not need sudo. Installation adds a boot service and asks for the sudo password only if required. Progress uses a separate SSH connection, so monitoring continues.</p>
    ${state.demo?'<p class="hint">Live pushes are disabled in demo mode.</p>':''}
    ${button('install-artifacts','Saved installs','','small')}${collectorProgress()}
  </section>`;
}
function endCollectorTransfer(){
  const active=collectorTransfer;if(!active)return;
  collectorTransfer=null;
  if(active.id)api('/collector-operations/'+encodeURIComponent(active.id)+'/cancel','POST').catch(()=>{});
  active.controller.abort();
  active.root.querySelector('.collector-password').replaceChildren();
}
document.addEventListener('harbour:overlay-close',endCollectorTransfer);
window.addEventListener('pagehide',()=>collectorTransfer?.controller.abort());

async function pushCollector(root,mode,options={}){
  if(collectorTransfer)return;
  const active={root,controller:new AbortController(),id:null,prompt:null};collectorTransfer=active;
  const status=root.querySelector('.collector-status'),output=root.querySelector('.collector-output'),password=root.querySelector('.collector-password');
  const download=root.querySelector('.collector-download');download.hidden=true;download.removeAttribute('href');
  root.hidden=false;root.querySelector('.collector-progress').hidden=false;output.textContent='';password.replaceChildren();
  root.querySelectorAll('[data-action=collector-push]').forEach(el=>el.disabled=true);
  root.querySelector('[data-action=collector-cancel]').hidden=false;
  status.classList.remove('form-error');status.textContent='Connecting…';let finished=false,result=null;
  root.scrollIntoView({block:'nearest'});
  const append=text=>{
    const atEnd=output.scrollHeight-output.scrollTop-output.clientHeight<40;
    const combined=output.textContent+text;output.textContent=combined.slice(-150000);
    if(combined.length>150000)root.querySelector('.collector-retention').textContent='Showing the latest output. Download output includes the full saved transcript. Install artifacts are retained on Harbour indefinitely.';
    if(atEnd)output.scrollTop=output.scrollHeight;
  };
  try{
    let body=JSON.stringify(options.body||{action:mode});
    if(options.body){delete options.body.password;options.body=null;}
    const pendingResponse=fetch(options.url||`/api/servers/${encodeURIComponent(root.dataset.server)}/collector/${root.dataset.kind}`,{
      method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':state.user.csrf},
      body,signal:active.controller.signal});
    body=null;
    const response=await pendingResponse;
    if(!response.ok){const data=await response.json();throw Error(typeof data.detail==='string'?data.detail:'Could not start collector setup.');}
    const reader=response.body.getReader(),decoder=new TextDecoder();let pending='';
    while(true){
      const {value,done}=await reader.read();
      pending+=decoder.decode(value,{stream:!done});
      if(pending.length>1100000)throw Error('Setup output exceeded its limit.');
      let index;
      while((index=pending.indexOf('\n'))>=0){
        const line=pending.slice(0,index);pending=pending.slice(index+1);if(!line)continue;
        const event=JSON.parse(line);
        if(event.kind==='started'){
          active.id=event.id;
          download.href='/api/install-artifacts/'+encodeURIComponent(event.id)+'/download';download.hidden=false;
        }
        if(event.kind==='output')append(event.text);
        if(event.kind==='progress')status.textContent=`Uploading… ${Math.round(event.received/event.total*100)}%`;
        if(event.kind==='password'){
          if(root.dataset.kind==='call-endpoint')throw Error('Unexpected password prompt: Docker deployment does not use sudo.');
          active.prompt=event.prompt;status.textContent='Waiting for sudo password';
          password.innerHTML=`<form class="collector-sudo-form" autocomplete="off"><p>${e(event.message)}</p><label>Sudo password<input type="password" name="sudo_password" autocomplete="off" maxlength="1024" required></label><p class="hint">Sent only to this host’s sudo prompt. It is never saved in Harbour or included in setup output.</p><div class="form-error" role="alert"></div><button type="submit" class="primary">Continue installation</button></form>`;
          password.querySelector('input').focus();
        }
        if(event.kind==='result'){
          result=event;
          finished=true;status.textContent=event.message;append('\n'+event.message+'\n');password.replaceChildren();
          status.classList.toggle('form-error',!event.ok);
        }
      }
      if(done)break;
    }
    if(!finished)throw Error('Setup connection ended before the result arrived. Check the host before retrying.');
  }catch(error){status.textContent=error.name==='AbortError'?'Setup stopped. Completed steps remain on the host.':error.message;append('\n'+status.textContent+'\n');}
  finally{
    if(!finished)active.controller.abort();
    if(collectorTransfer===active)collectorTransfer=null;
    password.replaceChildren();
    root.querySelectorAll('[data-action=collector-push]').forEach(el=>el.disabled=state.demo);
    root.querySelector('[data-action=collector-cancel]').hidden=true;
  }
  return result;
}
document.addEventListener('click',event=>{
  const button=event.target.closest('[data-action]');
  if(button?.dataset.action==='collector-push')pushCollector(button.closest('.collector-setup'),button.dataset.mode);
  if(button?.dataset.action==='collector-cancel')endCollectorTransfer();
});

async function showInstallArtifacts(before=''){
  const tableId='saved-installs',t=tableState(tableId);if(!t.sort){t.sort='time';t.order='desc';}t.dates=['time'];
  tableRefreshers.set(tableId,()=>showInstallArtifacts());
  const query=new URLSearchParams(tableQuery(tableId)).toString(),data=await api('/install-artifacts/table?'+query);
  if(query!==new URLSearchParams(tableQuery(tableId)).toString())return;
  t.page=data.pagination.page;
  const columns=[{key:'time',label:'Started',type:'date'},{key:'target',label:'Target'},{key:'setup',label:'Setup'},{key:'status',label:'Outcome'},{key:'actions',label:'Install artifact',action:true}];
  modal('Saved installs',`<p class="hint">Install artifacts are kept on this Harbour server indefinitely, including failed and stopped attempts.</p><div data-table-key="saved-installs"><div class="network-table"><table><thead><tr>${columns.map(c=>tableColumn(tableId,c)).join('')}</tr></thead><tbody>${data.artifacts.map(item=>`<tr><td>${e(new Date(item.created*1000).toLocaleString())}</td><td>${e(item.target_name||item.host)}<br><small>${e(item.host)} · ${e(item.actor)}</small></td><td>${e(item.kind)} · ${e(item.action)}</td><td>${e(item.status)}</td><td>${button('install-artifact','View','','small',`data-id="${e(item.id)}"`)} <a href="/api/install-artifacts/${encodeURIComponent(item.id)}/download">Download</a></td></tr>`).join('')||'<tr><td colspan="5">No saved installs yet.</td></tr>'}</tbody></table></div>${tableControls(tableId,data.pagination.total)}</div>`,true);
}
async function showInstallArtifact(id){
  const item=await api('/install-artifacts/'+encodeURIComponent(id));
  modal('Install artifact',`<p><b>${e(item.target_name||item.host)}</b> · ${e(item.status)}</p><p class="hint">${e(item.kind)} · ${e(item.action)} · ${e(item.actor)} · ${e(new Date(item.created*1000).toLocaleString())} · Harbour ${e(item.version)}</p><div class="collector-actions"><a href="/api/install-artifacts/${encodeURIComponent(item.id)}/download">Download output</a>${button('install-artifact','Refresh','','small',`data-id="${e(item.id)}"`)}${button('install-artifacts','Saved installs','','small')}</div><pre class="collector-output install-artifact-output" tabindex="0" aria-label="Saved install output">${e(item.output||'Setup has not produced output yet.')}</pre>`,true);
}
document.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action]');if(!el)return;
  try{
    if(el.dataset.action==='install-artifacts')await showInstallArtifacts(el.dataset.before);
    if(el.dataset.action==='install-artifact')await showInstallArtifact(el.dataset.id);
  }catch(error){toast(error.message,true);}
});
document.addEventListener('submit',async event=>{
  const form=event.target;if(!form.matches('.collector-sudo-form'))return;
  event.preventDefault();const active=collectorTransfer;if(!active?.id||!active.prompt)return;
  const input=form.querySelector('input'),submit=form.querySelector('button');
  let password=input.value;input.value='';submit.disabled=true;
  try{
    await api('/collector-operations/'+encodeURIComponent(active.id)+'/password','POST',{prompt:active.prompt,password});
    if(form.isConnected){form.remove();active.root.querySelector('.collector-status').textContent='Installing…';}
  }catch(error){if(form.isConnected){form.querySelector('.form-error').textContent=error.message;submit.disabled=false;}}
  finally{password='';}
});
