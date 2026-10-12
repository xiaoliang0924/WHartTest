'use strict';

function parseExpectedCount(value) {
  const expected = Number(value);
  if (!Number.isInteger(expected) || expected < 0) {
    throw new Error(`数量断言的期望值必须是非负整数，当前值=${String(value)}`);
  }
  return expected;
}

async function assertLocatorCount(locator, expectedValue, options = {}) {
  const expected = parseExpectedCount(expectedValue);
  const timeoutMs = Math.max(1, Number(options.timeoutMs) || 10000);
  const pollIntervalMs = Math.max(1, Number(options.pollIntervalMs) || 100);
  const wait = options.wait || ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  const deadline = Date.now() + timeoutMs;
  let actual = await locator.count();

  while (actual !== expected && Date.now() < deadline) {
    await wait(Math.min(pollIntervalMs, Math.max(1, deadline - Date.now())));
    actual = await locator.count();
  }

  if (actual !== expected) {
    throw new Error(
      `数量断言失败：预期元素数量=${expected}，实际=${actual}，等待 ${timeoutMs}ms 超时`
    );
  }
  return actual;
}


function matchesPageUrl({ expectedUrl, mode = 'exact' }) {
  const current = new URL(window.location.href);
  const expected = new URL(String(expectedUrl), current.href);
  const normalizePath = (url) => {
    url.pathname = url.pathname.replace(/\/+$/, '') || '/';
    return url;
  };
  if (mode === 'contains') return current.href.includes(expected.href);
  normalizePath(current);
  normalizePath(expected);
  if (mode === 'path') return `${current.origin}${current.pathname}` === `${expected.origin}${expected.pathname}`;
  return current.href === expected.href;
}

module.exports = { parseExpectedCount, assertLocatorCount, matchesPageUrl };
