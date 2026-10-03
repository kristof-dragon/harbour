'use strict';

function timezoneLabel(tz) {
  return tz ? `${e(tz.name || 'Zone name unavailable')} · ${e(tz.abbreviation || '')} (UTC${e(tz.offset || '')})` : 'Timezone pending';
}

function sidebarMetrics(server) {
  return [['cpu', 'cpu', server.metrics.cpu], ['memory', 'memory', server.metrics.memory.percent],
    ['disk', 'disk', Math.max(0, ...server.metrics.disks.map(d => d.percent))], ['temperature', 'temperature', server.metrics.temperature?.package]].map(([key, symbol, value]) => {
    const warning = server.warnings.some(w => key === 'temperature' ? w.kind === 'cpu_package' : w.id === key || w.id.startsWith(key + ':'));
    return `<span class="resource-chip ${warning ? 'resource-warning' : ''}" ${warning ? `data-warning-server="${e(server.id)}" data-warning-kind="${key}"` : ''}>${icon(symbol)}${value==null?'—':Math.round(value)+(key==='temperature'?'°':'%')}</span>`;
  }).join('');
}

function serverWarnings(server) {
  if (!server.warnings.length) return '';
  return `<section class="warning-banner server-warnings" aria-label="Server warnings" role="status">${icon('warning')}<div><b>${count(server.warnings.length, 'active warning')}</b><ul>${server.warnings.map(w => `<li><strong>${e(w.title)}</strong><span>${e(w.detail)}</span></li>`).join('')}</ul>${server.error && server.checked ? `<p>Last successful reading: ${age(server.checked)}.</p>` : ''}</div></section>`;
}

let tooltipTimer;
function hideWarningTooltip() { clearTimeout(tooltipTimer); $('#warning-tooltip')?.remove(); }
function showWarningTooltip(target) {
  const server = state.data?.servers.find(s => s.id === (target.dataset.warningServer || target.dataset.id));
  if (!server?.warnings.length) return;
  hideWarningTooltip();
  const kind = target.dataset.warningKind;
  const warnings = server.warnings.filter(w => !kind || w.id === kind || w.id.startsWith(kind + ':'));
  const tip = document.createElement('div');
  tip.id = 'warning-tooltip'; tip.className = 'warning-tooltip'; tip.setAttribute('role', 'tooltip');
  tip.innerHTML = `<b>${e(server.name)}</b><ul>${warnings.map(w => `<li><strong>${e(w.title)}</strong><span>${e(w.detail)}</span></li>`).join('')}</ul>`;
  document.body.append(tip);
  const rect = target.getBoundingClientRect(), bounds = tip.getBoundingClientRect();
  tip.style.left = Math.max(12, Math.min(innerWidth - bounds.width - 12, rect.right + 10)) + 'px';
  tip.style.top = Math.max(12, Math.min(innerHeight - bounds.height - 12, rect.top)) + 'px';
  target.setAttribute('aria-describedby', 'warning-tooltip');
  tip.addEventListener('pointerenter', () => clearTimeout(tooltipTimer));
  tip.addEventListener('pointerleave', hideWarningTooltip);
}
document.addEventListener('pointerover', event => { const target = event.target.closest('[data-warning-server]'); if (target && !target.contains(event.relatedTarget)) showWarningTooltip(target); });
document.addEventListener('pointerout', event => { const target = event.target.closest('[data-warning-server]'); if (target && !target.contains(event.relatedTarget)) tooltipTimer = setTimeout(hideWarningTooltip, 160); });
document.addEventListener('focusin', event => { if (event.target.matches('.server-item.has-warning')) showWarningTooltip(event.target); });
document.addEventListener('focusout', event => { if (event.target.matches('.server-item')) hideWarningTooltip(); });
document.addEventListener('keydown', event => { if (event.key === 'Escape') hideWarningTooltip(); });
document.addEventListener('click', hideWarningTooltip);

