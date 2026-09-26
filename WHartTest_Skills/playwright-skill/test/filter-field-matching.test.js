const assert = require('node:assert/strict');
const Module = require('node:module');

const originalLoad = Module._load;
Module._load = function mockPlaywright(request, parent, isMain) {
  if (request === 'playwright') return {};
  return originalLoad.call(this, request, parent, isMain);
};
const { normalizeFilterFieldLabel, filterInputLabelMatches } = require('../lib/helpers');
Module._load = originalLoad;

assert.equal(normalizeFilterFieldLabel('工单编号'), '工单号');
assert.equal(normalizeFilterFieldLabel('搜索工单号…'), '工单号');
assert.equal(filterInputLabelMatches('工单编号', '搜索工单号...'), true);
assert.equal(filterInputLabelMatches('工单号', '精准搜索工单号'), true);
assert.equal(filterInputLabelMatches('工单编号', '搜索处理人'), false);

console.log('filter field label normalization checks passed');
