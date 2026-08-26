const RUNNING_SESSION_KEY = 'langgraph_running_session';
export const RUNNING_SESSION_EVENT = 'langgraph-running-session-change';

function notifyRunningSessionChange(): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new Event(RUNNING_SESSION_EVENT));
}

export function markSessionRunning(sessionId: string): void {
  if (!sessionId) return;
  localStorage.setItem(RUNNING_SESSION_KEY, sessionId);
  notifyRunningSessionChange();
}

export function clearSessionRunning(sessionId?: string | null): void {
  const current = localStorage.getItem(RUNNING_SESSION_KEY);
  if (!current) return;
  if (!sessionId || current === sessionId) {
    localStorage.removeItem(RUNNING_SESSION_KEY);
    notifyRunningSessionChange();
  }
}

export function getRunningSessionId(): string | null {
  return localStorage.getItem(RUNNING_SESSION_KEY);
}

export function isSessionRunning(sessionId?: string | null): boolean {
  if (!sessionId) return false;
  return getRunningSessionId() === sessionId;
}
