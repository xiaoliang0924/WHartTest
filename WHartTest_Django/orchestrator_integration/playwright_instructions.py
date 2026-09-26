"""
UI 自动化用例生成指令

用于 Agent Loop 模式下的 UI 自动化生成任务。
"""

PLAYWRIGHT_SCRIPT_INSTRUCTION = """

## UI 自动化生成与绑定规范
1. **单次流复用**：直接复用本次浏览器执行过程中已获取的元素定位器与步骤组装 UI 自动化用例并绑定，严禁二次开启浏览器。
2. **客观真实断言**：断言中的 URL、页面标题、文本内容必须来源于实际执行观察值，定位器优先使用 data-testid / id / placeholder / 稳定属性组合。
"""

EXECUTION_RESULT_REPORT_FORMAT = """
无论通过、失败，还是脚本报错后无法继续，结束前必须在对话中输出完整报告（禁止只写三行【执行失败】，禁止沉默结束）：

## 测试执行结果: 通过/不通过

### 基本信息
- 测试用例ID:
- 名称:
- 优先级:

### 执行过程与结果
| 步骤 | 操作 | 结果 | 状态 |
|------|------|------|------|
| 1 | … | 符合预期 / 失败原因 | ✅ 通过 / ❌ 失败 / ⏭ 未执行 |

### 问题分析
下面三行**必须逐行写出**，不能省略、不能压缩成一句「无失败步骤」：
- 失败步骤：全部通过写「无」，否则写「步骤N」
- 失败原因：全部通过写「各步骤均满足预期，功能符合需求。」，否则写清原因（缺测试数据 / 页面不符合预期 / 脚本定位失败 等）
- 建议：全部通过写「无需处理。」，否则写具体建议

### 结论
只写**一句话**，套用下面的固定句式，不要展开、不要另起段落：
- 全部通过：本次测试执行全部 N 个步骤均通过，测试通过。
- 有失败：本次测试执行在步骤 N 失败，未完成全部步骤，测试不通过。
未执行的步骤必须标「未执行」，不得标通过。

### 报告之后
报告必须是最后一段内容；若还有其它输出要求（如 JSON 结果），只能紧跟在报告之后，禁止再补写说明或重复报告。
""".strip()


