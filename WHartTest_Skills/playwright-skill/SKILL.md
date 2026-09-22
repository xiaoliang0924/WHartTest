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
| **验收点在左侧导航栏**的步骤（「菜单展开」「菜单高亮」「子菜单出现」） | `await helpers.screenshotNavStep(page, <步骤号>);`；普通 `screenshotCaseStep` 只截主内容区，**左侧导航会被裁掉**，这类预期结果在图上完全看不到 |
| 列表页截图 | 自动包含搜索/筛选区 + 列表；步骤≥2 时若仍在登录页会报 `RESULT=FAIL` |
| 关遮挡弹窗 | `await helpers.dismissBlockingDialogs(page);` |
| 点按钮（先关遮罩/侧栏） | `await helpers.clickPageButton(page, '查询');`；普通按钮名带 `+` 时也可写成 `+ 创建工单`。**但筛选字段旁边的「+」不是按钮**，见下一行 |
| 步骤：打开筛选字段的多选弹窗（如「工单类型」） | `await helpers.openFilterDialogField(page, '工单类型');`。这类字段是**弹窗式触发器**：**点击字段本身**即可打开弹窗。字段旁的「+」只是装饰图标（真实 DOM 是 `<i class="el-icon add-icon"><svg/></i>`，没有 role / aria-label / 文本），不要去找它 |
| 步骤：在弹窗里勾选 1 个选项 | `await helpers.selectFilterDialogOption(page, '工单类型', '快递派送');`。选项名**必须是弹窗里真实存在的名称**（`.picker-item-name` 的文案）；写错不会静默超时，而是抛错并把**全部可选项 + 最接近的名字**列出来 |
| **选项名不确定时先查真实名单** | `await helpers.listFilterDialogOptions(page, '工单类型');` → 返回弹窗里全部选项名（只读，自动关闭弹窗）。**严禁**凭猜测写选项名后反复重试：报错里已明确说明「这是用例选项名与被测系统数据不一致，重试不会成功」，此时应把差异暴露给人工/改用例，不要换一个名字继续猜 |
| 步骤：点弹窗「确定」 | `await helpers.confirmFilterDialog(page, '工单类型');`。未勾选时按钮文案是「确定（0）」且 disabled，helper 会等它变为可点 |
| 弹窗多选（一步到底：弹窗+勾选+确定） | `await helpers.selectDialogMultiSelect(page, '字段标签', '选项名');`；**勾多个就传数组**：`selectDialogMultiSelect(page, '工单类型', ['快递派送', '快递UPS派送'])`。字段若是普通下拉会自动走下拉分支 |
| 断言区块/字段可见 | `await helpers.assertPageShows(page, ['基本信息', '工单类型', '工单摘要']);`；禁止 `getByText('工单类型*')`，必填星号是独立节点 |
| 列表行点操作 | `await helpers.clickRowAction(page, '行内文本', '按钮名');` |
| 普通下拉 | `await helpers.selectFormDropdownOption(page, '工单状态', '待处理');`；选完以筛选框当前值为准，禁止用列表里的同名文字代替 |
| 筛选项填值 | `await helpers.fillFilterField(page, '字段标签', '值');` |
| 点菜单并校验 | `await helpers.navigateByMenu(page, '父菜单 > 子菜单', null, '/目标路由');`。**步骤里给了路由片段（如 `/work-order/tickets`）就把它作第 4 参传进去作兜底** —— 菜单被遮挡/子菜单折叠时会自动 goto 该路由；禁止用 `/work-order` 这类父级公共前缀 |
| 点嵌套菜单路径 | `await helpers.navigateByMenuPath(page, ['父菜单', '子菜单']);`；禁止使用父级公共 URL 前缀 |
| 组合筛选 | `await helpers.filterByFields(page, { dropdowns: [{ fieldLabel, option }] });` |
| 工单详情类（查询→进详情→沟通区） | `await helpers.runTicketDetailCaseStep(page, <步骤号>);`（ticketNo 来自 WHARTTEST_TICKET_NO） |
| 工单总览 SLA 预警点工单号进详情 | **每步只调用** `await helpers.runOverviewSlaDetailCaseStep(page, <步骤号>);`（禁止手写 cl-table/tl-link 定位） |
| 看页面结构 | `await helpers.describePageForAI(page);` |

登录地址/账号优先用环境变量：`WHARTTEST_LOGIN_URL`、`WHARTTEST_USERNAME`、`WHARTTEST_PASSWORD`。

> **登录页已于 2026-09 改版为「统一身份认证」网关**（卡片式「自动登录 / 手动登录」，页面上没有 `请输入用户名` 输入框，也没有账号密码输入框，这是正常的）。
> `loginStep1` 已内置适配：自动点「手动登录」→ 等认证中心弹窗（`bot.by56.com/auth-admin`）→ 用 `WHARTTEST_USERNAME`/`WHARTTEST_PASSWORD`
> （缺省 `802714`/`000000`）填 `请输入账号`/`请输入密码` → 点「登录并继续」。**不要自己另写登录脚本**，
> 也不要在页面找 `请输入用户名`。登录地址会自动把 `http` 升级为 `https`（用 http 会被认证中心以
> "Frontend Origin is not allowed" 拒绝）。认证中心限流时 `loginStep1` 会返回 `RESULT=FAIL: 登录失败（认证中心限流/锁定）`。

## 截图

- 只写 `await helpers.screenshotCaseStep(page, N);`
- **验收点在左侧导航栏时改用 `await helpers.screenshotNavStep(page, N);`**
  （例：步骤「在左侧导航栏点击【工单中心】菜单」，预期「【工单中心】菜单展开」。
  普通 `screenshotCaseStep` 只截主内容面板，侧栏被裁掉 → 图上只有内容区，看不到菜单展开，
  会被误判成「截图截错了」。`screenshotNavStep` 截整页视口，含左侧导航。）
- **点一级父菜单常会同时跳默认页**：点「工单中心」后系统会跳到 `/work-order/dashboard`，
  主内容区显示「数据总览」属正常现象，不代表点错。要证明「菜单展开」必须带侧栏截图。
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
