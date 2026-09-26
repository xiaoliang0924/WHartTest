const assert = require('node:assert/strict');
const Module = require('node:module');

const originalLoad = Module._load;
Module._load = function mockPlaywright(request, parent, isMain) {
  if (request === 'playwright') return {};
  return originalLoad.call(this, request, parent, isMain);
};
const { hasVisibleExactText, waitForVisibleExactText } = require('../lib/helpers');
Module._load = originalLoad;

(async () => {
  let calls = 0;
  const page = {
    getByText(text, options) {
      assert.equal(text, '已解决');
      assert.deepEqual(options, { exact: true });
      return {
        async all() {
          calls += 1;
          return [
            { isVisible: async () => false },
            { isVisible: async () => calls >= 2 },
          ];
        },
      };
    },
    async waitForTimeout() {},
  };
  assert.equal(await hasVisibleExactText(page, '已解决'), false);
  assert.equal(await waitForVisibleExactText(page, '已解决', 100), true);
  assert.equal(calls, 2);
  const hiddenOnly = {
    getByText: () => ({ all: async () => [{ isVisible: async () => false }] }),
    async waitForTimeout() {},
  };
  assert.equal(await waitForVisibleExactText(hiddenOnly, '已解决', 0), false);
  console.log('completion status visibility checks passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
