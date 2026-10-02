# 用例 1173 自动生成 UI 用例中断排查

## 核验结果

生产执行记录 1145，session_id 为 1a0fcbc67ffdc5a54d5136158。功能执行状态 pass，generate_playwright_script=true，但 TestCase.ui_test_case_id 为 null，报告没有“UI 自动化用例生成”结果。此次未保存并绑定 UI 用例。

北京时间 21:11:04 功能执行完成。21:14:08 前端 Nginx 记录 GET /ui-automation，同时该对话的 POST /api/orchestrator/agent-loop/ 流结束；后端在同一秒记录 Testcase stream cancelled。未发现本次生成保存完成、生成失败或登录失败记录。访问日志和取消日志支持页面离开导致请求中断；不能据此判断用户是否有意停止任务。

## 代码依据

1. LangGraphChatView.vue 在 onUnmounted 中无条件 abortController.abort()；后台生成位于同一 SSE 请求中，未独立于客户端请求运行。刷新或离开页面会中断后续流程。
2. agent_loop_view.py 先保存功能测试通过的报告，再发送 state=generating_ui_case 的 testcase_progress 事件，然后等待 _finalize_generated_ui_case。
3. chatService.ts 的初次执行、恢复执行两条路径，仅当事件有 step 时更新进度。上述生成事件没有 step，因此提示被忽略。
4. 后端取消处理调用 finalize_testcase_run_record，但该函数遇到已经不为 running 的记录直接返回。功能已通过时，UI 生成取消不会写进报告。
5. 强制生成调用 await bound_model.ainvoke，期间没有单独的阶段总时限、周期进度或开始日志。本次模型配置单次请求超时 120 秒、最多重试 3 次；现有日志无法确定取消前模型是否在重试，不能把三分钟等待归因于登录。

## 修复方向

- 明确区分功能执行、UI 生成、回放验证三个阶段；无 step 的阶段事件也更新进度。
- 生成和验证作为可持久化的后台任务运行，前端通过任务状态获取结果，刷新或切页不取消任务；仅显式停止操作取消任务。
- 保存生成中、生成成功、生成失败/取消及验证结果，确保已通过的功能记录仍展示 UI 生成未完成的事实。
- 为生成模型调用设置阶段总时限和可定位日志，超时明确报告，避免长时间静默。

本次为只读生产排查，未重跑建单流程，未更改生产数据库、代码、部署或模型配置。