let helpTarget=null,helpTimer;
function hideHelpTooltip(){
  clearTimeout(helpTimer);$('#help-tooltip')?.remove();helpTarget?.removeAttribute('aria-describedby');helpTarget=null;
}
function showHelpTooltip(target){
  hideHelpTooltip();
  const tip=document.createElement('div');tip.id='help-tooltip';tip.className='warning-tooltip help-tooltip';tip.setAttribute('role','tooltip');tip.textContent=target.dataset.help;
  document.body.append(tip);helpTarget=target;target.setAttribute('aria-describedby','help-tooltip');
  const rect=target.getBoundingClientRect(),bounds=tip.getBoundingClientRect();
  tip.style.left=Math.max(12,Math.min(innerWidth-bounds.width-12,rect.left))+'px';
  tip.style.top=Math.max(12,Math.min(innerHeight-bounds.height-12,rect.bottom+bounds.height+8<innerHeight?rect.bottom+8:rect.top-bounds.height-8))+'px';
  tip.addEventListener('pointerenter',()=>clearTimeout(helpTimer));tip.addEventListener('pointerleave',hideHelpTooltip);
}
document.addEventListener('pointerover',event=>{const target=event.target.closest('[data-help]');if(target&&!target.contains(event.relatedTarget))showHelpTooltip(target);});
document.addEventListener('pointerout',event=>{const target=event.target.closest('[data-help]');if(target&&!target.contains(event.relatedTarget))helpTimer=setTimeout(hideHelpTooltip,160);});
document.addEventListener('focusin',event=>{if(event.target.matches('[data-help]'))showHelpTooltip(event.target);});
document.addEventListener('focusout',event=>{if(event.target.matches('[data-help]'))hideHelpTooltip();});
document.addEventListener('click',event=>{if(!event.target.closest('[data-help],#help-tooltip'))hideHelpTooltip();});

