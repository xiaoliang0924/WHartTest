"""
UI 自动化用例生成指令

用于 Agent Loop 模式下的 UI 自动化生成任务。
"""

PLAYWRIGHT_SCRIPT_INSTRUCTION = """

## UI 自动化生成与绑定规范
1. 仅当功能用例全部通过后，必须调用一次 `save_generated_ui_testcase` 工具，将本次运行实际观察到的 URL、定位器和操作按步骤保存；严禁二次开启浏览器或编造定位器。
2. UI 步骤组与功能步骤的对应关系必须显式保存。每个 `steps` 项都必须包含 `functional_step_numbers` 数组，填写该 UI 组通过本次真实浏览器动作和断言覆盖的功能步骤编号；一个 UI 组可覆盖多个功能步骤，一个功能步骤也可由多个 UI 组共同覆盖。全部功能步骤都必须至少被一组覆盖。不得为了匹配数量而猜测映射；若任何步骤没有真实操作/断言证据，先补做该步骤，不要调用保存工具。动作按实际执行顺序记录。定位优先使用 role / test_id / id / label / placeholder 等稳定定位；断言值必须来自本次页面观察。
   若功能流程包含不同用户/角色，生成前调用 `list_ui_auth_states` 获取当前项目的可用登录态，并在每个角色对应的 UI 步骤设置其 `auth_state_id`。不同账号必须绑定各自独立且已启用的登录态；不能把环境自动登录的单一账号用于所有角色。若缺少独立登录态，不要保存必然无法通过回放的用例，应说明需要先分别录制并启用这些账号的登录态。
   导航步骤必须反映实际页面状态：若步骤开始时当前 URL 的 origin+pathname 已与目标页面一致，不要重复点击导航菜单，改为使用本次观察到的 URL 保存 `assert mode=url, match=path`。确需点击时，只能定位本次观察到的可见菜单项；遇到隐藏菜单节点不得使用 force click 或仅替换 XPath。
   对列表空结果，必须保留“数据行数量为 0”这一业务断言；可见空态时先等待空态出现，再断言数据行数量为 0。禁止对数据行使用 `wait_for_visible`，因为空列表中数据行本来就不存在。若页面没有可确认的空态，只记录基于实际观察的零行数量断言，不得把它改成等待数据行出现。
3. 不要在 `fill` 动作中保存密码、令牌或其他秘密；密码值应使用 `${{password}}`。
4. 必须检查工具结果。只有工具返回 `status=success` 与 `ui_test_case_id` 后，才可在最终报告中声称 UI 用例已生成并绑定。工具失败时如实报告原因；不得把“已启用生成功能”说成“已生成”。
5. 若功能测试未通过，不要生成 UI 用例；最终结果需说明本次未生成的原因。
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
   当前步骤以本次浏览器工具已返回的连续 `RESULT=PASS` 为准，始终从第一条未通过/未执行步骤继续；每次调用只操作该步骤原文要求的控件。若准备点击的控件只出现在其他步骤中，先停止并重新核对步骤，不得重复上一/下一步动作。
2. **筛选单独成步**：步骤含筛选/查询时，只做选条件 + 查询 + 验收 + 截图，不要同一步点进详情。
   - 普通下拉：`selectFormDropdownOption(page, 字段标签, 选项)`。helper 已检查筛选框当前值；不要再手写 `.el-form-item` / `innerText()` 重复校验，避免依赖页面 DOM 结构。只有筛选控件自身当前值与目标选项完全一致，或 helper 明确返回成功，才可判定筛选成功；禁止用页面任意位置文本存在性代替控件状态，也禁止用列表单元格里的同名状态代替。
   - **按工单状态找行**（如「找到处理中工单」）：必须操作页面真实的「工单状态」下拉（`selectFormDropdownOption(page, '工单状态', '处理中')` 或 `filterWorkOrdersByStatus(page, '处理中')`），不能把状态填进「工单号/工单编号」文本搜索框。
   - **字段名必须来自当前页面**：若用例字段名与页面占位文字不同，先 `describePageForAI(page)` 并基于实际可见控件调用 helper；例如「工单编号」可对应页面的「搜索工单号…」。禁止猜不存在的 placeholder。拿不到控件时，报告实际可见字段与定位错误，不得改写成「缺失业务」。
   - **只打开多选弹窗**：必须调用 `openFilterDialogField(page, 字段标签)`，它会点击字段内真实的 `+` 触发器并等待弹窗。页面字段标题是静态文字，禁止用 `getByText('工单类型').click()` 或猜测 `.el-form-item` 去代替触发器。
   - **选择弹窗选项**：先从当前弹窗读取选项名；完整选择用 `selectDialogMultiSelect(page, 字段标签, 选项名)`。拆为多步时严格按签名 `selectFilterDialogOption(page, 字段标签, 选项名)`，再调用 `confirmFilterDialog(page, 字段标签)`；禁止漏传参数、传 `undefined` 或凭空改选项名。
   - **确认后核验筛选回填**：调用 `assertFilterFieldShows(page, 字段标签, 选项名)`，它读取真实 `.filter-field` 内容并核验完整值。禁止手写 `.el-form-item` / `.el-tag` 定位，因为页面自定义筛选布局不保证这些结构存在，标签视觉截断也不代表值缺失。
   - 日期范围：优先用 `selectFormDateRange(page, 字段标签, 开始日期, 结束日期)`；helper 会对可编辑框填写、对只读框在日历里逐端点选，并核对两个输入值。日期格式必须来自页面观察；只打开日历不能算选择成功。
   - 禁止用 `getByText('某状态').click()` 点表格里的状态文字。
   - 禁止 `getByPlaceholder('请选择工单状态')`：Element Plus 下拉通常没有这个 placeholder。
3. **筛选后验收**：列表列值必须符合该步预期；否则 `RESULT=FAIL` 并停止。
   断言区块/字段用 `helpers.assertPageShows(page, ['基本信息', '工单类型'])`。
   **列表校验必须依据当前页面真实可见内容**：禁止猜测或硬编码 `.el-table__body`、`tbody tr` 等组件内部 CSS 结构；优先用工单号精确文本出现次数，并核对页面显示的结果总数/空结果提示。调用 `assertPageShows` 时必须检查其返回值，不能无条件输出 `RESULT=PASS`。
   **工单状态筛选验收**：先核实每个造数工单号与状态的对应关系；只要求目标状态的造数工单出现、其他状态的造数工单不出现。若页面没有可观察的状态列、徽章或详情证据，必须报告“结果不可验证”，不得根据工单摘要或主题猜状态。
   **按列验收列表**：先读取当前表格可见的列头，再将列头映射到目标字段；禁止凭经验硬编码列序号。列表未展示该字段时，明确报告“列表未展示该字段，无法按列验证”，不要换用未观察到的 DOM 结构。
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
7. **下拉筛选**：`selectFormDropdownOption(page, 字段标签, 选项)`；不要额外用 `.el-form-item`、`innerText()` 手写读取校验。helper 返回失败就标当前步骤失败。禁止 `getByText('某状态').click()`。
8. **控件选择必须验收状态**：普通下拉以筛选框回填值为准；弹窗多选确认前必须观察到选中项/非零计数，确认后检查字段显示所选项；日期范围检查开始和结束日期均已回填。面板打开、元素可见或点击动作本身都不代表选择成功。
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
