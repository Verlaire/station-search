const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = { View: class {} };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../mods/station-search/main.js'), 'utf8')
  .replace(/^import .*;\r?\n/gm, '')
  .replace('export default class Search', 'globalThis.Search = class Search'), context);
const view = new context.Search();
const filters = [['', 'All'], ['/claude', 'Claude'], ['/antigravity', 'Antigravity'], ['/pi', 'Pi'], ['/open', 'Open']];
view.filter = '';
for (const [query, expected] of [
  ['/claude herdr', '/claude'], ['herdr /CLAUDE', '/claude'], ['/agy bug', '/antigravity'],
  ['/claude-code bug', '/claude'], ['/pi-coding-agent notes', '/pi'], ['/open logs', '/open'],
  ['/claude /open logs', '/claude'], ['/clau logs', ''], ['ordinary text', ''],
  ['/claude /antigravity logs', ''], ['/not-in-catalog logs', ''],
]) {
  view.text = query;
  assert.equal(view.activeFilter(filters), expected, query);
}
view.filter = '/claude';
view.text = 'logs';
assert.equal(view.activeFilter(filters), '/claude', 'clicked filters remain reflected');
view.text = '/antigravity logs';
assert.equal(view.activeFilter(filters), '', 'do not misrepresent a multi-agent search as a single agent');

view.resultsScale = 1.1;
view.filterWidth = 220; // 200 builder units, matching the app's zoomed size callbacks.
view.filterSizes = { '': {width: 55, height: 22}, '/claude': {width: 88, height: 22},
  '/antigravity': {width: 121, height: 22}, '/pi': {width: 44, height: 22}, '/open': {width: 66, height: 22} };
let pill = view.filterIndicator(filters, '/antigravity');
assert.equal(pill.left, 0, 'a wrapped chip begins a new row');
assert.equal(pill.top, 24, 'wrap uses the measured row height plus its gap');
assert.ok(Math.abs(pill.width - 110) < 0.001, 'pill width must apply zoom once');
pill = view.filterIndicator(filters, '/claude');
assert.ok(Math.abs(pill.left - 54) < 0.001);
assert.equal(pill.top, 0);
delete view.filterSizes[''];
assert.equal(view.filterIndicator(filters, '/claude'), null, 'wait for measurements before positioning');
console.log('Filter chips: typed/clicked filters, aliases, combinations, wrapping and zoom passed.');
