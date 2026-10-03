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
