'use strict';
const resourceFields=[
 ['web_memory_mib','Harbour memory limit (MiB)',128,65536,'Hard limit for the web service and its workers, including application memory, file cache and kernel memory.'],
 ['recorder_memory_mib','Recorder memory limit (MiB)',192,65536,'Hard limit for the network recorder and its child processes, including file cache.'],
 ['processing_mib','Evidence processing allowance (MiB)',2,128,'Maximum encoded evidence per operation. Analysis needs additional memory: each service limit must be at least eight times this allowance. Originals remain stored when a request is too large.'],
 ['buffer_total_mib','Shared recorder buffer (MiB)',1,512,'Payload budget shared by all network recorders. Included within recorder memory, not added to it. At most one quarter of the recorder memory limit.'],
 ['write_interval_seconds','Recording write interval (seconds)',1,60,'Normal interval between database batches. Backlogs and shutdown drain sooner. Other database activity and filesystem writes have their own timing. A sudden failure can lose readings still waiting in memory.']
];
let resourcePoll=null;
const resourceProfileName=value=>value==='sbc-1g'?'1 GB board · low write':'Standard';
function resourceSettingsContent(data){
 if(!data.available)return `<p class="form-error" role="alert">${e(data.reason)}</p>${button('resource-settings','Try again','refresh','small')}`;
 const cfg=data.saved,busy=data.operation?.status==='applying';
 return `<div class="info-box"><b>Currently running</b><p id="resource-active">${data.active?`${e(resourceProfileName(data.active.profile))} · Harbour ${data.active.web_memory_mib} MiB · recorder ${data.active.recorder_memory_mib} MiB · write batches ${data.active.write_interval_seconds} s<br>Evidence allowance ${data.active.processing_mib} MiB · shared buffer ${data.active.buffer_total_mib} MiB`:'Running values could not be confirmed.'}</p>${data.applied?'':'<p class="form-error">Saved and running settings have not been confirmed to match.</p>'}<small>Host memory: ${e(data.host_memory_mib??'Unavailable')} MiB</small></div>
 <form id="resource-settings-form"><fieldset ${busy?'disabled':''}><label>Resource profile<select name="profile"><option value="standard" ${cfg.profile==='standard'?'selected':''}>Standard</option><option value="sbc-1g" ${cfg.profile==='sbc-1g'?'selected':''}>1 GB board · low write</option></select></label><p id="resource-profile-note" class="hint"></p>
 <div class="form-grid">${resourceFields.map(([key,label,min,max,description])=>`<label>${fieldCaption(label,description)}<input aria-label="${e(label)}" name="${key}" type="number" min="${min}" max="${max}" step="1" required value="${cfg[key]}"></label>`).join('')}</div>
 <p id="resource-total" class="hint"></p><p class="hint">Applying restarts Harbour and its recorder. Recording pauses briefly and running tests may be interrupted. This page reconnects automatically.</p>
 <div class="form-error" role="alert"></div><div class="form-actions">${button('resource-defaults','Restore profile defaults','','ghost')}<button type="submit" class="primary">Apply &amp; restart</button></div></fieldset></form>
 <p id="resource-operation" role="status" aria-live="polite">${e(data.operation?.message||'')}</p>${button('footprint','View footprint','activity','small ghost')}`;
}
function resourceHints(root){
 const form=$('form',root);if(!form)return;
 const low=form.elements.profile.value==='sbc-1g';
 $('#resource-profile-note',root).textContent=low?'Uses slower recording to reduce writes: probes at least 2 seconds apart, telemetry at least 10 seconds apart, and no extra router-origin probes. You can adjust the limits below.':'Keeps the recording cadence chosen for each router. You can adjust the limits below.';
 const total=Number(form.elements.web_memory_mib.value)+Number(form.elements.recorder_memory_mib.value);
 $('#resource-total',root).textContent=`Combined memory limits: ${total} MiB${low?' · maximum 704 MiB for this profile':''}. Buffers and processing use memory within these limits.`;
}
function resourceDefaults(root){
 const form=$('form',root),preset=root.resourceData.profiles[form.elements.profile.value];
 for(const [key,value] of Object.entries(preset))form.elements[key].value=value;
 resourceHints(root);
}
function renderResourceSettings(root,data){
 if(!root.isConnected)return;
 root.resourceData=data;root.innerHTML=resourceSettingsContent(data);resourceHints(root);
 const form=$('form',root);if(form)markFormSaved(form);
}
async function showResourceSettings(){
 modal('Resource settings','<div id="resource-settings"><p>Loading current limits…</p></div>',true);
 const root=$('#resource-settings');
 try{
  const data=await api('/resource-settings');renderResourceSettings(root,data);
  if(root.isConnected&&data.operation?.status==='applying')watchResourceChange(root,data.operation.request_id);
 }catch(error){if(root.isConnected)root.innerHTML=`<p class="form-error">${e(error.message)}</p>${button('resource-settings','Retry','refresh','small')}`;}
}
function watchResourceChange(root,identity){
 clearTimeout(resourcePoll);const started=Date.now();
 async function poll(){
  if(!root.isConnected)return;
  try{
   const data=await api('/resource-settings');
   if(!root.isConnected)return;
   if(data.available&&data.operation?.request_id===identity&&data.operation.status!=='applying'){
    renderResourceSettings(root,data);return;
   }
   $('#resource-operation',root).textContent=data.available&&data.operation?.request_id===identity?data.operation.message:'Waiting for Harbour to reconnect and confirm the change…';
  }catch(error){if(root.isConnected)$('#resource-operation',root).textContent='Waiting for Harbour to reconnect and confirm the change…';}
  if(!root.isConnected)return;
  if(Date.now()-started>600000){$('#resource-operation',root).textContent='The result is still unconfirmed. Reopen Resource settings to check the running limits.';return;}
  resourcePoll=setTimeout(poll,3000);
 }
 resourcePoll=setTimeout(poll,2000);
}
document.addEventListener('harbour:overlay-close',()=>{clearTimeout(resourcePoll);resourcePoll=null;});
document.addEventListener('click',event=>{
 const action=event.target.closest('[data-action]')?.dataset.action;
 if(action==='resource-settings')showResourceSettings();
 if(action==='resource-defaults')resourceDefaults($('#resource-settings'));
});
document.addEventListener('change',event=>{const root=event.target.closest('#resource-settings');if(root&&event.target.name==='profile')resourceDefaults(root);});
document.addEventListener('input',event=>{const root=event.target.closest('#resource-settings');if(root)resourceHints(root);});
document.addEventListener('submit',async event=>{
 const form=event.target;if(form.id!=='resource-settings-form')return;event.preventDefault();
 if(form.dataset.busy)return;
 const root=form.closest('#resource-settings'),settings={profile:form.elements.profile.value};
 for(const [key] of resourceFields)settings[key]=Number(form.elements[key].value);
 const identity=Array.from(crypto.getRandomValues(new Uint8Array(18)),v=>v.toString(16).padStart(2,'0')).join('');
 form.dataset.busy='true';$('fieldset',form).disabled=true;$('.form-error',form).textContent='';
 try{
  await api('/resource-settings','POST',{settings,revision:root.resourceData.revision,request_id:identity});
  markFormSaved(form);$('#resource-operation',root).textContent='Saving settings and restarting Harbour…';watchResourceChange(root,identity);
 }catch(error){
  // A disconnected POST may already have been accepted. Check its identifier
  // before enabling another request; retries never silently repeat a restart.
  try{const data=await api('/resource-settings');if(data.operation?.request_id===identity){markFormSaved(form);renderResourceSettings(root,data);if(data.operation.status==='applying')watchResourceChange(root,identity);return;}if(!data.available)throw new Error('Reconnecting');}catch(ignored){if(root.isConnected){markFormSaved(form);$('#resource-operation',root).textContent='Waiting for Harbour to reconnect and confirm the change…';watchResourceChange(root,identity);}return;}
  if(root.isConnected){$('.form-error',form).textContent=error.message+' Reopen this panel if the running state has changed.';$('fieldset',form).disabled=false;delete form.dataset.busy;}
 }
});