const keySizes = {ed25519: {normal: 256}, ecdsa: {normal: 256, high: 384, xhigh: 521}, rsa: {normal: 3072, high: 4096, xhigh: 6144, excessive: 8192}};
const keyNotes = {ed25519:'Ed25519 has a fixed 256-bit key size. Its strength is not directly comparable with RSA bit lengths.',ecdsa:'ECDSA supports 256, 384 and 521-bit curves. There is no larger Excessive tier.',rsa:'Normal 3072 · High 4096 · Xhigh 6144 · Excessive 8192 bits. Larger RSA keys take longer to generate and use. SHA-1 RSA authentication is disabled.'};
function keyTierOptions(algorithm) {
  return ['normal', 'high', 'xhigh', 'excessive'].map(t => `<option value="${t}" ${!keySizes[algorithm][t] ? 'disabled' : ''}>${{normal: 'Normal', high: 'High', xhigh: 'Xhigh', excessive: 'Excessive'}[t]} · ${keySizes[algorithm][t] ? keySizes[algorithm][t] + ' bits' : 'not supported'}</option>`).join('');
}
function keyFields() {
  if(state.keyMode==='import')return `<label>Private key<textarea name="private_key" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" autocomplete="off"></textarea></label><label style="margin-top:10px">Passphrase (if encrypted)<input name="passphrase" type="password" autocomplete="off"></label><div class="key-create-action">${button('create-key','Import key','key','',state.demo?'disabled':'')}</div>`;
  return `<div class="form-grid"><label>${fieldCaption('Algorithm','Ed25519 is recommended for unattended SSH. ECDSA and RSA are available for host compatibility.')}<select name="key_algorithm" aria-label="Algorithm"><option value="ed25519">Ed25519 · recommended</option><option value="ecdsa">ECDSA · NIST curves</option><option value="rsa">RSA · SHA-2 signatures</option></select></label><label>${fieldCaption('Key-size tier',keyNotes.ed25519)}<select name="key_tier" aria-label="Key-size tier" disabled>${keyTierOptions('ed25519')}</select></label></div><div class="key-create-action">${button('create-key',state.connection?'Generate replacement key':'Generate key','key','',state.demo?'disabled':'')}</div>`;
}
function updateKeyTiers(){
  const algorithm=$('[name=key_algorithm]').value,select=$('[name=key_tier]');
  select.innerHTML=keyTierOptions(algorithm);select.disabled=algorithm==='ed25519';
  $('[aria-label="Key-size tier help"]').dataset.help=keyNotes[algorithm];hideHelpTooltip();
}
function keyOutput(){
  const existing=state.connection?.key?.id===state.key.id;
  return `<div class="key-output"><div class="between"><span class="tag green">${icon('check')}${existing?'Current SSH key':'Key ready'}</span>${help('Key installation','Install this public key in the selected SSH account’s ~/.ssh/authorized_keys using its password. Existing keys are preserved; duplicates are skipped. The host fingerprint is checked before password authentication. A fresh key-only connection verifies installation.')}</div><div class="key-install-row"><label>${fieldCaption('One-time SSH password','Used only for this installation, never saved. The server must allow password authentication.')}<input name="install_password" aria-label="One-time SSH password" type="password" autocomplete="off" maxlength="1024"></label>${button('install-key','Install key on server','key','small',state.demo?'disabled':'')}</div><div id="key-install-result" aria-live="polite"></div><details class="manual-key"><summary>Manual installation</summary><code class="code-block">restrict ${e(state.key.public_key)}</code><div class="flex">${button('copy-key','Copy public key','copy','small')}${help('Manual installation','Add this line to ~/.ssh/authorized_keys for the selected account. The restrict option disables forwarding and interactive terminals while allowing Harbour’s commands.')}</div></details></div>`;
}
function updatePasswordRequirement(){
  const form=$('#onboard-form');if(!form)return;
  const password=$('[name=ssh_password]',form),connection=state.connection;
  password.disabled=state.authMethod!=='password';
  password.required=state.authMethod==='password'&&(!connection?.has_password||!sameHost(connection,onboardTarget(form))||connection.username!==$('[name=username]',form).value.trim());
}
function setPasswordChoice(checked){
  $('#password-auth-toggle').checked=checked;
  state.authMethod='key';$('#password-auth-warning').hidden=!checked;$('#password-auth-fields').hidden=true;$('#ssh-key-section').hidden=checked;
  $('[name=ssh_password]').value='';updatePasswordRequirement();
}
function confirmPasswordAuth(){
  state.authMethod='password';$('#password-auth-warning').hidden=true;$('#password-auth-fields').hidden=false;$('#ssh-key-section').hidden=true;
  if($('[name=install_password]'))$('[name=install_password]').value='';
  updatePasswordRequirement();$('[name=ssh_password]').focus();
}
function busyOnboarding(form){
  form.dataset.busy='true';
  const controls=[...form.querySelectorAll('input,select,textarea,button')].filter(el=>el.dataset.action!=='close').map(el=>[el,el.disabled]);
  controls.forEach(([el])=>el.disabled=true);
  return ()=>{delete form.dataset.busy;controls.forEach(([el,disabled])=>el.disabled=disabled);};
}
async function installOnboardKey(){
  const form=$('#onboard-form'),password=$('[name=install_password]',form),output=$('#key-install-result');
  for(const name of ['host','port','username','fingerprint'])if(!$(`[name=${name}]`,form).reportValidity())return;
  if(!password.value)throw new Error('Enter the SSH account password to install the key.');
  const body={...onboardTarget(form),username:$('[name=username]',form).value.trim(),fingerprint:$('[name=fingerprint]',form).value.trim(),key_id:state.key.id,password:password.value};
  password.value='';const restore=busyOnboarding(form);
  output.innerHTML='<p class="hint" role="status">Installing key and verifying SSH access…</p>';
  try{
    const result=await api('/ssh/install-key','POST',body);
    if(form.isConnected)output.innerHTML=`<p class="fingerprint-accepted" role="status">${icon('check')}${result.status==='present'?'Key already installed':'Key installed'} · key-only login verified</p>`;
  }catch(error){if(form.isConnected)output.innerHTML=`<p class="form-error" role="alert">${e(error.message)}</p>`;}
  finally{body.password='';restore();}
}

