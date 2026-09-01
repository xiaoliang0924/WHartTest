<template>
  <div class="data-generation-layout">
    <a-tabs v-model:active-key="activeTab" type="rounded">
      <a-tab-pane key="plans" title="造数计划">
        <DataGenerationPlanView />
      </a-tab-pane>
      <a-tab-pane key="runs" title="执行记录">
        <DataGenerationRunView />
      </a-tab-pane>
    </a-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import DataGenerationPlanView from '@/features/data-generation/views/DataGenerationPlanView.vue';
import DataGenerationRunView from '@/features/data-generation/views/DataGenerationRunView.vue';

const route = useRoute();
const activeTab = ref('plans');

watch(
  () => route.query.tab,
  (tab) => {
    activeTab.value = tab === 'runs' ? 'runs' : 'plans';
  },
  { immediate: true },
);
</script>

<style scoped>
.data-generation-layout {
  padding: 0;
}
</style>
