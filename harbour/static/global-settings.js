'use strict';
const globalSettingsTabs=[['warnings','Warnings'],['monitoring','Monitoring & history'],['resources','Resources'],['notifications','Notifications'],['bridge','The Bridge']];

function showGlobalSettings(selected='warnings'){
 let root=$('#global-settings');
 if(!root){
  modal('Global settings',`<div id="global-settings"><div class="global-settings-tabs" role="tablist" aria-label="Global settings sections">${globalSettingsTabs.map(([key,label])=>`<button type="button" role="tab" id="settings-tab-${key}" aria-controls="settings-panel-${key}" aria-selected="false" tabindex="-1" data-action="global-settings-tab" data-tab="${key}">${label}</button>`).join('')}</div><div class="global-settings-body">${globalSettingsTabs.map(([key])=>`<section class="global-settings-panel" id="settings-panel-${key}" role="tabpanel" aria-labelledby="settings-tab-${key}" tabindex="0" data-tab="${key}" hidden></section>`).join('')}</div></div>`,true);
  root=$('#global-settings');root.closest('.modal').classList.add('global-settings-modal');
 }
 selectGlobalSettingsTab(root,selected);
}

function selectGlobalSettingsTab(root,key){
 if(!globalSettingsTabs.some(([name])=>name===key))return;
 hideHelpTooltip();
 root.querySelectorAll('[role=tab]').forEach(tab=>{
  const active=tab.dataset.tab===key;
  tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;
 });
 root.querySelectorAll('[role=tabpanel]').forEach(panel=>{panel.hidden=panel.dataset.tab!==key;});
 const panel=$('#settings-panel-'+key,root);
 if(!panel.dataset.loaded&&!panel.dataset.loading)loadGlobalSettingsPanel(panel);
}

async function loadGlobalSettingsPanel(panel){
 const key=panel.dataset.tab;
 panel.dataset.loading='true';panel.setAttribute('aria-busy','true');panel.innerHTML='<p role="status">Loading settings…</p>';
 try{
  let content;
  if(key==='warnings')content=`<form id="threshold-form" data-server=""><h3>${fieldCaption('Warning thresholds','Default warning thresholds for every server without an override. Disk warnings trigger on either usage or free space. CPU temperature uses package / SoC sensors; other device sensors warn separately.')}</h3>${thresholdFields(state.data.thresholds)}<div class="form-error" role="alert"></div><div class="form-actions"><button class="primary" type="submit">Save thresholds</button></div></form>`;
  else if(key==='monitoring')content=monitoringSettingsContent(await api('/monitoring'));
  else if(key==='notifications')content=notificationSettingsContent(await api('/notifications'))+`<hr class="section-rule"><h3>${fieldCaption('Personal notifications','Acknowledgement silences your warning indicators until recurrence. Removing a warning only hides its list entry. Bringing it back preserves its acknowledgement. Updates are managed separately.')}</h3><div class="notification-actions">${button('restore-dismissals','Bring back warnings','bell','small','data-kind="warnings"')}${button('restore-dismissals','Bring back updates','download','small','data-kind="updates"')}</div>`;
  else if(key==='bridge')content=bridgeSettings();
  else if(key==='resources'){
   panel.innerHTML='<div id="resource-settings"><p>Loading current limits…</p></div>';
   await loadResourceSettings($('#resource-settings',panel));
  }
  if(!panel.isConnected)return;
  if(content!==undefined)panel.innerHTML=content;
  panel.querySelectorAll('form').forEach(markFormSaved);
  panel.dataset.loaded='true';
 }catch(error){
  if(panel.isConnected)panel.innerHTML=`<p class="form-error" role="alert">${e(error.message)}</p>${button('global-settings-retry','Retry','refresh','small',`data-tab="${key}"`)}`;
 }finally{delete panel.dataset.loading;panel.removeAttribute('aria-busy');}
}

document.addEventListener('click',event=>{
 const button=event.target.closest('[data-action]'),root=button?.closest('#global-settings');if(!root)return;
 if(button.dataset.action==='global-settings-tab')selectGlobalSettingsTab(root,button.dataset.tab);
 if(button.dataset.action==='global-settings-retry')loadGlobalSettingsPanel($('#settings-panel-'+button.dataset.tab,root));
});
document.addEventListener('keydown',event=>{
 const tab=event.target.closest('#global-settings [role=tab]');if(!tab)return;
 const tabs=[...tab.parentElement.querySelectorAll('[role=tab]')],index=tabs.indexOf(tab);
 const next=event.key==='ArrowRight'?(index+1)%tabs.length:event.key==='ArrowLeft'?(index+tabs.length-1)%tabs.length:event.key==='Home'?0:event.key==='End'?tabs.length-1:null;
 if(next===null)return;
 event.preventDefault();tabs[next].focus();tabs[next].scrollIntoView({block:'nearest',inline:'nearest'});selectGlobalSettingsTab(tab.closest('#global-settings'),tabs[next].dataset.tab);
});