function onboardTarget(form) {
  return {host:$('[name=host]',form).value.trim(),port:Number($('[name=port]',form).value)};
}
function sameHost(a,b) {return a?.host===b?.host&&a?.port===b?.port;}
function onboardIdentityChanged(input) {
  if(!['host','port','username','fingerprint'].includes(input.name))return;
  const form=input.closest('form');updatePasswordRequirement();
  if($('#key-install-result'))$('#key-install-result').innerHTML='';
  if(input.name==='username')return;
  state.hostProbe=null;
  if(state.acceptedHost&&['host','port'].includes(input.name)&&!sameHost(state.acceptedHost,onboardTarget(form))){
    $('[name=fingerprint]',form).value='';
    state.acceptedHost=null;
    $('#fingerprint-result').innerHTML='<p class="hint" role="status">Server address changed. Fetch or paste its fingerprint again.</p>';
  }else{
    if(input.name==='fingerprint')state.acceptedHost=null;
    $('#fingerprint-result').innerHTML='';
  }
}
async function probeFingerprint() {
  const form=$('#onboard-form'),target=onboardTarget(form),output=$('#fingerprint-result');
  if(!target.host){$('[name=host]',form).reportValidity();return;}
  if(!$('[name=port]',form).reportValidity())return;
  const request={...target};state.hostProbe=request;
  output.innerHTML='<p class="hint" role="status">Reading the server’s SSH host key…</p>';
  try{
    const result=await api('/ssh/fingerprint','POST',target);
    if(!form.isConnected||state.hostProbe!==request||!sameHost(target,onboardTarget(form)))return;
    state.hostProbe=result;
    output.innerHTML=`<div class="fingerprint-preview"><div class="between"><b>Host key received</b><span class="tag">${e(result.key_type)}</span></div><p class="hint">${e(result.host)}:${result.port} · reached ${e(result.address)}</p><code class="code-block">${e(result.fingerprint)}</code><div class="flex">${button('accept-fingerprint','Accept fingerprint','shield','small')}${help('Accept fingerprint','Fetching a key does not independently verify identity. Compare it through a trusted source, or accept it as trust on first connection. Later changes to the pinned key are rejected.')}</div></div>`;
  }catch(error){
    if(form.isConnected&&state.hostProbe===request){state.hostProbe=null;output.innerHTML=`<p class="form-error" role="alert">${e(error.message)}</p>`;}
  }
}
function acceptFingerprint() {
  const form=$('#onboard-form'),probe=state.hostProbe;
  if(!form||!probe?.fingerprint||!sameHost(probe,onboardTarget(form)))return;
  $('[name=fingerprint]',form).value=probe.fingerprint;
  state.acceptedHost={host:probe.host,port:probe.port};state.hostProbe=null;
  $('#fingerprint-result').innerHTML=`<p class="fingerprint-accepted" role="status">${icon('check')}Fingerprint accepted for ${e(probe.host)}:${probe.port}.</p>`;
}

async function createKey(){
  const form=$('#onboard-form'),private_key=$('[name=private_key]',form)?.value,passphrase=$('[name=passphrase]',form)?.value;
  if(state.keyMode==='import'&&!private_key?.trim())throw new Error('Paste an existing private key before importing.');
  const body=state.keyMode==='import'?{private_key,passphrase:passphrase||null}:{algorithm:$('[name=key_algorithm]',form).value,tier:$('[name=key_tier]',form).value};
  const output=$('#key-output'),restore=busyOnboarding(form);
  output.innerHTML='<p class="hint" role="status">Preparing SSH key…</p>';
  try{
    const key=await api('/keys','POST',body);if(!form.isConnected)return;
    state.key=key;output.innerHTML=keyOutput();
    for(const name of ['private_key','passphrase'])if($(`[name=${name}]`,form))$(`[name=${name}]`,form).value='';
  }catch(error){if(form.isConnected){output.innerHTML=state.key?keyOutput():'';throw error;}}
  finally{restore();}
}

