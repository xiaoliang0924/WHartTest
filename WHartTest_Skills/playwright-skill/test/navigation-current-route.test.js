const assert = require('node:assert/strict');
const Module = require('node:module');

const originalLoad = Module._load;
Module._load = function mockPlaywright(request, parent, isMain) {
  if (request === 'playwright') return {};
  return originalLoad.call(this, request, parent, isMain);
};
const { navigateByMenu } = require('../lib/helpers');
Module._load = originalLoad;

function locatorStub(onClick) {
  let locator;
  locator = new Proxy({}, {
    get(_target, property) {
      if (property === 'count') return async () => 0;
      if (property === 'isVisible') return async () => false;
      if (property === 'click') return async () => onClick();
      return (..._args) => locator;
    },
  });
  return locator;
}

(async () => {
  const targetUrl = 'https://test.bot.by56.com/work-order/my-tickets';
  let clickCount = 0;
  let output = '';
  const locator = locatorStub(() => { clickCount += 1; });
  const page = {
    url: () => targetUrl,
    waitForLoadState: async () => {},
    waitForTimeout: async () => {},
    locator: () => locator,
    getByRole: () => locator,
    evaluate: async () => 100,
  };

  const originalLog = console.log;
  console.log = (line) => { output += String(line) + '\n'; };
  try {
    const resultUrl = await navigateByMenu(
      page,
      '工单中心',
      null,
      '/work-order/my-tickets',
    );
    assert.equal(resultUrl, targetUrl);
    assert.equal(clickCount, 0, 'should not click a parent menu on the target route');
    assert.match(output, /RESULT=PASS: 已进入菜单/);
  } finally {
    console.log = originalLog;
  }

  originalLog('already-at-target navigation smoke: PASS');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
