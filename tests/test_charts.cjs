// Exercise real chart output with missed polls, long outages and aggregation.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const context={localStorage:{getItem:()=>null},document:{addEventListener:()=>{}},start:()=>{},setInterval:()=>{},current:()=>({poll_seconds:60}),e:String,percent:n=>n.toFixed(1)};
vm.createContext(context);vm.runInContext(fs.readFileSync('harbour/static/monitoring.js','utf8'),context);
const point=(time,cpu)=>({time,cpu,sample_first:time,sample_last:time});
const data=points=>({points,from:0,to:600,hours:1,resolution_seconds:60,poll_seconds:60});
function svg(points){context.data=data(points);return vm.runInContext('chartSVG(data,["cpu"],{mini:true})',context);}
let image=svg([point(0,20),point(60,null),point(120,null),point(180,30)]);
assert.equal((image.match(/<polyline /g)||[]).length,1,'Two missed polls should stay connected');
image=svg([point(0,20),point(60,null),point(120,null),point(180,null),point(240,30)]);
assert.equal((image.match(/<circle /g)||[]).length,2,'Three missed polls leave a gap');
image=svg([point(0,20),point(180,30)]);
assert.equal((image.match(/<polyline /g)||[]).length,1,'Absent buckets also bridge two polls');
image=svg([{...point(0,20),sample_last:58},{...point(120,30),sample_first:121}]);
assert.equal((image.match(/<polyline /g)||[]).length,1,'Bucket boundaries must not create artificial gaps');
image=svg([point(0,null),point(60,null)]);
assert.match(image,/No successful readings/);
console.log('PASS: chart continuity across two missed polls; longer gaps and missing readings preserved');
// Load shares one count axis across all three periods, including values above 100.
context.data=data([point(0,20),{...point(60,20),load1:0,load5:.15,load15:110},
 { ...point(120,20),load1:4,load5:1.5,load15:120}]);
image=vm.runInContext('chartSVG(data,["load"])',context);
assert.equal((image.match(/<polyline /g)||[]).length,3);
assert.match(image,/data-chart-keys="load1,load5,load15"/);
assert.match(image,/Load · 1 min, Load · 5 min, Load · 15 min/);
assert.doesNotMatch(image,/%|NaN|undefined/);
for(const match of image.matchAll(/points="([^"]+)"/g)){
 for(const pair of match[1].split(' ')){
  const y=Number(pair.split(',')[1]);assert.ok(y>=30&&y<=260,'Load must fit its own scale');
 }
}
context.data=data([point(0,20),point(60,30)]);
assert.match(vm.runInContext('chartSVG(data,["load"])',context),/No load readings/);
context.data=data([{...point(0,20),load1:0,load5:0,load15:0}]);
image=vm.runInContext('chartSVG(data,["load"],{mini:true})',context);
assert.equal((image.match(/<circle /g)||[]).length,3);
assert.doesNotMatch(image,/NaN/);
console.log('PASS: three load series, independent count scale, zero and legacy missing readings');