function reauthFields() {
  return `<label>Current password<input type="password" name="password" required autocomplete="current-password"></label>${state.user.mfa_enabled ? '<label>Authenticator or recovery code<input name="code" required autocomplete="one-time-code" maxlength="80"></label>' : ''}`;
}
async function account() {
  const security = await api('/security/me');
  state.user.mfa_enabled = security.mfa_enabled;
  modal('My account', `<p>Signed in as <b>${e(state.user.name)}</b> · ${admin() ? 'Administrator' : 'Read-only user'}</p><div class="between"><h3>Two-factor authentication</h3><span class="tag ${security.mfa_enabled ? 'green' : ''}">${security.mfa_enabled ? 'Enabled' : 'Not enabled'}</span></div><p class="hint">Use an authenticator app such as 2FAS, Aegis or your existing TOTP app. Setup includes eight one-use recovery codes.</p>${state.demo ? '<p class="auth-message">2FA enrollment is disabled in the demo because its demo sign-in bypasses authentication.</p>' : ''}<div class="detail-actions">${security.mfa_enabled ? button('mfa-recovery', 'Replace recovery codes', 'key', 'small') + button('mfa-disable', 'Disable 2FA', '', 'small danger') : button('mfa-begin', 'Set up 2FA', 'shield', 'primary', state.demo ? 'disabled' : '')}</div>${security.mfa_enabled ? `<p class="hint">${security.recovery_remaining} recovery codes remaining.</p>` : ''}<hr class="section-rule"><h3>Active sessions</h3><p class="hint">Idle timeout: ${security.idle_minutes} minutes · absolute limit: ${security.absolute_hours} hours. Background monitoring does not keep a session alive.</p>${security.sessions.map(s => `<div class="session-row"><span class="mono">${e(s.address)}</span><span>${s.current ? 'This session' : 'Other session'}</span><small class="muted">Active ${age(s.last_activity)}</small></div>`).join('')}${button('revoke-sessions', 'Sign out other sessions', 'logout', 'small', 'style="margin-top:12px"')}<hr class="section-rule"><form id="password-form"><h3>Change password</h3><div class="stack"><label>Current password<input type="password" name="current" required autocomplete="current-password"></label><label>New password<input type="password" name="password" minlength="12" required autocomplete="new-password"></label>${state.user.mfa_enabled ? '<label>Authenticator or recovery code<input name="code" required autocomplete="one-time-code"></label>' : ''}</div><div class="form-error" role="alert"></div><div class="form-actions"><button type="submit" class="primary">Change password</button></div></form><hr class="section-rule">${button('restore-dismissals', 'Restore my dismissed updates', 'bell')}`);
}

