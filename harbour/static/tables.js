'use strict';
const tableStates=new Map(),tableSources=new Map(),tableRefreshers=new Map(),tableTimers=new Map();
function tableState(id){
  if(!tableStates.has(id))tableStates.set(id,{page:1,size:25,sort:'',order:'asc',filters:{}});
  return tableStates.get(id);
}
function tableFilterValue(value){return typeof value==='string'?value:'';}
function tableQuery(id){
  const t=tableState(id),filters={};
  for(const [key,value] of Object.entries(t.filters)){
    if(!value)continue;
    if(t.dates?.includes(key)){
      const utc=t.utcDates?.includes(key),start=new Date(value+'T00:00:00'+(utc?'Z':'')),end=new Date(start);if(utc)end.setUTCDate(end.getUTCDate()+1);else end.setDate(end.getDate()+1);
      filters[key]=[start.getTime()/1000,end.getTime()/1000];
    }else filters[key]=value;
  }
  return {page:t.page,size:t.size,sort:t.sort,order:t.order,filters:JSON.stringify(filters)};
}
function tableColumn(id,column,tag='th'){
  const t=tableState(id),selected=t.sort===column.key,direction=selected?t.order==='asc'?'ascending':'descending':'none';
  if(column.action)return `<${tag}>${e(column.label)}</${tag}>`;
  return `<${tag} class="data-column" role="columnheader" aria-sort="${direction}" data-key="column:${e(column.key)}"><button type="button" data-table-sort="${e(column.key)}" title="Sort by ${e(column.label)}">${e(column.label)} <span aria-hidden="true">${selected?t.order==='asc'?'↑':'↓':'↕'}</span></button><input data-table-filter="${e(column.key)}" type="${column.type==='date'?'date':'search'}" value="${e(tableFilterValue(t.filters[column.key]))}" aria-label="Filter ${e(column.label)}" placeholder="${column.type==='number'?'e.g. >=10':'Search…'}" title="${column.type==='number'?'Number, or comparison such as >=10':'Filter '+e(column.label)}" maxlength="128"></${tag}>`;
}
function tableControls(id,total){
  const t=tableState(id),pages=Math.max(1,Math.ceil(total/t.size));
  t.page=Math.max(1,Math.min(t.page,pages));
  return `<div class="data-table-controls"><label>Rows <select data-table-size aria-label="Rows per page">${[25,50,100].map(n=>`<option ${n===t.size?'selected':''}>${n}</option>`).join('')}</select></label><span class="hint" role="status">${total?(t.page-1)*t.size+1:0}–${Math.min(t.page*t.size,total)} of ${total}</span><div class="data-table-pages"><button type="button" class="small" data-table-page="${t.page-1}" ${t.page<=1?'disabled':''}>Previous</button><label>Page <input type="number" min="1" max="${pages}" value="${t.page}" data-table-jump aria-label="Page number"> of ${pages}</label><button type="button" class="small" data-table-page="${t.page+1}" ${t.page>=pages?'disabled':''}>Next</button></div>${Object.values(t.filters).some(Boolean)?'<button type="button" class="small" data-table-clear>Clear filters</button>':''}</div>`;
}
function tableValue(cell,type){
  if(cell.hasAttribute('data-value')){
    const value=cell.getAttribute('data-value');return value===''?null:Number.isFinite(Number(value))?Number(value):value;
  }
  const text=cell.textContent.trim();
  if(!text||/^(—|Unavailable|Unknown|Not run yet)$/.test(text))return null;
  // Compound metrics sort by their leading measure; explicit raw values take precedence.
  const m=text.match(/^(-?\d[\d,]*(?:\.\d+)?)(?:\s*(ms|µs|s|KiB|MiB|GiB|KB|MB|GB|%|°C|dBm|Mbps|kbps))?(?:\s*(?:\/|·)|$)/);
  if(m){const units={µs:.001,ms:1,s:1000,KiB:1024,MiB:1048576,GiB:1073741824,KB:1000,MB:1e6,GB:1e9,kbps:1,Mbps:1000};return Number(m[1].replaceAll(',',''))*(units[m[2]]||1);}
  return text;
}
function tableMatch(cell,column,filter){
  if(!filter)return true;
  if(column.type==='date'){
    const value=tableValue(cell,'date'),start=new Date(filter+'T00:00:00'+(column.utc?'Z':'')),end=new Date(start);if(column.utc)end.setUTCDate(end.getUTCDate()+1);else end.setDate(end.getDate()+1);
    return value!=null&&value>=start.getTime()/1000&&value<end.getTime()/1000;
  }
  const comparison=filter.match(/^\s*(<=|>=|<|>|=)\s*(-?\d+(?:\.\d+)?)\s*$/);
  if(comparison){const value=tableValue(cell,column.type),n=Number(comparison[2]);return typeof value==='number'&&({'<':value<n,'>':value>n,'<=':value<=n,'>=':value>=n,'=':value===n})[comparison[1]];}
  return cell.textContent.toLocaleLowerCase().includes(filter.toLocaleLowerCase());
}
function renderLocalTable(id,original){
  const table=original.cloneNode(true),t=tableState(id),headers=[...table.tHead.rows[0].cells];
  const columns=headers.map((h,i)=>({key:String(i),label:h.textContent,type:h.dataset.type||'text',utc:h.textContent.includes('(UTC)'),action:h.dataset.sort==='none'||/^(Controls|History|Review|Report|Results|File|Install artifact|Evidence)$/.test(h.textContent)}));
  t.dates=columns.filter(c=>c.type==='date').map(c=>c.key);
  const body=table.tBodies[0];if(!body)return table;
  const empty=body.rows.length===1&&body.rows[0].cells.length===1&&body.rows[0].cells[0].colSpan>1?body.rows[0].cloneNode(true):null;
  let rows=[...body.rows].filter(r=>r.cells.length===columns.length);
  rows=rows.filter(r=>columns.every((c,i)=>tableMatch(r.cells[i],c,t.filters[c.key])));
  const selected=columns.find(c=>c.key===t.sort);
  if(selected){const index=columns.indexOf(selected),collator=new Intl.Collator(undefined,{numeric:true,sensitivity:'base'});rows=rows.map((r,i)=>({r,i})).sort((a,b)=>{
    const av=tableValue(a.r.cells[index],selected.type),bv=tableValue(b.r.cells[index],selected.type);
    if(av==null||bv==null)return av==null?(bv==null?a.i-b.i:1):-1;
    const cmp=typeof av==='number'&&typeof bv==='number'?av-bv:collator.compare(String(av),String(bv));
    return (t.order==='desc'?-cmp:cmp)||a.i-b.i;
  }).map(x=>x.r);}
  const controls=tableControls(id,rows.length);
  body.replaceChildren(...rows.slice((t.page-1)*t.size,t.page*t.size));
  if(!rows.length&&empty&&!Object.values(t.filters).some(Boolean))body.append(empty);
  else if(!rows.length){const row=body.insertRow();const cell=row.insertCell();cell.colSpan=columns.length;cell.textContent='No matching records.';}
  table.tHead.rows[0].innerHTML=columns.map(c=>tableColumn(id,c)).join('');
  const shell=document.createElement('div');shell.className='data-table-shell';shell.dataset.tableKey=id;shell.dataset.key='table:'+id;
  shell.innerHTML='<div class="data-table-scroll"></div>'+controls;
  shell.firstChild.append(table);return shell;
}
function prepareDataTables(root){
  for(const table of root.querySelectorAll('table[data-table]')){
    if(table.closest('[data-table-key]'))continue;
    const id=(typeof current==='function'?current()?.id||'global':'global')+':'+table.dataset.table;
    tableSources.set(id,table.cloneNode(true));table.replaceWith(renderLocalTable(id,table));
  }
}
async function refreshDataTable(id){
  if(tableRefreshers.has(id)){
    const active=document.activeElement,filter=active?.dataset.tableFilter,start=active?.selectionStart,end=active?.selectionEnd;
    try{await tableRefreshers.get(id)();}catch(error){if(typeof toast==='function')toast(error.message,true);}
    if(filter!=null&&!active.isConnected){for(const shell of document.querySelectorAll('[data-table-key]'))if(shell.dataset.tableKey===id){const input=[...shell.querySelectorAll('[data-table-filter]')].find(n=>n.dataset.tableFilter===filter);if(input){input.focus();if(start!=null)input.setSelectionRange(start,end);}}}
    return;
  }
  const source=tableSources.get(id);if(!source)return;
  for(const shell of document.querySelectorAll('[data-table-key]'))if(shell.dataset.tableKey===id)patchViewNode(shell,renderLocalTable(id,source));
}
document.addEventListener('click',event=>{
  const node=event.target.closest('[data-table-sort],[data-table-page],[data-table-clear]'),shell=node?.closest('[data-table-key]');if(!shell)return;
  const id=shell.dataset.tableKey,t=tableState(id);
  if(node.hasAttribute('data-table-sort')){const key=node.dataset.tableSort;t.order=t.sort===key&&t.order==='asc'?'desc':'asc';t.sort=key;t.page=1;}
  if(node.hasAttribute('data-table-page'))t.page=Number(node.dataset.tablePage);
  if(node.hasAttribute('data-table-clear')){t.filters={};t.page=1;}
  clearTimeout(tableTimers.get(id));refreshDataTable(id);
});
document.addEventListener('input',event=>{
  const node=event.target,shell=node.closest('[data-table-key]');if(!shell||!node.hasAttribute('data-table-filter'))return;
  const id=shell.dataset.tableKey,t=tableState(id);t.filters[node.dataset.tableFilter]=node.value;t.page=1;
  clearTimeout(tableTimers.get(id));tableTimers.set(id,setTimeout(()=>refreshDataTable(id),350));
});
document.addEventListener('change',event=>{
  const node=event.target,shell=node.closest('[data-table-key]');if(!shell)return;
  const id=shell.dataset.tableKey,t=tableState(id);
  if(node.hasAttribute('data-table-size')){t.size=Number(node.value);t.page=1;}
  else if(node.hasAttribute('data-table-jump'))t.page=Math.max(1,Math.min(Number(node.max),Number(node.value)||1));
  else return;
  clearTimeout(tableTimers.get(id));refreshDataTable(id);
});
function infoMenu(id,label,content){return `<details class="info-menu" data-key="info:${e(id)}"><summary aria-label="${e(label)}" title="${e(label)}">${icon('info')}</summary><div class="info-menu-content"><b>${e(label)}</b>${content}</div></details>`;}
document.addEventListener('toggle',event=>{
  const menu=event.target;if(!menu.matches?.('.info-menu')||!menu.open)return;
  for(const other of document.querySelectorAll('.info-menu[open]'))if(other!==menu)other.open=false;
  const panel=menu.querySelector('.info-menu-content'),r=menu.querySelector('summary').getBoundingClientRect();
  panel.style.left=Math.max(12,Math.min(r.left,innerWidth-panel.offsetWidth-12))+'px';
  panel.style.top=Math.max(12,Math.min(r.bottom+6,innerHeight-panel.offsetHeight-12))+'px';
},true);
document.addEventListener('click',event=>document.querySelectorAll('.info-menu[open]').forEach(menu=>{if(!menu.contains(event.target))menu.open=false;}));
document.addEventListener('keydown',event=>{if(event.key==='Escape')document.querySelectorAll('.info-menu[open]').forEach(menu=>menu.open=false);});
