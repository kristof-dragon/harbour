'use strict';
const busyViewNodes=new WeakSet();
// Reconcile refreshed markup without replacing controls, scrollers or animations.
function viewKey(node){
  if(node.nodeType!==1)return null;
  for(const name of ['id','data-key','data-open','data-warning-open','data-card-chart']){
    if(node.hasAttribute(name))return name+':'+node.getAttribute(name);
  }
  if(node.hasAttribute('data-action'))return 'action:'+JSON.stringify(['action','id','kind','service','server','tab','mode','resource','filter','value','visual','field'].map(k=>node.getAttribute('data-'+k)));
  return null;
}
function sameViewNode(a,b){return a.nodeType===b.nodeType&&a.nodeName===b.nodeName&&viewKey(a)===viewKey(b);}
function patchViewChildren(parent,next){
  const old=[...parent.childNodes],used=new Set(),keyed=new Map(old.filter(n=>viewKey(n)).map(n=>[viewKey(n),n]));
  let cursor=parent.firstChild;
  for(const desired of [...next.childNodes]){
    const key=viewKey(desired);
    let node=key?keyed.get(key):old.find(n=>!used.has(n)&&!viewKey(n)&&sameViewNode(n,desired));
    if(node&&!sameViewNode(node,desired))node=null;
    if(!node){node=desired.cloneNode(true);parent.insertBefore(node,cursor);}
    else{
      used.add(node);
      if(node!==cursor)parent.insertBefore(node,cursor);
      patchViewNode(node,desired);
    }
    cursor=node.nextSibling;
  }
  for(const node of old)if(!used.has(node))node.remove();
}
function patchViewNode(node,next){
  if(node.nodeType!==1){if(node.nodeValue!==next.nodeValue)node.nodeValue=next.nodeValue;return;}
  // Open details and tooltip associations belong to the current interaction.
  const local=name=>(name==='open'&&node.tagName==='DETAILS')||name==='aria-describedby'||(name==='style'&&node.matches('.resource-card-grid,[data-resource-card]'))||(name==='disabled'&&busyViewNodes.has(node));
  for(const attr of [...node.attributes])if(!local(attr.name)&&!next.hasAttribute(attr.name))node.removeAttribute(attr.name);
  for(const attr of next.attributes)if(!local(attr.name)&&node.getAttribute(attr.name)!==attr.value)node.setAttribute(attr.name,attr.value);
  // Mini charts are updated independently after their history request completes.
  if(!node.hasAttribute('data-preserve-children'))patchViewChildren(node,next);
  if(node.tagName==='INPUT'){
    if(node.type==='checkbox'||node.type==='radio'){if(node.checked!==next.checked)node.checked=next.checked;}
    else if(node.value!==next.value)node.value=next.value;
  }
  if(node.tagName==='SELECT'&&node.value!==next.value)node.value=next.value;
}
function updateHTML(root,markup){
  if(!root)return;
  const template=document.createElement('template');template.innerHTML=markup;
  patchViewChildren(root,template.content);
}
function updateText(node,text){if(node&&node.textContent!==text)node.textContent=text;}
