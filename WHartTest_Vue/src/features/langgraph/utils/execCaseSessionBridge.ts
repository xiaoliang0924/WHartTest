const EXEC_CASE_CONTEXT_PREFIX = 'langgraph_exec_case_context_';

export interface ExecCaseSessionContext {
  message: string;
  displayMessage?: string;
  promptId?: number | null;
  testCaseId?: number;
  projectId?: number;
  generatePlaywrightScript?: boolean;
  executionMode?: 'hybrid' | 'script_only' | 'ai_only';
  uiTestCaseId?: number | null;
  storedAt: number;
}

const readContext = (raw: string | null): ExecCaseSessionContext | null => {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as ExecCaseSessionContext;
    if (typeof parsed.message === 'string' && parsed.message.trim()) {
      return parsed;
    }
  } catch {
    // ignore invalid cache
  }
  return null;
};

export function storeExecCaseSessionContext(
  sessionId: string,
  payload: Omit<ExecCaseSessionContext, 'storedAt'>,
): void {
  if (!sessionId) return;
  const context: ExecCaseSessionContext = {
    ...payload,
    storedAt: Date.now(),
  };
  localStorage.setItem(`${EXEC_CASE_CONTEXT_PREFIX}${sessionId}`, JSON.stringify(context));
}

export function getExecCaseSessionContext(sessionId?: string | null): ExecCaseSessionContext | null {
  if (!sessionId) return null;
  return readContext(localStorage.getItem(`${EXEC_CASE_CONTEXT_PREFIX}${sessionId}`));
}

export function clearExecCaseSessionContext(sessionId?: string | null): void {
  if (!sessionId) return;
  localStorage.removeItem(`${EXEC_CASE_CONTEXT_PREFIX}${sessionId}`);
}

export function buildExecCaseAgentMessage(testCaseId: number): string {
  return `执行用例管理测试用例 ID=${testCaseId}。请使用后端注入的最新用例步骤和执行规则。`;
}

function parseOptionalInt(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === 'string' && value.trim()) {
    const parsed = parseInt(value, 10);
    if (!Number.isNaN(parsed)) {
      return parsed;
    }
  }
  return undefined;
}

/** 从 localStorage 或 URL 查询参数还原用例执行 autostart 上下文（新窗口/刷新兜底）。 */
export function resolveExecCaseAutostartContext(
  sessionId: string,
  routeQuery: Record<string, unknown>,
): Omit<ExecCaseSessionContext, 'storedAt'> | null {
  const stored = getExecCaseSessionContext(sessionId);
  if (stored?.testCaseId) {
    return stored;
  }

  const testCaseId = parseOptionalInt(routeQuery.test_case_id);
  if (!testCaseId) {
    return null;
  }

  const promptId = parseOptionalInt(routeQuery.prompt_id);
  const projectId = parseOptionalInt(routeQuery.project_id);
  const generatePlaywrightScript = routeQuery.generate_playwright_script === '1'
    || routeQuery.generate_playwright_script === 'true';
  const requestedMode = routeQuery.execution_mode;
  const executionMode = requestedMode === 'hybrid'
    || requestedMode === 'script_only'
    || requestedMode === 'ai_only'
    ? requestedMode
    : undefined;
  const uiTestCaseId = parseOptionalInt(routeQuery.ui_test_case_id) ?? null;

  const message = buildExecCaseAgentMessage(testCaseId);
  return {
    message,
    displayMessage: message,
    promptId: promptId ?? null,
    testCaseId,
    projectId,
    generatePlaywrightScript,
    executionMode,
    uiTestCaseId,
  };
}