async function securityPanel(tab = 'policy') {
  state.securityTab = tab;
  const tabs = `<nav class="tabs">${button('security-policy', 'Session & login policy', '', tab === 'policy' ? 'active' : '')}${button('security-log', 'Sign-in log & bans', '', tab === 'log' ? 'active' : '')}</nav>`;
  if (tab === 'policy') {
    const p = await api('/security/policy');
    modal('Security & sign-ins', `${tabs}<form id="policy-form"><div class="form-grid"><label>Idle session timeout (minutes)<input type="number" name="idle_minutes" min="1" max="240" value="${p.idle_minutes}" required></label><label>Absolute session limit (hours)<input type="number" name="absolute_hours" min="1" max="168" value="${p.absolute_hours}" required></label><label>IP ban duration (minutes)<input type="number" name="ban_minutes" min="1" max="1440" value="${p.ban_minutes}" required></label><div><div class="detail-label">Ban threshold</div><p>5 failures in 15 minutes</p></div><label class="compact-checkbox full"><input name="bind_ip" type="checkbox" ${p.bind_ip ? 'checked' : ''}>Require sign-in when the client IP changes</label><label class="compact-checkbox full"><input name="bind_browser" type="checkbox" ${p.bind_browser ? 'checked' : ''}>Require sign-in when the browser identity changes</label></div><p class="hint">IP changes, including switching networks or IPv4/IPv6, invalidate the session. Browser binding checks the User-Agent; it is an extra signal, not proof of device ownership.</p><div class="info-box"><h3>Nginx Proxy Manager</h3><p>${p.secure_cookie ? 'HTTPS-only, host-bound cookies enabled.' : 'Local HTTP mode. Enable secure cookies when you deploy behind HTTPS.'}</p><p>Trusted proxy: <span class="mono">${p.trusted_proxies.length ? p.trusted_proxies.map(e).join(', ') : 'None configured; forwarded IP headers are ignored.'}</span></p><p class="hint">Configure the trusted proxy address and public origin in the deployment settings. Avoid trusting an entire shared Docker network.</p></div><hr class="section-rule"><h3>Confirm policy changes</h3><div class="stack">${reauthFields()}</div><p class="hint">Saving rotates your session and signs out your other sessions. Limits apply to all users.</p>${state.demo ? '<p class="hint">This preview uses a generated demo password; configure real authentication policies in the production instance.</p>' : ''}<div class="form-error" role="alert"></div><div class="form-actions"><button type="submit" class="primary">Save security policy</button></div></form>`, true);
  } else {
    const data = await api('/security/log?offset=' + state.logOffset);
    modal('Security & sign-ins', `${tabs}<p class="hint">Authentication events retained for 90 days. Times below are UTC. Passwords, OTPs and recovery codes are never logged.</p><div class="auth-log-wrap"><table class="auth-log"><thead><tr><th>Time (UTC)</th><th>Username</th><th>Client IP</th><th>Result</th></tr></thead><tbody>${data.entries.map(x => `<tr><td class="mono">${e(new Date(x.created * 1000).toISOString().replace('T', ' ').slice(0, 19))}</td><td>${e(x.username)}</td><td class="mono">${e(x.address)}</td><td><span class="tag ${x.outcome === 'success' ? 'green' : /fail|blocked|revoked/.test(x.outcome) ? 'red' : ''}">${e(x.outcome.replaceAll('_', ' '))}</span>${x.detail ? `<small>${e(x.detail)}</small>` : ''}</td></tr>`).join('') || '<tr><td colspan="4">No authentication events yet.</td></tr>'}</tbody></table></div><div class="between log-pagination"><span class="muted">${Math.min(state.logOffset + 1, data.total)}–${Math.min(state.logOffset + 50, data.total)} of ${data.total}</span><div class="flex">${button('log-prev', 'Previous', '', 'small', state.logOffset === 0 ? 'disabled' : '')}${button('log-next', 'Next', '', 'small', state.logOffset + 50 >= data.total ? 'disabled' : '')}</div></div><hr class="section-rule"><h3>Active IP bans</h3>${data.bans.map(b => `<div class="session-row"><span class="mono">${e(b.address)}</span><span>Until ${e(new Date(b.until * 1000).toISOString().replace('T', ' ').slice(0, 19))} UTC</span>${button('unban', 'Unban', '', 'small', `data-address="${e(b.address)}"`)}</div>`).join('') || '<p class="muted">No active bans.</p>'}<div class="form-error" role="alert"></div>`, true);
  }
}

function showRecoveryCodes(result) {
  state.user = result;
  modal('Save your recovery codes', `<p>2FA is enabled. Save these eight codes in a safe place. Each code works once in place of your authenticator code.</p><p class="auth-message">These codes are shown only now. Replacing them invalidates the previous set.</p><code class="code-block recovery-output">${result.recovery_codes.map(e).join('\n')}</code><p class="hint">Your other sessions have been signed out. A used authenticator code cannot be reused during the same 30-second step.</p><div class="form-actions">${button('account', 'I have saved my codes', 'check', 'primary')}</div>`);
  // Remove plaintext recovery codes from long-lived user state after rendering.
  delete state.user.recovery_codes;
}

