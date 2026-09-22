const EXEC_CASE_CONTEXT_PREFIX = 'langgraph_exec_case_context_';

export interface ExecCaseSessionContext {
  message: string;
  displayMessage?: string;
  promptId?: number | null;
  testCaseId?: number;
  projectId?: number;
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
