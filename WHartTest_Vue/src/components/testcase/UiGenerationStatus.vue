<template>
  <a-alert v-if="visible" class="generation-alert" :type="alertType" show-icon>
    <template #title>{{ isEnglish ? 'UI test generation' : 'UI 自动化用例生成' }}</template>
    <div class="generation-summary-row">
      <div class="generation-message">
        {{ record.ui_generation?.message || (isEnglish ? 'No generation result has been saved.' : '本次尚未保存 UI 用例生成结果。') }}
      </div>
      <a-space class="generation-actions">
      <a-button v-if="canRetry" size="small" :loading="busy" @click="manage('retry')">
        {{ isEnglish ? 'Retry from execution record' : '使用执行记录重试生成' }}
      </a-button>
      <a-button v-if="active && record.ui_generation?.status !== 'stopping'" size="small" :loading="busy" @click="manage('stop')">
        {{ isEnglish ? 'Stop generation' : '停止生成' }}
      </a-button>
      </a-space>
    </div>
    <details v-if="record.ui_generation?.reason" class="generation-details">
      <summary>{{ isEnglish ? 'View technical details' : '查看详细日志' }}</summary>
      <pre class="generation-reason">{{ record.ui_generation.reason }}</pre>
    </details>
  </a-alert>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import { Message } from '@arco-design/web-vue';
import { useAppI18n } from '@/composables/useAppI18n';
import { isUiGenerationActive, manageUiGeneration, type TestCaseRunRecord } from '@/services/testcaseService';

const props = defineProps<{ record: TestCaseRunRecord; projectId: number }>();
const emit = defineEmits<{ (e: 'updated', value: TestCaseRunRecord): void }>();
const { isEnglish } = useAppI18n();
const busy = ref(false);
const visible = computed(() => props.record.status === 'pass' && props.record.generate_playwright_script
  && (!!props.record.ui_generation?.status
    || !props.record.summary.includes('### UI 自动化用例生成')
    || /### UI 自动化用例生成\s*(?:失败|已生成但需要修正)/.test(props.record.summary)));
const active = computed(() => isUiGenerationActive(props.record.ui_generation));
const canRetry = computed(() => !active.value && props.record.ui_generation?.status !== 'success');
const alertType = computed(() => props.record.ui_generation?.status === 'success' ? 'success'
  : active.value ? 'info' : 'warning');
const manage = async (action: 'retry' | 'stop') => {
  if (busy.value) return;
  busy.value = true;
  try {
    const result = await manageUiGeneration(props.projectId, props.record.testcase, props.record.id, action);
    if (result.success && result.data) emit('updated', result.data);
    else Message.error(result.error || '操作失败');
  } finally {
    busy.value = false;
  }
};
</script>

<style scoped>
.generation-summary-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.generation-message { min-width: 0; line-height: 1.45; overflow-wrap: anywhere; }
.generation-actions { flex: 0 0 auto; }
.generation-details { margin-top: 6px; font-size: 12px; color: var(--color-text-3); }
.generation-details > summary { width: fit-content; cursor: pointer; user-select: none; }
.generation-details[open] > summary { margin-bottom: 6px; }
.generation-reason {
  max-height: 180px;
  overflow: auto;
  margin: 0;
  padding: 8px 10px;
  border-radius: 4px;
  background: rgba(29, 33, 41, 0.04);
  color: var(--color-text-2);
  font: inherit;
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.generation-alert :deep(.arco-alert-content) { min-width: 0; }
.generation-alert :deep(.arco-alert-title) { margin-bottom: 4px; }
@media (max-width: 760px) {
  .generation-summary-row { align-items: flex-start; flex-direction: column; gap: 8px; }
}
</style>
