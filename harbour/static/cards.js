'use strict';
const cardSizes=[['small','S'],['medium','M'],['large','L']];
const pendingCardLayouts=new Map(),cardSaves=new Set();
let resourceDrag=null,resourceGhost=null,resourceDropSlot=null,cardGridObserver=null,observedCardGrid=null;
const resourceCardGrids=new WeakMap();
const cardLayout=s=>pendingCardLayouts.get(s.id)||s.card_layout||{default_size:'medium',sizes:{},order:[]};
const resourceCardId=r=>r.group==='load'?'load':r.id;
const resourceCardSize=(s,id)=>cardLayout(s).sizes?.[id]||cardLayout(s).default_size||'medium';
function cardSizeField(s,id,instance=id){return `<div class="card-size-field" data-card-size-field="${e(id)}"><input type="hidden" value="${resourceCardSize(s,id)}" data-card-size-value="${e(id)}">${slidingControl('resource-size-'+encodeURIComponent(instance),'Card size',cardSizes,resourceCardSize(s,id),'card-settings-size',`data-card="${e(id)}"`)}</div>`;}
function cardLayoutCommonSize(s){const sizes=new Set(resourceCardIds(s).map(id=>resourceCardSize(s,id)));return sizes.size===1?[...sizes][0]:'mixed';}
function cardLayoutControls(s){return `<div class="card-layout-controls"><span>Set all cards</span>${slidingControl('all-resource-sizes','Set all resource card sizes',[['small','Small'],['medium','Medium'],['large','Large']],cardLayoutCommonSize(s),'card-size-all')}</div>`;}
function resourceCardIds(s){return [...new Set(['cpu','memory','disk','load',...(s.metrics?.resources||[]).map(resourceCardId)])];}
function arrangeResourceMarkup(s,markup){
  const template=document.createElement('template');template.innerHTML=markup;
  const articles=[...template.content.children],layout=cardLayout(s),primary=primaryResource(s);
  for(const article of articles){
    const key=article.dataset.key,id=key==='metric:temperature'?(primary?.id||'temperature'):key.startsWith('metric:')?key.slice(7):key.slice(9);
    article.dataset.resourceCard=id;article.dataset.cardSize=resourceCardSize(s,id);
    const row=resourceRow(s,id),label=row?.label||resourceMeta[id]?.label||'CPU / SoC';
    const header=article.querySelector('.metric-label'),chart=article.querySelector('.card-chart'),value=article.querySelector('.metric-value,.load-values'),bottom=article.querySelector('.metric-bottom'),sensors=article.querySelector('.sensor-details');
    const tag=header.querySelector('.tag'),symbol=header.querySelector('.icon');
    header.replaceChildren();if(symbol)header.append(symbol);
    const title=document.createElement('span');title.className='resource-card-title';title.textContent=id===primary?.id?'CPU / SoC':label;title.title=label;header.append(title);if(tag)header.append(tag);
    if(admin())header.insertAdjacentHTML('beforeend',`${iconButton('resource-card-settings',label+' card settings','info',`data-card="${e(id)}"`)}<button class="resource-card-handle" type="button" aria-label="Move ${e(label)} card" title="Drag to arrange; arrow keys also move this card" draggable="false">⠿</button>`);
    const body=document.createElement('div');body.className='resource-card-body';const reading=document.createElement('div');reading.className='resource-card-reading';if(value)reading.append(value);if(bottom)reading.append(bottom);body.append(reading);if(chart)body.append(chart);
    article.replaceChildren(header,body);
    const details=document.createElement('div');details.className='resource-card-details';
    const source=row?.source||({cpu:'Host CPU',memory:'Host memory',disk:cardDisk(s)?.mount||'Volume',load:'System load'}[id])||'Temperature sensor';
    const limits=row?.warn?[[row.low,'Low'],[row.high,'High']].filter(([v])=>v!=null).map(([v,k])=>`${k}: ${sensorValue(v,row.unit)}`).join(' · '):row?'Warnings off':id==='disk'?`${s.thresholds.disk}% used · ${s.thresholds.disk_free_gb} GB free`:id==='load'?`${s.metrics?.cores??'—'} logical CPUs`:'Selected sensor';
    details.innerHTML=`<div><span>Source</span><b>${e(source)}</b></div><div><span>${id==='load'?'Capacity':'Warnings'}</span><b>${e(limits)}</b></div>`;article.append(details);if(sensors)article.append(sensors);
  }
  const order=layout.order||[];
  articles.sort((a,b)=>(order.indexOf(a.dataset.resourceCard)<0?order.length:order.indexOf(a.dataset.resourceCard))-(order.indexOf(b.dataset.resourceCard)<0?order.length:order.indexOf(b.dataset.resourceCard)));
  return articles.map(article=>article.outerHTML).join('');
}
function setCardStyle(el,key,value){if(el.style[key]!==value)el.style[key]=value;}
function resourceGridGeometry(width){
  const gap=10,columns=Math.max(1,Math.floor((width+gap)/230)),medium=2*Math.floor((width-gap*(columns-1))/columns/2);
  return {width,gap,rowHeight:88,rowStep:98,widths:{small:width<390?Math.max(138,Math.floor((width-gap)/2)):Math.round(medium*.75),medium,large:medium*1.5}};
}
function resourceSlotFree(slot,placed,geometry){
  return slot.x>=0&&slot.x+slot.width<=geometry.width&&slot.row>=0&&slot.row<=4096&&!placed.some(other=>slot.row<other.row+other.span&&slot.row+slot.span>other.row&&slot.x<other.x+other.width+geometry.gap-.5&&slot.x+slot.width+geometry.gap-.5>other.x);
}
function resourceFreeSlot(card,desired,placed,geometry){
  // New cards flow across full-height rows. Only explicit positions use the lower half.
  for(let row=desired.row;row<=4096;row+=2){
    const x=row===desired.row?desired.x:0;
    const choices=[x,0,...placed.map(other=>other.x+other.width+geometry.gap)].filter(v=>v>=x);
    for(const left of [...new Set(choices)].sort((a,b)=>a-b)){
      const slot={...card,x:left,row};if(resourceSlotFree(slot,placed,geometry))return slot;
    }
  }
  if(desired.row>0||desired.x>0)return resourceFreeSlot(card,{x:0,row:0},placed,geometry);
  throw new Error('The resource card grid is full.');
}
function planResourceCards(items,width,positions={}){
  const geometry=resourceGridGeometry(width),placed=[],slots=new Map();
  const cards=items.map(item=>({...item,width:Math.min(width,geometry.widths[item.size]||geometry.widths.medium),span:item.size==='small'?1:2}));
  // Reserve saved slots first, including intentional gaps, before adding discoveries.
  for(const card of cards.filter(card=>positions[card.id])){
    const saved=positions[card.id],desired={x:Math.min(width-card.width,Math.round(saved.x*width)),row:saved.row};
    const slot=resourceFreeSlot(card,desired,placed,geometry);placed.push(slot);slots.set(card.id,slot);
  }
  let cursor={x:0,row:0};
  for(const card of cards.filter(card=>!positions[card.id])){
    const slot=resourceFreeSlot(card,cursor,placed,geometry);placed.push(slot);slots.set(card.id,slot);cursor={x:slot.x+slot.width+geometry.gap,row:slot.row};
  }
  return {geometry,slots};
}
function layoutResourceCards(){
  const grid=$('.resource-card-grid');if(!grid||resourceDrag?.started)return;
  const width=Math.floor(grid.clientWidth);if(!width)return;
  const cards=[...grid.querySelectorAll(':scope>[data-resource-card]')];
  const plan=planResourceCards(cards.map(card=>({id:card.dataset.resourceCard,size:card.dataset.cardSize})),width,cardLayout(current()).positions);
  resourceCardGrids.set(grid,plan);setCardStyle(grid,'gridTemplateColumns',`repeat(${width}, 1px)`);
  for(const card of cards){const slot=plan.slots.get(card.dataset.resourceCard);setCardStyle(card,'width',slot.width+'px');setCardStyle(card,'gridColumn',`${slot.x+1} / span ${slot.width}`);setCardStyle(card,'gridRow',`${slot.row+1} / span ${slot.span}`);}
  if(observedCardGrid!==grid){cardGridObserver?.disconnect();observedCardGrid=grid;cardGridObserver=new ResizeObserver(layoutResourceCards);cardGridObserver.observe(grid);}
}
async function saveCardPreferences(serverId,body){
  if(cardSaves.has(serverId))throw new Error('Please wait for the card settings to finish saving.');
  cardSaves.add(serverId);++dashboardSequence;
  const s=state.data.servers.find(s=>s.id===serverId);
  if(body.layout&&s){const old=cardLayout(s);pendingCardLayouts.set(serverId,{default_size:body.layout.default_size||old.default_size,sizes:{...(body.layout.reset_sizes?{}:old.sizes),...body.layout.sizes},order:body.layout.order||old.order,positions:{...old.positions,...body.layout.positions}});if(current()?.id===serverId)renderMain();}
  try{const saved=await api(`/servers/${serverId}/cards`,'PATCH',body);const server=state.data.servers.find(s=>s.id===serverId);if(server)server.card_layout=saved.card_layout;await load();return saved;}
  finally{pendingCardLayouts.delete(serverId);cardSaves.delete(serverId);if(current()?.id===serverId)renderMain();}
}
function overlayCardLayout(form){return {default_size:form.elements.card_default_size.value,reset_sizes:form.elements.card_reset_sizes.value==='1',sizes:Object.fromEntries([...form.querySelectorAll('[data-card-size-value]')].map(input=>[input.dataset.cardSizeValue,input.value]))};}
function setCardSizeChoice(el){
  const form=el.closest('form');if(!form||form.dataset.busy)return;
  for(const field of form.querySelectorAll('[data-card-size-field]'))if(field.dataset.cardSizeField===el.dataset.card){field.querySelector('input').value=el.dataset.value;updateSlidingControl(field.querySelector('.sliding-control'),el.dataset.value);}
}
function setOverlayCardSize(el){
  const form=el.closest('form');if(!form||form.dataset.busy)return;
  form.elements.card_default_size.value=el.dataset.value;form.elements.card_reset_sizes.value='1';
  updateSlidingControl(el.closest('.sliding-control'),el.dataset.value);
  for(const field of form.querySelectorAll('[data-card-size-field]')){field.querySelector('input').value=el.dataset.value;updateSlidingControl(field.querySelector('.sliding-control'),el.dataset.value);}
}
function resourceChoicesFromForm(form){return [...form.querySelectorAll('[data-resource-setting]')].map(row=>{
  const limit_mode=row.querySelector('[data-resource-limit=mode]').value;
  const limits=Object.fromEntries(['low','high'].map(side=>{const value=row.querySelector('[data-resource-limit='+side+']').value;return [side,limit_mode==='custom'&&value!==''?Number(value):null];}));
  return {id:row.dataset.resourceSetting,...Object.fromEntries(['monitor','warn','card'].map(key=>[key,row.querySelector('[data-resource-option='+key+']').checked])),limit_mode,...limits};
});}
function resourceCardSettings(el){
  const s=current(),id=el.dataset.card,rows=(s.metrics?.resources||[]).filter(r=>resourceCardId(r)===id),disks=id==='disk'?(s.metrics.disks||[]).filter(d=>d.monitor&&d.card):[];
  const label=resourceMeta[id]?.label||rows[0]?.label||'CPU / SoC',rect=el.getBoundingClientRect();
  modal(e(label)+' settings',`<form id="resource-card-form" data-server="${e(s.id)}" data-card="${e(id)}"><label>Card size</label>${cardSizeField(s,id)}<p class="hint">Large keeps Medium’s height and uses 1.5× its width.</p>${rows.map(r=>`<fieldset data-resource-setting="${e(r.id)}" data-group="${e(r.group)}">${rows.length>1?`<legend>${e(r.label)}</legend>`:''}<div class="card-checks">${['monitor','warn','card'].map((key,i)=>`<label class="check-line"><input type="checkbox" data-resource-option="${key}" ${r[key]?'checked':''} ${key!=='monitor'&&!r.monitor?'disabled':''}>${['Monitoring','Warning','Resource card'][i]}</label>`).join('')}</div><div class="card-warning-limits"><label>Warning limits<select data-resource-limit="mode" ${r.monitor&&r.warn?'':'disabled'}><option value="default" ${r.limit_mode==='default'?'selected':''}>${['cpu','memory','temperature'].includes(r.group)?'Inherited':'No defaults'}</option><option value="custom" ${r.limit_mode==='custom'?'selected':''}>Custom</option></select></label><div class="resource-limits">${['low','high'].map(side=>`<label>${side==='low'?'Low':'High'} ${e(r.unit)}<input type="number" step="any" data-resource-limit="${side}" value="${r[side]??''}" placeholder="Off" ${r.monitor&&r.warn&&r.limit_mode==='custom'?'':'disabled'}></label>`).join('')}</div></div></fieldset>`).join('')}${disks.map(d=>`<fieldset data-volume="${e(d.mount)}"><legend>Volume ${e(d.mount)}</legend>${['monitor','warn','card'].map((key,i)=>`<label class="check-line"><input type="checkbox" data-volume-option="${key}" ${d[key]?'checked':''}>${['Monitoring','Warning','Resource card'][i]}</label>`).join('')}<p class="hint">Warnings use this server’s disk limits.</p></fieldset>`).join('')}${!rows.length&&!disks.length?'<p class="hint">Resource controls become available after a successful check.</p>':''}<div class="form-error" role="alert"></div><div class="form-actions">${button('close','Cancel','','ghost')}<button type="submit" class="primary">Save</button></div></form>`);
  $('.modal-backdrop').classList.add('resource-popover-backdrop');const pop=$('.modal');pop.classList.add('resource-card-popover');
  pop.style.left=Math.max(10,Math.min(innerWidth-pop.offsetWidth-10,rect.right-pop.offsetWidth))+'px';pop.style.top=Math.max(10,Math.min(innerHeight-pop.offsetHeight-10,rect.bottom+8))+'px';
}
async function saveResourceCardForm(form){
  const resources=resourceChoicesFromForm(form),volumes=[...form.querySelectorAll('[data-volume]')].map(row=>({mount:row.dataset.volume,...Object.fromEntries(['monitor','warn','card'].map(key=>[key,row.querySelector('[data-volume-option='+key+']').checked]))}));
  const size=form.querySelector('[data-card-size-value]').value;
  await saveCardPreferences(form.dataset.server,{layout:{sizes:{[form.dataset.card]:size}},resources,volumes});markFormSaved(form);closeOverlay();toast('Resource card saved');
}
function clearResourceDrag(){
  const old=resourceDrag;resourceDrag=null;resourceGhost?.remove();resourceGhost=null;resourceDropSlot?.remove();resourceDropSlot=null;
  document.querySelectorAll('.resource-card-dragging').forEach(el=>el.classList.remove('resource-card-dragging'));
  if(old?.started){state.dragging=false;old.grid.style.minHeight=old.minHeight;}
  if(old?.handle.hasPointerCapture(old.pointer))old.handle.releasePointerCapture(old.pointer);
}
async function moveResourceCard(id,position,serverId){
  if(!position||current()?.id!==serverId)return;
  const grid=$('.resource-card-grid'),plan=resourceCardGrids.get(grid),old=plan?.slots.get(id);if(!old)return;
  const next={...old,...position},others=[...plan.slots.values()].filter(slot=>slot.id!==id);
  if(next.x===old.x&&next.row===old.row)return;
  if(!resourceSlotFree(next,others,plan.geometry))throw new Error('That space is occupied. Choose an empty slot.');
  const slots=[...others,next].sort((a,b)=>a.row-b.row||a.x-b.x);
  const positions=Object.fromEntries(slots.map(slot=>[slot.id,{x:slot.x/plan.geometry.width,row:slot.row}]));
  await saveCardPreferences(serverId,{layout:{positions,order:slots.map(slot=>slot.id)}});
}
function updateResourceDrop(event){
  const d=resourceDrag,plan=resourceCardGrids.get(d.grid),slot=plan.slots.get(d.id),rect=d.grid.getBoundingClientRect(),g=plan.geometry;
  const inside=event.clientX>=rect.left&&event.clientX<=rect.right&&event.clientY>=rect.top&&event.clientY<=rect.bottom;
  resourceDropSlot.hidden=!inside;d.position=null;if(!inside)return;
  const others=[...plan.slots.values()].filter(other=>other.id!==d.id);
  let x=Math.max(0,Math.min(g.width-slot.width,Math.round((event.clientX-rect.left-d.dx)/10)*10));
  const guides=[0,g.width-slot.width,...others.flatMap(other=>[other.x,other.x+other.width+g.gap,other.x-slot.width-g.gap])].filter(left=>left>=0&&left+slot.width<=g.width);
  const nearest=guides.sort((a,b)=>Math.abs(a-x)-Math.abs(b-x))[0];if(Math.abs(nearest-x)<=16)x=nearest;
  const row=Math.max(0,Math.round((event.clientY-rect.top-d.dy)/g.rowStep)),position={x,row};
  const valid=resourceSlotFree({...slot,...position},others,g);if(valid)d.position=position;
  resourceDropSlot.classList.toggle('invalid',!valid);resourceDropSlot.style.left=rect.left+x+'px';resourceDropSlot.style.top=rect.top+row*g.rowStep+'px';resourceDropSlot.style.width=slot.width+'px';resourceDropSlot.style.height=slot.span*g.rowStep-g.gap+'px';
}
document.addEventListener('pointerdown',event=>{
  const handle=event.target.closest('.resource-card-handle');if(!handle||!event.isPrimary||event.button!==0||!admin()||cardSaves.has(current()?.id))return;
  const card=handle.closest('[data-resource-card]'),rect=card.getBoundingClientRect(),grid=card.parentNode;event.preventDefault();handle.focus({preventScroll:true});handle.setPointerCapture(event.pointerId);
  resourceDrag={handle,card,grid,minHeight:grid.style.minHeight,id:card.dataset.resourceCard,server:current().id,pointer:event.pointerId,x:event.clientX,y:event.clientY,dx:event.clientX-rect.left,dy:event.clientY-rect.top,started:false,position:null};
});
document.addEventListener('pointermove',event=>{
  const d=resourceDrag;if(!d||event.pointerId!==d.pointer)return;
  if(!d.started&&Math.hypot(event.clientX-d.x,event.clientY-d.y)>6){
    d.started=true;state.dragging=true;++dashboardSequence;const rect=d.card.getBoundingClientRect();
    resourceGhost=d.card.cloneNode(true);resourceGhost.removeAttribute('data-resource-card');resourceGhost.removeAttribute('data-key');resourceGhost.querySelectorAll('[id]').forEach(el=>el.removeAttribute('id'));resourceGhost.classList.add('resource-card-ghost');resourceGhost.inert=true;resourceGhost.setAttribute('aria-hidden','true');resourceGhost.style.width=rect.width+'px';resourceGhost.style.height=rect.height+'px';document.body.append(resourceGhost);d.card.classList.add('resource-card-dragging');
    resourceDropSlot=document.createElement('div');resourceDropSlot.className='resource-card-slot';resourceDropSlot.setAttribute('aria-hidden','true');document.body.append(resourceDropSlot);d.grid.style.minHeight=d.grid.offsetHeight+196+'px';
  }
  if(!d.started)return;
  resourceGhost.style.left=Math.max(5,Math.min(innerWidth-resourceGhost.offsetWidth-5,event.clientX-d.dx))+'px';resourceGhost.style.top=Math.max(5,Math.min(innerHeight-resourceGhost.offsetHeight-5,event.clientY-d.dy))+'px';
  updateResourceDrop(event);
});
document.addEventListener('pointerup',async event=>{const d=resourceDrag;if(!d||event.pointerId!==d.pointer)return;clearResourceDrag();try{if(d.started)await moveResourceCard(d.id,d.position,d.server);}catch(error){toast(error.message,true);}finally{if(d.started)renderMain();}});
for(const name of ['pointercancel','lostpointercapture'])document.addEventListener(name,()=>{if(resourceDrag){clearResourceDrag();renderMain();}});
window.addEventListener('blur',()=>{if(resourceDrag){clearResourceDrag();renderMain();}});
document.addEventListener('keydown',async event=>{
  if(event.key==='Escape'&&resourceDrag){event.preventDefault();clearResourceDrag();renderMain();return;}
  const handle=event.target.closest('.resource-card-handle');if(!handle||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)||cardSaves.has(current()?.id))return;
  event.preventDefault();const card=handle.closest('[data-resource-card]'),plan=resourceCardGrids.get(card.parentNode),slot=plan.slots.get(card.dataset.resourceCard),position={x:slot.x,row:slot.row};
  if(event.key==='ArrowUp')position.row=Math.max(0,slot.row-1);
  if(event.key==='ArrowDown')position.row=Math.min(4096,slot.row+1);
  if(event.key==='ArrowLeft')position.x=Math.max(0,slot.x-slot.width-plan.geometry.gap);
  if(event.key==='ArrowRight')position.x=Math.min(plan.geometry.width-slot.width,slot.x+slot.width+plan.geometry.gap);
  try{await moveResourceCard(slot.id,position,current().id);handle.focus();}catch(error){toast(error.message,true);}
});
