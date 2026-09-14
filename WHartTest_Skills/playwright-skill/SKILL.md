---
name: playwright-skill
description: 通用浏览器自动化。用于页面操作、表单填写、登录、截图。不绑定具体产品和用例。
---

# Playwright 浏览器自动化

通用浏览器执行工具。产品文案、菜单路径、筛选字段以**当前用例步骤**为准，不要写死某个系统。

## 强制规则

1. **同一 session_id**：多步骤必须全程 `session_id="case_<用例ID>"`，直接用已有 `page`。禁止 `chromium.launch()` / `newPage()` / `browser.close()`。
2. **跳转后关遮挡弹窗**：`await helpers.dismissBlockingDialogs(page);` 只关「我知道了」类提示，不要点业务弹窗的确定/取消。
3. **禁止 `#el-id-*`**：用 `getByRole` / `getByPlaceholder` / `getByText`。
4. **容器必须无头**：有 session_id 时不要自己 launch。
5. **断言失败不要杀进程**：`console.log('RESULT=FAIL: ...')` + 截图。通过则 `RESULT=PASS`。禁止 `throw` / `process.exit(1)`。

## 通用 helpers

| 用途 | 调用 |
|------|------|
| 步骤1：先截登录页再登录 | 该步只调用 `await helpers.loginStep1(page);`（已含步骤1截图，禁止再调用 `screenshotCaseStep(page, 1)`；只有 `RESULT=PASS` 才算成功） |
| 按步骤截图（系统自动上传） | `await helpers.screenshotCaseStep(page, <步骤号>);` |
| 列表页截图 | 自动包含搜索/筛选区 + 列表；步骤≥2 时若仍在登录页会报 `RESULT=FAIL` |
| 关遮挡弹窗 | `await helpers.dismissBlockingDialogs(page);` |
| 点按钮（先关遮罩/侧栏） | `await helpers.clickPageButton(page, '查询');`；按钮名带 `+` 时也可写成 `+ 创建工单` |
| 断言区块/字段可见 | `await helpers.assertPageShows(page, ['基本信息', '工单类型', '工单摘要']);`；禁止 `getByText('工单类型*')`，必填星号是独立节点 |
| 列表行点操作 | `await helpers.clickRowAction(page, '行内文本', '按钮名');` |
| 普通下拉 | `await helpers.selectFormDropdownOption(page, '工单状态', '待处理');`；选完以筛选框当前值为准，禁止用列表里的同名文字代替 |
| 筛选项填值 | `await helpers.fillFilterField(page, '字段标签', '值');` |
| 弹窗多选（带 + 的筛选） | `await helpers.selectDialogMultiSelect(page, '字段标签', '选项名');` |
| 点菜单并校验 | `await helpers.navigateByMenu(page, '菜单名');`；也兼容 `'父菜单 > 子菜单'`，明确知道目标路由时才传唯一 URL 片段 |
| 点嵌套菜单路径 | `await helpers.navigateByMenuPath(page, ['父菜单', '子菜单']);`；禁止使用父级公共 URL 前缀 |
| 组合筛选 | `await helpers.filterByFields(page, { dropdowns: [{ fieldLabel, option }] });` |
| 工单详情类（查询→进详情→沟通区） | `await helpers.runTicketDetailCaseStep(page, <步骤号>);`（ticketNo 来自 WHARTTEST_TICKET_NO） |
| 工单总览 SLA 预警点工单号进详情 | **每步只调用** `await helpers.runOverviewSlaDetailCaseStep(page, <步骤号>);`（禁止手写 cl-table/tl-link 定位） |
| 看页面结构 | `await helpers.describePageForAI(page);` |

登录地址/账号优先用环境变量：`WHARTTEST_LOGIN_URL`、`WHARTTEST_USERNAME`、`WHARTTEST_PASSWORD`。

## 截图

- 只写 `await helpers.screenshotCaseStep(page, N);`
- 系统自动上传到用例详情
- **禁止**调用 `upload_screenshot` / `upload_screenshots`
- **禁止**手写 `case_xxx.png` 路径字符串

## 调用方式

```
node run.js "await helpers.loginStep1(page);"
```

代码一行、分号分隔。也可以只传以 `await`/`const` 开头的裸 JS。

## 逐步执行

按用例步骤编号一步一脚本、一步一截图。登录步骤的截图由 `loginStep1` 自带，不要重复截图。登录输出 `RESULT=FAIL` 时立即停止，禁止继续菜单步骤。筛选步只做筛选和验收，不要在同一步点进详情；若用例已拆分为「步骤3查询、步骤4进详情」，则分别执行，或使用 `runTicketDetailCaseStep`。
禁止用 `page.getByText('某状态').click()` 选状态下拉（表格里常有多行同文案）。
