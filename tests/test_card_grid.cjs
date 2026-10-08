const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const context={document:{addEventListener(){}},window:{addEventListener(){}}};
vm.createContext(context);vm.runInContext(fs.readFileSync('harbour/static/cards.js','utf8'),context);
const items=[{id:'cpu',size:'medium'},{id:'fan',size:'small'},{id:'power',size:'small'}];
const positions=plan=>Object.fromEntries([...plan.slots].map(([id,slot])=>[id,{x:slot.x/plan.geometry.width,row:slot.row}]));
function check(plan){
 const slots=[...plan.slots.values()];
 for(const slot of slots)assert.ok(context.resourceSlotFree(slot,slots.filter(other=>other!==slot),plan.geometry),'Cards fit without overlapping: '+slot.id);
}
const initial=context.planResourceCards(items,1060);check(initial);
assert.equal(initial.slots.get('fan').row,initial.slots.get('power').row);
assert.notEqual(initial.slots.get('fan').x,initial.slots.get('power').x);
const saved=positions(initial);saved.power={x:saved.fan.x,row:saved.fan.row+1};
const stacked=context.planResourceCards(items,1060,saved);check(stacked);
assert.equal(stacked.slots.get('power').x,stacked.slots.get('fan').x);
assert.equal(stacked.slots.get('power').row,stacked.slots.get('fan').row+1);
saved.power={x:.7,row:3};
const independent=context.planResourceCards(items,1060,saved);check(independent);
assert.deepEqual(independent.slots.get('fan'),initial.slots.get('fan'));
assert.equal(independent.slots.get('power').row,3);
// Removing another card or adding a discovery does not fill intentional gaps.
const hidden=context.planResourceCards(items.slice(1),1060,saved);check(hidden);
assert.deepEqual(hidden.slots.get('power'),independent.slots.get('power'));
const discovery=context.planResourceCards([...items,{id:'new',size:'small'}],1060,saved);check(discovery);
assert.deepEqual(discovery.slots.get('power'),independent.slots.get('power'));
for(const width of [240,390,720,1060,1900]){
 for(const size of ['small','medium','large']){
  const changed=items.map(item=>({...item,size}));check(context.planResourceCards(changed,width,saved));
 }
}
assert.deepEqual(context.planResourceCards(items,1060,saved),independent);
// Colliding saved positions at the last allowed row must never overlap.
check(context.planResourceCards(items,390,Object.fromEntries(items.map(item=>[item.id,{x:0,row:4096}]))));
console.log('PASS: independent slots, optional stacking, intentional gaps, hide/discover stability, resize/size collision handling, bounded saved positions');
