import type { Router } from 'vue-router';

const SESSION_STORAGE_KEY = 'langgraph_session_id';
const PROJECT_STORAGE_KEY = 'selected_project_id';

export interface OpenLangGraphChatOptions {
  /** 用例执行：在目标页启动 Agent Loop 流（避免仅原页持有 SSE） */
  execAutostart?: boolean;
  testCaseId?: number;
  promptId?: number | null;
  generatePlaywrightScript?: boolean;
  executionMode?: 'hybrid' | 'script_only' | 'ai_only';
  uiTestCaseId?: number | null;
}

function appendExecCaseQuery(query: Record<string, string>, options?: OpenLangGraphChatOptions): void {
  if (!options?.execAutostart) {
    return;
  }
  query.exec_autostart = '1';
  if (options.testCaseId) {
    query.test_case_id = String(options.testCaseId);
  }
  if (options.promptId) {
    query.prompt_id = String(options.promptId);
  }
  if (options.generatePlaywrightScript) {
    query.generate_playwright_script = '1';
  }
  if (options.executionMode) {
    query.execution_mode = options.executionMode;
  }
  if (options.uiTestCaseId != null) {
    query.ui_test_case_id = String(options.uiTestCaseId);
  }
}

/** Open LLM chat in a new tab so the current page stays on test case management. */
export function openLangGraphChatInNewWindow(
  router: Router,
  sessionId?: string | null,
  projectId?: number | null,
  options?: OpenLangGraphChatOptions,
): void {
  if (sessionId) {
    localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
  }
  if (projectId) {
    localStorage.setItem(PROJECT_STORAGE_KEY, String(projectId));
  }

  const query: Record<string, string> = {};
  if (sessionId) {
    query.session_id = sessionId;
  }
  if (projectId) {
    query.project_id = String(projectId);
  }
  appendExecCaseQuery(query, options);

  const { href } = router.resolve({
    name: 'LangGraphChat',
    query: Object.keys(query).length > 0 ? query : undefined,
  });
  window.open(href, '_blank', 'noopener,noreferrer');
}

/** Navigate to LLM chat in the current tab. */
export function openLangGraphChatInCurrentPage(
  router: Router,
  sessionId?: string | null,
  projectId?: number | null,
  options?: OpenLangGraphChatOptions,
): void {
  if (sessionId) {
    localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
  }
  if (projectId) {
    localStorage.setItem(PROJECT_STORAGE_KEY, String(projectId));
  }

  const query: Record<string, string> = {};
  if (sessionId) {
    query.session_id = sessionId;
  }
  if (projectId) {
    query.project_id = String(projectId);
  }
  appendExecCaseQuery(query, options);

  router.push({
    name: 'LangGraphChat',
    query: Object.keys(query).length > 0 ? query : undefined,
  });
}