MANUAL_TESTCASE_EXECUTION_HINT = """

## 【用例管理执行】ID 命名空间说明

当请求携带 `test_case_id`（来自用例管理「执行」按钮）时：
- 该 ID 是**用例管理/功能测试用例**的主键，不是 UI 自动化模块 `UiTestCase` 的 ID。
- **读取步骤**：后端已注入完整步骤；**禁止**再调用 `get_testcase_detail` / `get_testcases`（易漏 `--project_id` 导致退出码 2）。
- 若必须调用 whart-test，**必须**带 `--project_id` 与 `--case_id`（见上方注入块）。
- **禁止**直接用 `ui-automation-skill` 的 `get_testcase` / `execute_testcase` 按同一数字 ID 查询（会误报不存在）。
- **浏览器执行**：**只能**用 `playwright-skill`，全程 `session_id="case_<test_case_id>"`。
- **禁止** `playwright-cli` / `browser-use`（snapshot 后 `click eXX` 极易 Element not found，且无法保持登录态）。
- **截图回传（唯一入口）**：
  - 每步结束后调用 `await helpers.screenshotCaseStep(page, <N>);`（N 与步骤编号一致）。
  - 系统会自动将该截图上传到用例详情。
  - **严禁**调用 `whart-test` 的 `upload_screenshot` / `upload_screenshots`；严禁手动上传任何文件。

## 【步骤执行纪律】（违反会导致跳步、虚报通过）

1. **逐步执行**：按步骤编号一步一脚本、一步一截图；禁止跳步、合并或省略。
2. **筛选单独成步**：步骤含筛选/查询时，只做选条件 + 查询 + 验收 + 截图，不要同一步点进详情。
   - 普通下拉：`selectFormDropdownOption(page, 字段标签, 选项)`。选完必须看筛选框里的值，禁止用列表单元格里的同名状态代替。
   - **按工单状态找行**（如「找到处理中工单」）：必须操作页面真实的「工单状态」下拉（`selectFormDropdownOption(page, '工单状态', '处理中')` 或 `filterWorkOrdersByStatus(page, '处理中')`），不能把状态填进「工单号/工单编号」文本搜索框。
   - **字段名必须来自当前页面**：若用例字段名与页面占位文字不同，先 `describePageForAI(page)` 并基于实际可见控件调用 helper；例如「工单编号」可对应页面的「搜索工单号…」。禁止猜不存在的 placeholder。拿不到控件时，报告实际可见字段与定位错误，不得改写成「缺失业务」。
   - 弹窗多选：`selectDialogMultiSelect(page, 字段标签, 选项名)`
   - 禁止用 `getByText('某状态').click()` 点表格里的状态文字。
   - 禁止 `getByPlaceholder('请选择工单状态')`：Element Plus 下拉通常没有这个 placeholder。
3. **筛选后验收**：列表列值必须符合该步预期；否则 `RESULT=FAIL` 并停止。
   断言区块/字段用 `helpers.assertPageShows(page, ['基本信息', '工单类型'])`。
   禁止 `getByText('工单类型*')`：必填星号是独立节点，整段匹配会误判缺失。
   断言用 `.first()` / `getByRole`，避免 `getByText` 命中多个节点。
   「xx标签」以页面徽章文案为准（如高/中/低），不要只搜字段名本身。
4. **截图**：每步 `screenshotCaseStep(page, N)`，系统自动上传。禁止 `upload_screenshot`。第 N 步图必须是完成第 N 步后的页面。
5. **结束必须输出完整报告**。脚本 SyntaxError 先改当前步再重试。
6. **保留原始失败原因**：Playwright 的 `locator` / `fill` / `click` 超时、未找到控件等错误，报告必须指出原始控件和失败步骤；不得归因为“缺失业务”或未执行过的后续步骤。

""" + EXECUTION_RESULT_REPORT_FORMAT + """

## 【Playwright 执行铁律】不遵守会出现「命令执行失败 (退出码 1)」

1. **全程同一个 session_id**：所有 `execute_skill_script(skill_name="playwright-skill")` 必须带 `session_id="case_<test_case_id>"`。直接使用已有 `page`，**禁止** `const { chromium } = require('playwright')` / `chromium.launch()` / `newPage()` / `browser.close()`。
2. **登录或跳转后立刻** `await helpers.dismissBlockingDialogs(page);` 关掉「我知道了」类提示，否则点击会被遮罩拦截。
3. **禁止 `#el-id-*`**：Element Plus 动态 ID 每次刷新都变。用 `getByRole('button', { name: '...' })`、`getByPlaceholder(...)`、`getByText(...)`。
4. **容器内必须无头**：不要 `headless: false`。
5. **产品不符合预期不要杀进程**：断言失败时 `console.log('RESULT=FAIL: ...')` + 截图上传，禁止 `throw` / `process.exit(1)`。通过则 `RESULT=PASS`。定位超时才允许脚本失败。
6. **步骤1登录**：该步只调用 `await helpers.loginStep1(page);`（已含步骤1截图，禁止再调用 `screenshotCaseStep(page, 1)`）。**禁止**把登录 URL、`802714/000000` 整段、或密码作为 `loginStep1` 的位置参数传入（账号密码已由环境变量注入）。只有 stdout 出现 `RESULT=PASS` 才算成功；出现 `RESULT=FAIL` 必须停止。步骤 N 截图：`await helpers.screenshotCaseStep(page, N);`。禁止手写路径、禁止 Python 风格 goto/fill/click。
7. **下拉筛选**：`selectFormDropdownOption(page, 字段标签, 选项)`。禁止 `getByText('某状态').click()`。
8. **菜单页不要走错**：步骤写哪个菜单就进哪个页。优先用 `navigateByMenu(page, 菜单名)`；明确知道目标路由时才传唯一 URL 片段，禁止传 `/work-order` 这类父级公共前缀。
"""

CLAIMABLE_PENDING_EXECUTION_HINT = """

## 【待处理未分配→领取工单 — 强制专用 helper】

当用例含「待处理、未分配、领取工单、列表点处理进详情」时：

1. **每一步只允许一行脚本**：`await helpers.runClaimableTicketCaseStep(page, <步骤号>);`
2. **禁止**手写 `fillFilterField` / `selectFormDropdownOption` / `clickRowAction` / `getByRole('button', { name: '处理' })` 等组合去筛状态或点「处理」。
3. **禁止**因 TYPE_A 造数工单只在「待分配」列表就判用例不适用或步骤3/4失败；helper 已兼容：待分配时用工单号链接进详情并校验「领取工单」。
4. **禁止** `ticketId` / URL 直达详情（不会出现「领取工单」）。
5. 步骤4 只有 stdout 出现 `RESULT=PASS` 且含「领取工单」才算通过；`RESULT=FAIL` 必须停止，禁止改报通过。
6. **截图纪律**：步骤 1～N 每一步都必须单独执行 `runClaimableTicketCaseStep` 一次；工具输出须含 `[CASE_SCREENSHOT]` 且用例详情已出现「步骤N」截图。缺任一步截图 **不得** 在报告中写通过。

"""