const securityActions = new Set(['security', 'security-policy', 'security-log', 'log-prev', 'log-next', 'unban', 'revoke-sessions', 'mfa-begin', 'mfa-disable', 'mfa-recovery']);
async function handleSecurityAction(action, el) {
  if (['security', 'security-policy'].includes(action)) return securityPanel('policy');
  if (action === 'security-log') { state.logOffset = 0; return securityPanel('log'); }
  if (action === 'log-prev' || action === 'log-next') { state.logOffset = Math.max(0, state.logOffset + (action === 'log-next' ? 50 : -50)); return securityPanel('log'); }
  if (action === 'unban') { await api('/security/unban', 'POST', {address: el.dataset.address}); return securityPanel('log'); }
  if (action === 'revoke-sessions') { await api('/security/sessions', 'DELETE'); toast('Other sessions signed out'); return account(); }
  const operation = {'mfa-begin': 'begin', 'mfa-disable': 'disable', 'mfa-recovery': 'recovery'}[action];
  modal({'begin': 'Set up two-factor authentication', 'disable': 'Disable two-factor authentication', 'recovery': 'Replace recovery codes'}[operation], `<p>${operation === 'begin' ? 'Confirm your password to start authenticator setup.' : operation === 'disable' ? 'Confirm your password and second factor to disable 2FA.' : 'Confirm your password and second factor. The old recovery codes will stop working.'}</p><form id="mfa-auth-form" data-operation="${operation}"><div class="stack">${reauthFields()}</div><div class="form-error" role="alert"></div><div class="form-actions"><button type="submit" class="${operation === 'disable' ? 'danger' : 'primary'}">${operation === 'begin' ? 'Continue' : 'Confirm'}</button></div></form>`);
}

const securityForms = new Set(['policy-form', 'mfa-auth-form', 'mfa-confirm-form']);
async function handleSecurityForm(form, data) {
  if (form.id === 'policy-form') {
    const result = await api('/security/policy', 'PUT', {idle_minutes: Number(data.idle_minutes), absolute_hours: Number(data.absolute_hours), ban_minutes: Number(data.ban_minutes), bind_ip: !!data.bind_ip, bind_browser: !!data.bind_browser, password: data.password, code: data.code || ''});
    state.user = result; toast('Security policy saved'); return securityPanel('policy');
  }
  if (form.id === 'mfa-confirm-form') {
    const result = await api('/security/totp/confirm', 'POST', data); state.totpSetup = null; showRecoveryCodes(result); return;
  }
  const operation = form.dataset.operation;
  const result = await api('/security/totp/' + operation, 'POST', data);
  if (operation === 'begin') {
    state.totpSetup = true;
    modal('Connect your authenticator', `<p>Scan this QR code in your authenticator app, or enter the setup key manually. Nothing leaves Harbour to generate this code.</p><div class="totp-qr"><img src="${e(result.qr)}" alt="Authenticator setup QR code"></div><label>Manual setup key<code class="code-block">${e(result.secret)}</code></label><form id="mfa-confirm-form"><label style="margin-top:18px">Six-digit authenticator code<input name="code" inputmode="numeric" pattern="[0-9]{6}" minlength="6" maxlength="6" autocomplete="one-time-code" required></label><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary" type="submit">Enable 2FA</button></div></form>`);
  } else if (operation === 'recovery') showRecoveryCodes(result);
  else { state.user = result; toast('2FA disabled; other sessions signed out'); await account(); }
}

// Only actual user interaction sends activity heartbeats. Dashboard polling is read-only.
let activityPending = false;
for (const type of ['pointerdown', 'keydown', 'scroll', 'touchstart']) document.addEventListener(type, event => {
  if (event.isTrusted && state.user) activityPending = true;
}, {passive: true, capture: true});
setInterval(async () => {
  if (!activityPending || !state.user || document.hidden) return;
  activityPending = false;
  try { await api('/session/activity', 'POST'); } catch (error) { if (state.user) toast(error.message, true); }
}, 20000);
