const assert = require('node:assert/strict');
const Module = require('node:module');
const originalLoad = Module._load;
Module._load = function mockPlaywright(request, parent, isMain) {
  if (request === 'playwright') return {};
  return originalLoad.call(this, request, parent, isMain);
};
const { assertTableColumnEquals } = require('../lib/helpers');
Module._load = originalLoad;

function collection(items) {
  return {
    count: async () => items.length,
    nth: (index) => items[index],
    allTextContents: async () => items.map((item) => item.text),
  };
}
function cell(text) {
  return { innerText: async () => text, isVisible: async () => true };
}
function tableStub(headers, rowCells) {
  const rows = rowCells.map((cells) => ({
    isVisible: async () => true,
    locator: (selector) => selector === 'td' ? collection(cells.map(cell)) : collection([]),
  }));
  return {
    isVisible: async () => true,
    locator(selector) {
      if (selector.includes('header-wrapper')) return collection(headers.map((text) => ({ text })));
      if (selector.includes('body-wrapper')) return collection(rows);
      return collection([]);
    },
  };
}
function pageStub(tables) {
  return { locator: (selector) => selector === '.el-table:visible' ? collection(tables) : collection([]) };
}

(async () => {
  let output = '';
  const originalLog = console.log;
  console.log = (message) => { output += `${message}\n`; };
  try {
    const result = await assertTableColumnEquals(
      pageStub([tableStub(['选择', '工单号', '触发节点'], [['', '123', 'TYPE_A']])]),
      '优先级',
      '高',
    );
    assert.equal(result.status, 'unverifiable');
    assert.match(output, /当前可见表格没有「优先级」列/);

    output = '';
    const mismatch = await assertTableColumnEquals(
      pageStub([tableStub(['选择', '工单号', '优先级'], [['', '123', '中']])]),
      '优先级',
      '高',
    );
    assert.equal(mismatch.status, 'fail');
    assert.match(output, /第1行=中/);

    output = '';
    const pass = await assertTableColumnEquals(
      pageStub([tableStub(['选择', '工单号', '优先级'], [['', '123', '高'], ['', '456', '高']])]),
      '优先级',
      '高',
    );
    assert.equal(pass.status, 'pass');
    assert.match(output, /已核对 2 行/);
  } finally {
    console.log = originalLog;
  }
  originalLog('table column assertion checks passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
