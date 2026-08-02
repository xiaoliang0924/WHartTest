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

MANUAL_TESTCASE_EXECUTION_HINT = """

## 【用例管理执行】ID 命名空间说明

当请求携带 `test_case_id`（来自用例管理「执行」按钮）时：
- 该 ID 是**用例管理/功能测试用例**的主键，不是 UI 自动化模块 `UiTestCase` 的 ID。
- **读取步骤**：使用 `whart-test` → `get_testcase_detail --project_id <项目ID> --case_id <test_case_id>`。
- **禁止**直接用 `ui-automation-skill` 的 `get_testcase` / `execute_testcase` 按同一数字 ID 查询（会误报不存在）。
- **浏览器执行**：优先 `agent-browser-skill`，必要时 `playwright-skill`。
- **截图回传**：使用 `whart-test` 的 `upload_screenshot` / `upload_screenshots`，`case_id` 与上述 test_case_id 相同。
"""
