const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('app.js', 'utf8');
const boundary = source.indexOf("$('#search').addEventListener");
assert.ok(boundary > 0, 'Could not isolate app initialization');
const context = {Intl};
vm.runInNewContext(`${source.slice(0, boundary)}\nglobalThis.hooks={state,matches,pickerMatches,filtered,sortedPlasmids,dragExportDescriptor};`, context);
const {state, matches, pickerMatches, filtered, sortedPlasmids, dragExportDescriptor} = context.hooks;

state.synonymClusters = [{id: 1, terms: ['ITPR1', 'IP3R1']}];
state.plasmids = [
  {id: 1, name: 'ITPR1 construct.dna', tags: [], note: '', favorite: true, lastViewedAt: '2026-09-28T08:00:00', groupIds: [], importedAt: '2026-09-27', size: 2},
  {id: 2, name: 'IP3R1 construct.dna', tags: [], note: '', favorite: false, lastViewedAt: '2026-09-28T09:00:00', groupIds: [], importedAt: '2026-09-27', size: 1},
  {id: 3, name: 'Other.dna', tags: ['Feature-X'], note: '待测序 ITPR1 control', favorite: false, lastViewedAt: null, groupIds: [], importedAt: '2026-09-27', size: 3},
];

state.searchDetails = true;
state.search = 'ITPR1';
assert.deepEqual(Array.from(filtered(), p => p.id), [1, 2, 3]);
state.searchDetails = false;
assert.deepEqual(Array.from(filtered(), p => p.id), [1, 2]);
assert.equal(matches(state.plasmids[2], 'Feature-X'), false);
state.searchDetails = true;
assert.equal(matches(state.plasmids[2], 'Feature-X'), true);
assert.equal(pickerMatches(state.plasmids[2], 'Feature-X', true), true);
assert.equal(pickerMatches(state.plasmids[2], 'Feature-X', false), false);
assert.equal(pickerMatches(state.plasmids[2], 'control', true), true);
assert.equal(pickerMatches(state.plasmids[2], 'control', false), false);
assert.equal(pickerMatches(state.plasmids[1], 'ITPR1', false), true);
state.view = 'favorites';
state.search = '';
assert.deepEqual(Array.from(filtered(), p => p.id), [1]);
state.view = 'recent';
assert.deepEqual(Array.from(sortedPlasmids(filtered()), p => p.id), [2, 1]);
assert.equal(dragExportDescriptor({id: 7, name: 'JH:sample.dna'}, 'http://127.0.0.1:1234'), 'application/octet-stream:JH_sample.dna:http://127.0.0.1:1234/api/file/7');
console.log('Frontend search scopes and collections: OK');
