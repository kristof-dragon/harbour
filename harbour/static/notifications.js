'use strict';

const telegramKinds=[['cpu','CPU'],['memory','Memory'],['disk','Storage'],['temperature','Temp'],['resource','Resources']];
async function notificationSettings(){
  const settings=await api('/notifications');
  const rules=new Map(settings.rules.map(rule=>[rule.server_id+':'+rule.kind,rule]));
  const rows=settings.servers.map((server,index)=>`<tr><th scope="row" title="${e(server.name)}">${e(server.name)}${server.monitoring_enabled?'':'<span class="tag">Paused</span>'}</th>${telegramKinds.map(([kind,label])=>{
    const rule=rules.get(server.id+':'+kind)||{enabled:false,delay_seconds:300,repeat_seconds:3600};
    const prefix=`telegram-${index}-${kind}`,name=`${server.name} ${label}`;
    return `<td><div class="telegram-rule" data-server="${e(server.id)}" data-kind="${kind}"><input type="checkbox" name="${prefix}-enabled" aria-label="${e(name)} notifications" ${rule.enabled?'checked':''}><input type="number" name="${prefix}-delay" aria-label="${e(name)} trigger delay in minutes" title="Warning active for this many minutes before sending" min="0" max="10080" step="1" value="${rule.delay_seconds/60}" ${rule.enabled?'':'disabled'} required><input type="number" name="${prefix}-repeat" aria-label="${e(name)} repeat interval in minutes" title="Repeat every this many minutes; 0 sends once per issue" min="0" max="43200" step="1" value="${rule.repeat_seconds/60}" ${rule.enabled?'':'disabled'} required></div></td>`;
  }).join('')}</tr>`).join('');
  modal('Notifications',`<form id="telegram-form"><div class="telegram-heading"><h3>${icon('bell')} Telegram</h3><label class="compact-checkbox"><input name="telegram_enabled" type="checkbox" ${settings.enabled?'checked':''}>Enable Telegram</label></div><div class="form-grid telegram-credentials"><label>${fieldCaption('Bot token','Create a bot with Telegram’s @BotFather. Saved tokens are encrypted and never returned to your browser. Leave blank to keep the saved token.')}<input name="bot_token" type="password" autocomplete="new-password" maxlength="256" placeholder="${settings.token_saved?'Saved · leave blank to keep':'Token from @BotFather'}"></label><label>${fieldCaption('Chat ID','The target chat’s numeric ID, including the minus sign for groups, or an @channel username. Start a chat with your bot first; for a group, add the bot and allow it to send messages.')}<input name="chat_id" value="${e(settings.chat_id)}" maxlength="128" autocomplete="off" placeholder="e.g. -1001234567890"></label></div><div class="telegram-tools"><label class="compact-checkbox"><input name="clear_token" type="checkbox" ${settings.token_saved?'':'disabled'}>Remove saved token</label>${button('telegram-test',settings.demo?'Simulate test message':'Send test message','bell','small')}</div><div class="telegram-table-wrap" tabindex="0" aria-label="Per-server Telegram notification rules"><table class="telegram-table"><thead><tr><th scope="col">Server name</th>${telegramKinds.map(([,label])=>`<th scope="col">${label}<small>On · After / Repeat (min)</small></th>`).join('')}</tr></thead><tbody>${rows||'<tr><td colspan="6">Add a server to configure warning notifications.</td></tr>'}</tbody></table></div><p class="hint telegram-footnote">After = continuous warning duration. Repeat 0 = once until cleared. Uses each server’s warning thresholds, monitored volumes and per-resource limits; Resources covers load, fans, batteries and electrical sensors; paused servers send no alerts. Timers use fresh readings and survive restarts.</p><div id="telegram-delivery-status" class="hint" role="status">${telegramStatus(settings)}</div><details class="telegram-setup"><summary>Bot setup ${icon('down')}</summary><p class="hint">Create a bot with <a href="https://t.me/BotFather" target="_blank" rel="noopener noreferrer">@BotFather</a>, send your bot /start (or add it to a group), and enter the token and destination Chat ID above. Telegram’s getUpdates response includes the ID as message.chat.id. <a href="https://core.telegram.org/bots/tutorial" target="_blank" rel="noopener noreferrer">Telegram setup guide</a>.</p></details>${settings.demo?'<p class="hint">Demo: delivery is simulated; no messages leave Harbour.</p>':''}<div class="form-error" role="alert"></div><div class="form-actions"><button type="submit" class="primary">Save</button></div></form>`,true);
  $('.modal').classList.add('telegram-modal');
}
function telegramStatus(settings){
  return settings.last_error?e(settings.last_error):settings.last_sent?`${settings.demo?'Last simulated delivery':'Last delivery'}: ${e(new Date(settings.last_sent*1000).toLocaleString())}`:'No messages sent yet.';
}
async function saveTelegram(form){
  const rules=[...form.querySelectorAll('.telegram-rule')].map(cell=>{
    const [enabled,delay,repeat]=cell.querySelectorAll('input');
    return {server_id:cell.dataset.server,kind:cell.dataset.kind,enabled:enabled.checked,delay_seconds:Number(delay.value)*60,repeat_seconds:Number(repeat.value)*60};
  });
  const settings=await api('/notifications','PUT',{enabled:form.elements.telegram_enabled.checked,chat_id:form.elements.chat_id.value.trim(),bot_token:form.elements.bot_token.value.trim(),clear_token:form.elements.clear_token.checked,rules});
  form.elements.bot_token.value='';form.elements.bot_token.placeholder=settings.token_saved?'Saved · leave blank to keep':'Token from @BotFather';
  form.elements.clear_token.checked=false;
  // The submit handler restores disabled controls before this microtask runs.
  setTimeout(()=>{if(form.isConnected)form.elements.clear_token.disabled=!settings.token_saved;},0);
  $('#telegram-delivery-status',form).innerHTML=telegramStatus(settings);
  markFormSaved(form);toast('Notification settings saved');
}
async function testTelegram(){
  if(overlayDirty())throw new Error('Save your changes before sending a test message.');
  const result=await api('/notifications/test','POST');
  const status=$('#telegram-delivery-status');if(status)status.textContent=result.simulated?'Test simulated. No message was sent.':'Test message delivered.';
  toast(result.simulated?'Test simulated · no external message sent':'Telegram test message sent');
}
document.addEventListener('change',event=>{
  const cell=event.target.closest('.telegram-rule');
  if(cell&&event.target.type==='checkbox')for(const input of cell.querySelectorAll('input[type=number]'))input.disabled=!event.target.checked;
});
