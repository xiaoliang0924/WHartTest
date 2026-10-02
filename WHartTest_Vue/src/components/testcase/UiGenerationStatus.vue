<template>
  <a-alert v-if="visible" :type="alertType" show-icon>
    <template #title>{{ isEnglish ? 'UI test generation' : 'UI 自动化用例生成' }}</template>
    <div>{{ record.ui_generation?.message || (isEnglish ? 'No generation result has been saved.' : '本次尚未保存 UI 用例生成结果。') }}</div>
    <div v-if="record.ui_generation?.reason" class="generation-reason">{{ record.ui_generation.reason }}</div>
    <a-space class="generation-actions">
      <a-button v-if="canRetry" size="small" :loading="busy" @click="manage('retry')">
        {{ isEnglish ? 'Retry from execution record' : '使用执行记录重试生成' }}
      </a-button>
      <a-button v-if="active && record.ui_generation?.status !== 'stopping'" size="small" :loading="busy" @click="manage('stop')">
        {{ isEnglish ? 'Stop generation' : '停止生成' }}
      </a-button>
    </a-space>
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
.generation-reason { margin-top: 6px; white-space: pre-wrap; overflow-wrap: anywhere; }
.generation-actions { margin-top: 8px; }
</style>
