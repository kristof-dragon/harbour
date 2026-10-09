'use strict';
let collectorTransfer=null;
function collectorSetupControls(s,kind){
  return `<section class="collector-setup" data-server="${e(s.id)}" data-kind="${kind}" aria-label="Push collector to host">
    <p>Send the bundle to <b>${e(s.username)}</b>’s home directory on <b>${e(s.name)}</b>. Each push creates a new file; extraction creates its own folder.</p>
    <div class="collector-actions">${[['push','Push to host'],['extract','Push & extract'],['install','Push & install']].map(([action,label])=>button('collector-push',label,'',action==='install'?'primary':'',`data-mode="${action}" ${state.demo?'disabled':''}`)).join('')}</div>
    <p class="hint">Push and extraction do not need sudo. Installation adds a boot service and asks for the sudo password only if required. Progress uses a separate SSH connection, so monitoring continues.</p>
    ${state.demo?'<p class="hint">Live pushes are disabled in demo mode.</p>':''}
    <div class="collector-progress" hidden><p class="collector-status" role="status"></p><pre class="collector-output" tabindex="0" aria-label="Collector setup output"></pre><div class="collector-password"></div>${button('collector-cancel','Stop setup','','ghost small')}<p class="hint">Closing this dialog stops the setup connection. Uploaded files and completed installation steps remain on the host.</p></div>
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

async function pushCollector(root,mode){
  if(collectorTransfer)return;
  const active={root,controller:new AbortController(),id:null,prompt:null};collectorTransfer=active;
  const status=root.querySelector('.collector-status'),output=root.querySelector('.collector-output'),password=root.querySelector('.collector-password');
  root.querySelector('.collector-progress').hidden=false;output.textContent='';password.replaceChildren();
  root.querySelectorAll('[data-action=collector-push]').forEach(el=>el.disabled=true);
  root.querySelector('[data-action=collector-cancel]').hidden=false;
  status.classList.remove('form-error');status.textContent='Connecting…';let finished=false;
  const append=text=>{output.textContent=(output.textContent+text).slice(-150000);output.scrollTop=output.scrollHeight;};
  try{
    const response=await fetch(`/api/servers/${encodeURIComponent(root.dataset.server)}/collector/${root.dataset.kind}`,{
      method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':state.user.csrf},
      body:JSON.stringify({action:mode}),signal:active.controller.signal});
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
        if(event.kind==='started')active.id=event.id;
        if(event.kind==='output')append(event.text);
        if(event.kind==='progress')status.textContent=`Uploading… ${Math.round(event.received/event.total*100)}%`;
        if(event.kind==='password'){
          active.prompt=event.prompt;status.textContent='Waiting for sudo password';
          password.innerHTML=`<form class="collector-sudo-form" autocomplete="off"><p>${e(event.message)}</p><label>Sudo password<input type="password" name="sudo_password" autocomplete="off" maxlength="1024" required></label><p class="hint">Sent only to this host’s sudo prompt. It is never saved in Harbour or included in setup output.</p><div class="form-error" role="alert"></div><button type="submit" class="primary">Continue installation</button></form>`;
          password.querySelector('input').focus();
        }
        if(event.kind==='result'){
          finished=true;status.textContent=event.message;append('\n'+event.message+'\n');password.replaceChildren();
          status.classList.toggle('form-error',!event.ok);
        }
      }
      if(done)break;
    }
    if(!finished)throw Error('Setup connection ended before the result arrived. Check the host before retrying.');
  }catch(error){status.textContent=error.name==='AbortError'?'Setup stopped. Completed steps remain on the host.':error.message;}
  finally{
    if(!finished)active.controller.abort();
    if(collectorTransfer===active)collectorTransfer=null;
    password.replaceChildren();
    root.querySelectorAll('[data-action=collector-push]').forEach(el=>el.disabled=state.demo);
    root.querySelector('[data-action=collector-cancel]').hidden=true;
  }
}
document.addEventListener('click',event=>{
  const button=event.target.closest('[data-action]');
  if(button?.dataset.action==='collector-push')pushCollector(button.closest('.collector-setup'),button.dataset.mode);
  if(button?.dataset.action==='collector-cancel')endCollectorTransfer();
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