TRANSFER_UI_EXECUTION_HINT = """

## 【转派 UI — 强制专用 helper】

当用例名称含「转派按钮可见性」或「转派弹窗」时：

1. **每一步只允许一行脚本**：`await helpers.runTransferTicketCaseStep(page, <步骤号>);`
2. **禁止**改用 `runClaimableTicketCaseStep`（那是领取流程，最多 5 步，无法覆盖转派弹窗 9 步）。
3. **禁止**手写筛选/点处理/弹窗选人脚本；helper 会注入造数工单号与转派目标姓名。
4. 按钮可见性用例只跑到步骤4；弹窗成功流程跑完选择对象→填原因→二次确认。
5. `RESULT=FAIL` 必须停止，禁止改报通过。
6. **截图纪律**：每一步都必须单独执行 `runTransferTicketCaseStep` 一次；缺任一步截图 **不得** 在报告中写通过。

"""

PROCESSING_HANDOVER_EXECUTION_HINT = """

## 【处理中工单→移交审批 — 强制专用 helper】

当用例属于移交审批流程（含提交前、提交后及拦截校验）时：

1. **每一步只允许一行脚本**：`await helpers.runProcessingHandoverCaseStep(page, <步骤号>);`
2. **禁止**改用 `runClaimableTicketCaseStep`（会按「待处理」筛选，找不到已是「处理中」的造数工单）。
3. **禁止**手写筛选/点处理；helper 会按造数工单号及用例需要的审批状态定位。
4. 造数与执行会按步骤识别提交前/提交后、审批详情、审批人列表及原因边界等流程；必须使用本次造数工单。
5. `RESULT=FAIL` 必须停止，禁止改报通过。

"""

PENDING_NO_HANDOVER_EXECUTION_HINT = """

## 【待处理详情页不展示移交审批按钮 — 强制专用 helper】

本用例只有 3 步：登录、进入本次造数的待处理工单详情、检查右上角按钮区域。
每一步只调用 `await helpers.runProcessingHandoverCaseStep(page, <步骤号>);`。
步骤 2 必须进入本次工单详情；步骤 3 必须在详情页确认「移交审批」按钮不可见。
禁止调用 `runClaimableTicketCaseStep`；它的步骤 3 只会筛选列表，不能验证本用例。
只有步骤 3 输出 `RESULT=PASS: 步骤3已确认待处理工单详情未显示移交审批按钮`
且有步骤 3 截图时，才可报告通过。

"""

COMPLETE_TICKET_UI_EXECUTION_HINT = """

## 【处理中工单→工单完成 — 按步骤语义执行的专用 helper】

当用例名称/步骤含「点击工单完成 / 确认弹窗 / 确认完成」时：

1. **每一步只允许一行脚本**：`await helpers.runCompleteTicketCaseStep(page, <步骤号>);`
2. **禁止**改用 `runClaimableTicketCaseStep`（会按「待处理」筛选，截图停在列表页，与步骤文案不符）。
3. helper 按当前用例每步描述执行：进详情、检查按钮、打开确认弹窗、确定或取消，并验证对应状态/沟通区；不要把不同用例都当作三步流程。
4. `RESULT=FAIL` 必须停止，禁止改报通过。

"""

OVERVIEW_SLA_DETAIL_EXECUTION_HINT = """

## 【工单总览-SLA进详情 — 强制专用 helper】

当用例名称/步骤含「工单总览 + SLA预警 + 点击工单ID/进详情」时：

1. **每一步只允许一行脚本**：`await helpers.runOverviewSlaDetailCaseStep(page, <步骤号>);`
2. **禁止**手写 `.cl-table` / `.el-table` / `a.tl-link` / `getByText('基本信息')` 等定位去点 SLA 表或验详情。
3. **禁止**使用 `runOverviewCaseStep`（那是 KPI/图表类总览用例，不含 SLA 点链进详情）。
4. helper 会自动：滚到 SLA 预警明细 → 点第一行蓝色工单号 → 等详情页 → 校验工单号。
5. **本用例故意不走自动造数**，依赖环境「工单总览 → SLA预警明细」已有可点击工单号。
6. 若 stdout 出现 `SLA预警明细表无数据行` / `未找到蓝色工单号链接`：停止并如实报告「环境缺少 SLA 预警预置数据」；**禁止**在建议里写「补充造数 / 造数脚本」。

"""
