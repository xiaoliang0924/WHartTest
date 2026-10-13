'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { roleLocator, dynamicCountRoleLocator } = require('./role_locator');
const container = { getByRole: (role, options) => ({ role, options }) };
test('named role expressions become accessible-name locators', () => {
  assert.deepEqual(roleLocator(container, 'button[name="登录"]'), {
    role: 'button', options: { name: '登录', exact: true },
  });
  const result = roleLocator(container, 'button[name=/图片/i]');
  assert.equal(result.role, 'button');
  assert.ok(result.options.name.test('发送图片'));
  assert.equal(result.options.name.test('发送文件'), false);
  assert.deepEqual(roleLocator(container, 'textbox'), { role: 'textbox', options: undefined });
});
test('malformed roles fail rather than producing a false zero count', () => {
  for (const value of ['button[name=图片]', 'bogus', 'button[name=/图片/g]', 'button[name="图片]']) {
    assert.throws(() => roleLocator(container, value));
  }
});

test('dynamic selection counts produce a narrowly anchored button fallback', () => {
  assert.equal(dynamicCountRoleLocator('button[name="确定"]'),
    'button[name=/^确定(?:\\s*[（(]\\s*\\d+\\s*[）)])?$/]');
  assert.equal(dynamicCountRoleLocator('button[name="查询"]'),
    'button[name=/^查询(?:\\s*[（(]\\s*\\d+\\s*[）)])?$/]');
  assert.equal(dynamicCountRoleLocator('link[name="确定"]'), null);
  assert.equal(dynamicCountRoleLocator('button[name=/确定/]'), null);
});
