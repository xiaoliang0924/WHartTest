const assert = require('node:assert/strict');
const Module = require('node:module');

const originalLoad = Module._load;
Module._load = function mockPlaywright(request, parent, isMain) {
  if (request === 'playwright') return {};
  return originalLoad.call(this, request, parent, isMain);
};
const { completeTicketActionForStep } = require('../lib/helpers');
Module._load = originalLoad;

const examples = [
  [2, '在左侧导航进入【工单中心】>【我的工单】', '', 'enter_list'],
  [3, '找到状态为「处理中」的目标测试工单，点击操作列「处理」按钮进入详情页', '', 'enter_detail'],
  [4, '查看页面右上角按钮区域', '可见绿色「工单完成」按钮', 'inspect_complete_button'],
  [3, '点击绿色「工单完成」按钮', '弹出确认完成弹窗', 'open_confirm_dialog'],
  [4, '点击弹窗蓝色「确定」按钮', '状态变为已解决', 'confirm_complete'],
  [5, '查看顶部状态徽标', '已解决', 'verify_resolved_badge'],
  [3, '点击「工单完成」，在确认弹窗点击「确定」', '已解决', 'open_and_confirm'],
  [4, '查看页面底部沟通区域', '不展示消息输入框，无法再发送', 'verify_resolved_composer_hidden'],
  [2, '进入【工单中心】>【我的工单】，点击目标工单「处理」进入详情页，点击「工单完成」并点击「确定」', '已解决', 'enter_detail_and_confirm'],
  [3, '返回【我的工单】列表页面，按工单号搜索目标工单', '当前状态已解决', 'verify_resolved_list'],
  [3, '点击「工单完成」按钮', '审批未完成，请等待审批完成', 'blocked_approval'],
  [4, '查看工单状态和底部沟通区', '仍为处理中', 'verify_processing_composer'],
  [4, '点击弹窗「取消」按钮', '仍为处理中', 'cancel_complete'],
  [5, '查看底部沟通区，尝试输入消息并点击发送', '发送成功', 'send_message_processing'],
];

for (const [step, description, expected, action] of examples) {
  const input = JSON.stringify({ [step]: { description, expected } });
  assert.equal(completeTicketActionForStep(step, input), action, description);
}
assert.equal(completeTicketActionForStep(6, '{}'), null);
assert.equal(completeTicketActionForStep(4, '{broken'), null);
console.log(`${examples.length} completion-step routing examples passed`);
