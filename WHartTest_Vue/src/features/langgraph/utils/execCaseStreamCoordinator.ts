const deferKey = (sessionId: string) => `wharttest_defer_exec_stream_${sessionId}`;

/** 用户已选择到 LLM 对话页执行，对应用例 session 不再在用例管理页启动 SSE。 */
export function markDeferExecStreamToChat(sessionId: string): void {
  if (!sessionId) {
    return;
  }
  try {
    sessionStorage.setItem(deferKey(sessionId), '1');
  } catch {
    // ignore
  }
}

export function resetDeferExecStreamToChat(sessionId: string): void {
  if (!sessionId) {
    return;
  }
  try {
    sessionStorage.removeItem(deferKey(sessionId));
  } catch {
    // ignore
  }
}

export function shouldDeferExecStreamToChat(sessionId: string): boolean {
  if (!sessionId) {
    return false;
  }
  try {
    return sessionStorage.getItem(deferKey(sessionId)) === '1';
  } catch {
    return false;
  }
}
