'use strict';
function recordingLabel(s){
  if(s.server_type==='openwrt')return 'Read-only OpenWRT probes';
  return s.recording?.mode==='local'?'Local recorder':s.recording?.mode==='fallback'?'Remote probes · recorder unavailable':s.recording?.mode==='remote'?'Remote probes':'Collection mode pending';
}
function recordingSettings(s){
  if(s.server_type==='openwrt')return `<section class="settings-section"><h3>OpenWRT collection</h3><p>Read-only router probes. Configure fast local recording in the Network tab. Router installation and configuration changes are disabled.</p>${['record_seconds','disk_seconds','inventory_seconds'].map(k=>`<input type="hidden" name="${k}" value="${s[k]||60}">`).join('')}</section>`;
  return `<section class="settings-section"><div class="settings-section-heading"><h3>Resource collection</h3><span class="tag">${e(recordingLabel(s))}</span></div>
    <p class="hint">Installing the recorder on this host enables local recording automatically. Both modes use the existing SSH connection. Pausing monitoring pauses transfer; an installed recorder keeps recording.</p>
    <div class="threshold-row"><label>Recorder sample interval (s)<input name="record_seconds" aria-label="Recorder sample interval (seconds)" type="number" min="15" max="3600" required value="${s.record_seconds||60}"></label><label>Disk capacity interval (s)<input name="disk_seconds" aria-label="Disk capacity interval (seconds)" type="number" min="15" max="3600" required value="${s.disk_seconds||300}"></label><label>Docker inventory interval (s)<input name="inventory_seconds" aria-label="Docker inventory interval (seconds)" type="number" min="30" max="3600" required value="${s.inventory_seconds||300}"></label></div>
    <p class="hint">The poll interval controls remote probes or recorder collection. Samples keep their original times when collected later. Alerts use the latest collected reading and can be delayed by the collection interval.</p>
    <div class="form-actions">${button('recorder-setup','Install recorder','download','small')}${button('recorder-remove','Remove recorder','trash','ghost small')}</div></section>`;
}
const recorderShellArg=value=>"'"+String(value).replaceAll("'","'\"'\"'")+"'";
function recorderSetup(s,remove=false){
  if(s.server_type==='openwrt'){toast('OpenWRT is read-only; router installation is disabled.',true);return;}
  const install=`sudo python3 resource_install.py --reader ${recorderShellArg(s.username)}`;
  const uninstall='sudo python3 /usr/local/libexec/harbour-resources/resource_install.py --uninstall';
  modal(remove?'Remove resource recorder':'Install resource recorder',`<div class="stack"><p><b>${e(s.name)}</b> · ${e(recordingLabel(s))}</p>
    ${remove?`<p>Run this on the monitored host to stop and remove its recorder. Harbour will switch to remote probes at the next check. The existing resource history in Harbour is retained.</p><code>${uninstall} --dry-run</code><code>${uninstall}</code><p>The local queue is retained by default. To also delete that queue, add <code>--purge-data</code> when uninstalling. Collect pending samples before deleting it.</p><p>If the installed files are unavailable, extract a fresh bundle and run <code>sudo python3 resource_install.py --uninstall</code> there. Removal does not require the original SSH account to still exist.</p>`:
    `${collectorSetupControls(s,'resources')}<details><summary>Manual installation</summary><p>Download and extract the bundle on this host, then preview and install:</p><code>${e(install.replace('sudo ',''))} --dry-run</code><code>${e(install)}</code><p>The recorder starts at boot and runs as <b>${e(s.username)}</b>. Installation needs administrator access. It opens no network port; Harbour reads its private local socket through SSH.</p><p>There is no enable switch. A healthy installed recorder is selected automatically; hosts without one use remote probes. Reinstalling upgrades the recorder while preserving its queue. Use the same reader account.</p><p>The queue retains up to 20,000 samples or 64 MiB of sample payloads, whichever fills first. Queue loss is reported. Samples are durably committed individually.</p></details>`}
    <p><a class="button" href="/api/resource-recorder/download">Download recorder bundle</a></p><p class="hint">The authentication/login collector is a separate service. Removing this resource recorder leaves it running.</p></div>`);
}
let bootRequest=0;
async function bootHistory(s){
  const request=++bootRequest;
  modal('Uptime & reboot history',`<p>Loading boot observations for ${e(s.name)}…</p>`);
  try{
    const data=await api(`/servers/${encodeURIComponent(s.id)}/boots`);
    if(request!==bootRequest||state.modal!=='Uptime & reboot history'||current()?.id!==s.id)return;
    const stamp=value=>value==null?'Unknown':new Date(value*1000).toLocaleString();
    modal('Uptime & reboot history',`<div class="stack"><p><b>${e(s.name)}</b> · uptime at latest sample: <b>${uptime(s.metrics?.uptime)}</b></p><p>Different boot identities confirm that the host rebooted. Restarting the recorder does not create a reboot. Boot times are estimated from the host’s clock and uptime.</p>
      <div class="resource-settings-wrap"><table class="resource-settings"><thead><tr><th>Observation</th><th>Estimated boot time</th><th>First sample</th><th>Received by Harbour</th></tr></thead><tbody>${data.boots.map(b=>`<tr><td>${b.initial?'First observed boot':'Reboot observed'}</td><td>${e(stamp(b.boot_at))}</td><td>${e(stamp(b.first_sample))}</td><td>${e(stamp(b.detected_at))}</td></tr>`).join('')||'<tr><td colspan="4">Boot tracking starts with the next successful resource sample.</td></tr>'}</tbody></table></div>
      <p class="hint">Latest 100 observations within the ${data.retention_days}-day retention window. Remote probes cannot detect every intervening reboot while disconnected. A recorder preserves sampled boots while its local queue has capacity.</p></div>`);
  }catch(err){toast(err.message);}
}
document.addEventListener('click',event=>{
  const action=event.target.closest('[data-action]')?.dataset.action;
  if(action==='recorder-setup')recorderSetup(current());
  if(action==='recorder-remove')recorderSetup(current(),true);
  if(action==='boot-history')bootHistory(current());
});
