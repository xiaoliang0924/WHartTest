import { defineStore } from 'pinia';
import { ref } from 'vue';
import { getManualTodoSummary, type ManualTodoSummary } from '@/services/manualTestExecutionService';
import { useAuthStore } from '@/store/authStore';

export const useManualTodoStore = defineStore('manualTodo', () => {
  const pendingCount = ref(0);
  const summary = ref<ManualTodoSummary | null>(null);
  const loading = ref(false);

  function applySummary(data: ManualTodoSummary) {
    summary.value = data;
    pendingCount.value = data.pending_count;
  }

  function clear() {
    pendingCount.value = 0;
    summary.value = null;
  }

  async function refresh(projectId: number | null) {
    if (!projectId) {
      clear();
      return;
    }
    const authStore = useAuthStore();
    if (!authStore.user?.id) {
      clear();
      return;
    }
    loading.value = true;
    try {
      const data = await getManualTodoSummary(projectId, { assignee_id: authStore.user.id });
      applySummary(data);
    } catch {
      clear();
    } finally {
      loading.value = false;
    }
  }

  return { pendingCount, summary, loading, applySummary, clear, refresh };
});
