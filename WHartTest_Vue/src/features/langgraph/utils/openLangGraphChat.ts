import type { Router } from 'vue-router';

const SESSION_STORAGE_KEY = 'langgraph_session_id';
const PROJECT_STORAGE_KEY = 'selected_project_id';

/** Open LLM chat in a new tab so the current page stays on test case management. */
export function openLangGraphChatInNewWindow(
  router: Router,
  sessionId?: string | null,
  projectId?: number | null,
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

  const { href } = router.resolve({
    name: 'LangGraphChat',
    query: Object.keys(query).length > 0 ? query : undefined,
  });
  window.open(href, '_blank', 'noopener,noreferrer');
}
