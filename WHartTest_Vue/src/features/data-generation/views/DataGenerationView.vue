<template>
  <div class="data-generation-layout">
    <a-tabs v-model:active-key="activeTab" type="rounded" lazy-load>
      <a-tab-pane key="plans" title="造数计划">
        <DataGenerationPlanView @run-completed="handleRunCompleted" />
      </a-tab-pane>
      <a-tab-pane key="runs" title="执行记录">
        <DataGenerationRunView ref="runViewRef" />
      </a-tab-pane>
      <a-tab-pane key="quick" title="快速造数">
        <DataGenerationQuickView @run-completed="handleRunCompleted" />
      </a-tab-pane>
    </a-tabs>
  </div>
</template>

<script setup lang="ts">
import { nextTick, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import DataGenerationPlanView from '@/features/data-generation/views/DataGenerationPlanView.vue';
import DataGenerationRunView from '@/features/data-generation/views/DataGenerationRunView.vue';
import DataGenerationQuickView from '@/features/data-generation/views/DataGenerationQuickView.vue';

const route = useRoute();
const activeTab = ref('plans');
const runViewRef = ref<InstanceType<typeof DataGenerationRunView> | null>(null);

async function refreshRunList(resetPage = false) {
  await nextTick();
  await runViewRef.value?.refresh?.({ resetPage });
}

function handleRunCompleted() {
  refreshRunList(true);
}

watch(
  () => route.query.tab,
  (tab) => {
    if (tab === 'runs') activeTab.value = 'runs';
    else if (tab === 'quick') activeTab.value = 'quick';
    else activeTab.value = 'plans';
  },
  { immediate: true },
);

watch(activeTab, (tab) => {
  if (tab === 'runs') {
    refreshRunList(true);
  }
});
</script>

<style scoped>
.data-generation-layout {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background-color: var(--color-bg-2);
  border-radius: 8px;
  padding: 16px;
  box-sizing: border-box;
}

.data-generation-layout :deep(.arco-tabs) {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.data-generation-layout :deep(.arco-tabs-content) {
  flex: 1;
  overflow: auto;
}
</style>
