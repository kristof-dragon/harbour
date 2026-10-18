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

// Adjacency follows card edges, including stacked columns and mixed widths.
const mixed=[{id:'cpu',size:'small'},{id:'memory',size:'small'},{id:'load',size:'large'},{id:'sensor',size:'small'}];
const start=context.planResourceCards(mixed,1060);
const relative=context.relativeResourcePositions([...start.slots.values()],start.geometry);
relative.memory={...relative.memory,anchor:'cpu',edge:'left',offset:0,row:1};
relative.load={...relative.load,anchor:'cpu',edge:'right',offset:10,row:0};
relative.sensor={...relative.sensor,anchor:'load',edge:'right',offset:10,row:0};
for(const width of [1060,1500,1900,3000]){
 const plan=context.planResourceCards(mixed,width,relative);check(plan);
 const {cpu,memory,load,sensor}=Object.fromEntries(plan.slots);
 assert.equal(memory.x,cpu.x);assert.equal(memory.row,cpu.row+1);
 assert.equal(load.x-cpu.x-cpu.width,10);
 assert.equal(sensor.x-load.x-load.width,10);
}
check(context.planResourceCards(mixed,340,relative));
const restored=context.planResourceCards(mixed,1060,relative);
assert.equal(restored.slots.get('sensor').x-restored.slots.get('load').x-restored.slots.get('load').width,10);
const hiddenAnchor=context.planResourceCards(mixed.filter(item=>item.id!=='load'),1900,relative,{sizes:{load:'large'}});
assert.equal(hiddenAnchor.slots.get('sensor').x,context.planResourceCards(mixed,1900,relative).slots.get('sensor').x);
// Recover the original legacy grid from exact pixel fractions and tight edges.
const old=context.planResourceCards(mixed,1060),oldPositions=positions(old);
const migrated=context.planResourceCards(mixed,1900,oldPositions);check(migrated);
for(let i=1;i<mixed.length;i++){
 const a=migrated.slots.get(mixed[i-1].id),b=migrated.slots.get(mixed[i].id);
 if(a.row===b.row)assert.equal(b.x-a.x-a.width,10);
}
console.log('PASS: relative neighbours across growing viewports, stacked alignment, mixed widths, hidden anchors, narrow reflow and legacy spacing recovery');
