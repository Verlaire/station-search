// Exercise the actual view's selection lifecycle without user histories or a live app.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function element() {
  return new Proxy({}, {
    get(_target, key) {
      if (key === 'when') return (condition, build) => condition ? build(element()) : element();
      return () => element();
    },
  });
}

const context = {
  View: class {}, div: element, svg: element, image: element, v_flex: element, h_flex: element,
  Input: { new: element },
  InputState: { new: () => ({ on() {}, focus() {}, release() {} }) },
  shared: () => 'en', theme: () => ({}),
};
vm.createContext(context);
const source = fs.readFileSync(path.join(__dirname, '../mods/station-search/main.js'), 'utf8')
  .replace(/^import .*;\r?\n/gm, '')
  .replace('export default class Search', 'globalThis.Search = class Search');
vm.runInContext(source, context);
const view = new context.Search();
const cx = {
  timer: { after: () => ({ cancel() {} }) }, spawn: () => ({ cancel() {} }),
  focus_handle: () => ({ focus() {}, is_focused: () => true }), notify() {}, stop_propagation() {},
};
view.init({}, cx);
view.results = Array.from({ length: 25 }, (_, i) => ({ agent: 'claude', title: `Result ${i}` }));
view.resultHeights = view.results.map((_, i) => [72, 85, 78][i % 3]);
view.resultsViewH = 460;
view.resultsBodyH = view.resultHeights.reduce((a, b) => a + b, 0) + 2 * (view.results.length - 1);
function assertSelectedVisible() {
  const top = view.resultHeights.slice(0, view.selected).reduce((sum, height) => sum + height + 2 * view.resultsScale, 0);
  assert.ok(top + 0.0001 >= view.resultsY, 'selected row must not be above the viewport');
  assert.ok(top + view.resultHeights[view.selected] <= view.resultsY + view.resultsViewH + 0.0001,
    'selected row must not be below the viewport');
}

for (let i = 0; i < 20; i++) view.key({ key: 'down' }, cx);
view.render(cx);
assert.equal(view.selected, 20);
assertSelectedVisible();

view.scrollResults(-150, cx);
const wheelPosition = view.resultsY;
view.render(cx);
view.render(cx);
assert.equal(view.resultsY, wheelPosition, 'ordinary redraws must not undo wheel scrolling');

view.key({ key: 'up' }, cx);
view.render(cx);
assertSelectedVisible();

view.show(true, cx);
const beforePreview = view.resultsY;
view.selectResult(24);
view.render(cx);
assert.equal(view.resultsY, beforePreview, 'do not move the hidden results list from the preview');
view.show(false, cx);
view.render(cx);
assertSelectedVisible();

view.scrollResults(99999, cx);
assert.equal(view.resultsY, view.resultsBodyH - view.resultsViewH, 'wheel clamps at the bottom');
view.scrollResults(-99999, cx);
assert.equal(view.resultsY, 0, 'wheel clamps at the top');

view.selectResult(999);
view.render(cx);
assert.equal(view.selected, 24);
view.selectResult(-1);
view.render(cx);
assert.equal(view.selected, 0);
assertSelectedVisible();
view.selectResult(NaN);
assert.equal(view.selected, 0);
// A new result set starts at the first item, even after scrolling the previous list.
view.resultsY = 500;
view.selectResult(0);
view.render(cx);
assert.equal(view.resultsY, 0);
// Layout callbacks report zoomed dimensions; row gaps and builder offsets must agree.
view.resultsScale = 1.1;
view.resultHeights = view.results.map(() => 84);
view.resultsViewH = 506;
view.resultsBodyH = view.resultHeights.reduce((sum, h) => sum + h, 0) + 2 * 1.1 * 24;
view.selectResult(24);
view.render(cx);
assertSelectedVisible();
assert.equal(view.resultsY, view.resultsBodyH - view.resultsViewH, 'last row ends at viewport bottom');
assert.ok(Math.abs((view.resultsY / view.resultsScale) * 1.1 - view.resultsY) < 0.001,
  'rendering the offset applies zoom exactly once');
console.log('Results scrolling: keyboard reveal, wheel independence, preview return and bounds passed.');
