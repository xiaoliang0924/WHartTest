"""
UI 自动化用例生成指令

用于 Agent Loop 模式下的 UI 自动化生成任务。
"""

PLAYWRIGHT_SCRIPT_INSTRUCTION = """

## 【强制要求】UI自动化用例生成

## 主要流程
1.判断之前是否生成过UI自动化用例，有则基于存在的用例进行修改和完善，没有则生成新的用例。
2.具体怎么保存用例查看 ui-automation 工具的操作方法和描述。
3.记得执行一下，确定保存的用例是可执行的。

**重要 本次任务必须在执行完所有功能测试步骤后，生成并调用相应工具保存UI自动化用例。**

### 断言规则（非常重要）
1. **禁止猜测 URL**：断言中的 URL 必须使用执行步骤时**实际观察到的 URL**，不要自己编造或猜测
2. **禁止使用通配符模式**：不要使用 `**/dashboard` 这样的模式，必须使用完整的实际 URL
3. **断言必须来源于实际结果**所有断言值 URL、标题、文本等必须是执行过程中**实际看到的值**
4. **当无法确定元素的具体文本时，优先使用可见性断言
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
- 失败步骤：
- 失败原因：（缺测试数据 / 页面不符合预期 / 脚本定位失败 等，写清楚）
- 建议：

### 结论
未执行的步骤必须标「未执行」，不得标通过。
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

""" + EXECUTION_RESULT_REPORT_FORMAT + """

## 【Playwright 执行铁律】不遵守会出现「命令执行失败 (退出码 1)」

1. **全程同一个 session_id**：所有 `execute_skill_script(skill_name="playwright-skill")` 必须带 `session_id="case_<test_case_id>"`。直接使用已有 `page`，**禁止** `const { chromium } = require('playwright')` / `chromium.launch()` / `newPage()` / `browser.close()`。
2. **登录或跳转后立刻** `await helpers.dismissBlockingDialogs(page);` 关掉「我知道了」类提示，否则点击会被遮罩拦截。
3. **禁止 `#el-id-*`**：Element Plus 动态 ID 每次刷新都变。用 `getByRole('button', { name: '...' })`、`getByPlaceholder(...)`、`getByText(...)`。
4. **容器内必须无头**：不要 `headless: false`。
5. **产品不符合预期不要杀进程**：断言失败时 `console.log('RESULT=FAIL: ...')` + 截图上传，禁止 `throw` / `process.exit(1)`。通过则 `RESULT=PASS`。定位超时才允许脚本失败。
6. **步骤1登录**：该步只调用 `await helpers.loginStep1(page);`（已含步骤1截图，禁止再调用 `screenshotCaseStep(page, 1)`）。只有 stdout 出现 `RESULT=PASS` 才算成功；出现 `RESULT=FAIL` 必须停止。步骤 N 截图：`await helpers.screenshotCaseStep(page, N);`。禁止手写路径、禁止 Python 风格 goto/fill/click。
7. **下拉筛选**：`selectFormDropdownOption(page, 字段标签, 选项)`。禁止 `getByText('某状态').click()`。
8. **菜单页不要走错**：步骤写哪个菜单就进哪个页。优先用 `navigateByMenu(page, 菜单名)`；明确知道目标路由时才传唯一 URL 片段，禁止传 `/work-order` 这类父级公共前缀。
"""
