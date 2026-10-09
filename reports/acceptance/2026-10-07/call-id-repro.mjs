import fs from 'node:fs';
import assert from 'node:assert/strict';
import {hydrateTurns} from '../../../apps/web/.test-build/session.js';
const snapshot = JSON.parse(fs.readFileSync(new URL('./browser-completed-restart.json', import.meta.url),'utf8'));
const turns = hydrateTurns(snapshot).filter(t=>t.role==='assistant');
const observed = turns.map(t=>Object.values(t.tools).map(e=>({id:e.id,status:e.status,content:e.finished?.content})));
console.log(JSON.stringify(observed,null,2));
assert.equal(turns[0].tools['demo-1'].status,'succeeded','first approved turn must retain success after later denial');
assert.equal(turns[1].tools['demo-1'].status,'denied');
