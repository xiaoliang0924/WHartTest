const RUNNING_SESSION_KEY = 'langgraph_running_session';

export function markSessionRunning(sessionId: string): void {
  if (!sessionId) return;
  localStorage.setItem(RUNNING_SESSION_KEY, sessionId);
}

export function clearSessionRunning(sessionId?: string | null): void {
  const current = localStorage.getItem(RUNNING_SESSION_KEY);
  if (!current) return;
  if (!sessionId || current === sessionId) {
    localStorage.removeItem(RUNNING_SESSION_KEY);
  }
}

export function getRunningSessionId(): string | null {
  return localStorage.getItem(RUNNING_SESSION_KEY);
}

export function isSessionRunning(sessionId?: string | null): boolean {
  if (!sessionId) return false;
  return getRunningSessionId() === sessionId;
}
