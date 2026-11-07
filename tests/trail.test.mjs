import test from 'node:test';
import assert from 'node:assert/strict';
import { createTrail, safeEntry, MAX_TRAIL } from '../site/trail.js';
const site=(id)=>({id:String(id),repo:'demo/game'+id,repoUrl:'https://github.com/demo/game'+id,url:'https://demo.github.io/game'+id+'/',name:'Game '+id});
const storage=()=>{let value=null;return {getItem:()=>value,setItem:(_,v)=>{value=v}}};
test('visit, previous, forward and branch reset',()=>{
  const trail=createTrail(storage());
  trail.visit(site(1));trail.visit(site(2));trail.visit(site(3));
  assert.equal(trail.cursor,2);assert.equal(trail.goto(1).id,'2');
  assert.equal(trail.canForward,true);
  trail.visit(site(4));assert.deepEqual(trail.entries.map(x=>x.id),['1','2','4']);
  assert.equal(trail.canForward,false);
});
test('history survives refresh and remains bounded',()=>{
 const s=storage(); const trail=createTrail(s);
 for(let i=0;i<130;i++)trail.visit(site(i));
 assert.equal(trail.entries.length,MAX_TRAIL);
 const restored=createTrail(s);assert.equal(restored.current.id,'129');
 assert.equal(restored.entries[0].id,'30');
});
test('invalid persisted URLs are dropped and storage can be unavailable',()=>{
 assert.equal(safeEntry({...site(1),url:'https://evil.example/'}),null);
 assert.equal(safeEntry({...site(1),repoUrl:'https://github.com/other/repo'}),null);
 const trail=createTrail({getItem(){throw Error('no storage')},setItem(){throw Error('no storage')}});
 trail.visit(site(1));assert.equal(trail.current.id,'1');
});
test('revisiting an already selected site does not duplicate the trail',()=>{
 const trail=createTrail(storage());trail.visit(site(1));trail.visit(site(1));
 assert.equal(trail.entries.length,1);assert.equal(trail.goto(-1),null);
});
