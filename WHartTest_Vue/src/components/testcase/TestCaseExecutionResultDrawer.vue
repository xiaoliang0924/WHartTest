<template>
  <a-drawer
    v-model:visible="drawerVisible"
    :width="860"
    :footer="false"
    unmount-on-close
    @cancel="handleClose"
  >
    <template #title>
      {{ text.title }} · {{ testCase?.name || '-' }}
    </template>

    <div v-if="!testCase" class="empty-wrap">
      <a-empty :description="text.noCase" />
    </div>
    <div v-else class="execution-drawer">
      <div class="status-bar">
        <a-tag :color="statusColor">{{ statusLabel }}</a-tag>
        <span v-if="record?.started_at" class="meta">{{ text.startedAt }}：{{ formatTime(record.started_at) }}</span>
        <span v-if="record?.completed_at" class="meta">{{ text.completedAt }}：{{ formatTime(record.completed_at) }}</span>
        <a-spin v-if="isRunning" size="small" />
      </div>

      <a-alert v-if="streamError" type="error" :title="streamError" show-icon class="block" />
      <a-alert v-if="isRunning" type="info" :title="text.runningHint" show-icon class="block" />

      <a-card v-if="record?.summary" :title="text.summary" size="small" class="block">
        <div class="summary-text">{{ record.summary }}</div>
      </a-card>

      <a-card :title="text.stepResults" size="small" class="block">
        <a-table
          v-if="displaySteps.length > 0"
          :data="displaySteps"
          :columns="stepColumns"
          :pagination="false"
          row-key="key"
          size="small"
        >
          <template #status="{ record: step }">
            <a-tag :color="stepStatusColor(step.status)">{{ stepStatusLabel(step.status) }}</a-tag>
          </template>
        </a-table>
        <a-empty v-else :description="isRunning ? text.waitingSteps : text.noSteps" />
      </a-card>

      <a-card v-if="screenshots.length > 0" :title="text.screenshots" size="small" class="block">
        <a-image-preview-group infinite>
          <a-space wrap>
            <a-image
              v-for="shot in screenshots"
              :key="shot.id"
              :src="shot.screenshot_url || shot.screenshot"
              width="120"
              height="90"
              fit="cover"
            />
          </a-space>
        </a-image-preview-group>
      </a-card>

      <div class="footer-actions">
        <a-button v-if="sessionId" type="outline" @click="openChat">{{ text.openChat }}</a-button>
        <a-button type="primary" @click="refreshRecord" :loading="loading">{{ text.refresh }}</a-button>
      </div>
    </div>
  </a-drawer>
</template>

<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue';
import { useRouter } from 'vue-router';
import type { TableColumnData } from '@arco-design/web-vue';
import {
  getLatestTestCaseRunRecord,
  getTestCaseScreenshots,
  type TestCase,
  type TestCaseRunRecord,
  type TestCaseRunStepResult,
  type TestCaseScreenshot,
} from '@/services/testcaseService';
import { useAppI18n } from '@/composables/useAppI18n';
import { openLangGraphChatInNewWindow } from '@/features/langgraph/utils/openLangGraphChat';

const props = defineProps<{
  visible: boolean;
  projectId: number | null;
  testCase: TestCase | null;
  sessionId?: string | null;
}>();

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void;
  (e: 'finished'): void;
}>();

const router = useRouter();
const { isEnglish } = useAppI18n();

const record = ref<TestCaseRunRecord | null>(null);
const screenshots = ref<TestCaseScreenshot[]>([]);
const loading = ref(false);
const streamError = ref('');
let pollTimer: ReturnType<typeof setInterval> | null = null;

const drawerVisible = computed({
  get: () => props.visible,
  set: (value: boolean) => emit('update:visible', value),
});

const text = computed(() => (
  isEnglish.value
    ? {
        title: 'Execution Result',
        noCase: 'No test case selected',
        startedAt: 'Started',
        completedAt: 'Completed',
        runningHint: 'Execution in progress. Results will refresh automatically.',
        summary: 'Summary',
        stepResults: 'Step Results',
        waitingSteps: 'Waiting for execution results...',
        noSteps: 'No structured step results',
        screenshots: 'Screenshots',
        openChat: 'Open in LLM Chat',
        refresh: 'Refresh',
        step: 'Step',
        description: 'Description',
        expected: 'Expected',
        actual: 'Actual',
        status: 'Status',
        running: 'Running',
        pass: 'Pass',
        fail: 'Fail',
        error: 'Error',
        stopped: 'Stopped',
        unknown: 'Unknown',
      }
    : {
        title: '执行结果',
        noCase: '未选择测试用例',
        startedAt: '开始时间',
        completedAt: '完成时间',
        runningHint: '用例正在执行中，结果将自动刷新。',
        summary: '结果摘要',
        stepResults: '步骤结果',
        waitingSteps: '等待执行结果...',
        noSteps: '暂无结构化步骤结果',
        screenshots: '执行截图',
        openChat: '在 LLM 对话中查看',
        refresh: '刷新',
        step: '步骤',
        description: '描述',
        expected: '预期结果',
        actual: '实际结果',
        status: '状态',
        running: '执行中',
        pass: '通过',
        fail: '失败',
        error: '错误',
        stopped: '已停止',
        unknown: '未知',
      }
));

const isRunning = computed(() => record.value?.status === 'running');

const statusLabel = computed(() => {
  const status = record.value?.status;
  if (!status) return text.value.unknown;
  const map: Record<string, string> = {
    running: text.value.running,
    pass: text.value.pass,
    fail: text.value.fail,
    error: text.value.error,
    stopped: text.value.stopped,
  };
  return map[status] || text.value.unknown;
});

const statusColor = computed(() => {
  const status = record.value?.status;
  return ({ running: 'arcoblue', pass: 'green', fail: 'red', error: 'orangered', stopped: 'gray' } as Record<string, string>)[status || ''] || 'gray';
});

const stepColumns = computed<TableColumnData[]>(() => [
  { title: text.value.step, dataIndex: 'step_number', width: 70, align: 'center' },
  { title: text.value.description, dataIndex: 'description', ellipsis: true, tooltip: true },
  { title: text.value.expected, dataIndex: 'expected_result', ellipsis: true, tooltip: true },
  { title: text.value.actual, dataIndex: 'actual_result', ellipsis: true, tooltip: true },
  { title: text.value.status, slotName: 'status', width: 90, align: 'center' },
]);

const displaySteps = computed(() => {
  const steps = record.value?.step_results || [];
  return steps.map((step: TestCaseRunStepResult, index: number) => ({
    key: `${step.step_number || index + 1}-${index}`,
    step_number: step.step_number || index + 1,
    description: step.description || '-',
    expected_result: step.expected_result || '-',
    actual_result: step.actual_result || step.error || '-',
    status: step.status || 'unknown',
  }));
});

const stepStatusLabel = (status?: string) => {
  if (status === 'pass') return text.value.pass;
  if (status === 'fail') return text.value.fail;
  return text.value.unknown;
};

const stepStatusColor = (status?: string) => (status === 'pass' ? 'green' : status === 'fail' ? 'red' : 'gray');

const formatTime = (value: string) => {
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
};

const stopPolling = () => {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
};

const loadScreenshots = async () => {
  if (!props.projectId || !props.testCase) return;
  const response = await getTestCaseScreenshots(props.projectId, props.testCase.id);
  if (response.success && response.data) {
    screenshots.value = response.data;
  }
};

const refreshRecord = async () => {
  if (!props.projectId || !props.testCase) return;
  loading.value = true;
  try {
    const response = await getLatestTestCaseRunRecord(
      props.projectId,
      props.testCase.id,
      props.sessionId || undefined
    );
    if (response.success && response.data) {
      record.value = response.data;
      if (response.data.status !== 'running') {
        stopPolling();
        emit('finished');
      }
    }
    await loadScreenshots();
  } finally {
    loading.value = false;
  }
};

const startPolling = () => {
  stopPolling();
  pollTimer = setInterval(() => {
    if (isRunning.value) {
      refreshRecord();
    } else {
      stopPolling();
    }
  }, 2500);
};

watch(
  () => [props.visible, props.projectId, props.testCase?.id, props.sessionId] as const,
  async ([visible]) => {
    if (!visible) {
      stopPolling();
      return;
    }
    streamError.value = '';
    record.value = props.testCase?.latest_run || null;
    await refreshRecord();
    if (isRunning.value) {
      startPolling();
    }
  },
  { immediate: true }
);

onBeforeUnmount(() => stopPolling());

const handleClose = () => {
  drawerVisible.value = false;
};

const openChat = () => {
  openLangGraphChatInNewWindow(router, props.sessionId, props.projectId);
};

defineExpose({
  setStreamError(message: string) {
    streamError.value = message;
  },
  async markCompleted() {
    await refreshRecord();
    emit('finished');
  },
});
</script>

<style scoped>
.execution-drawer {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.status-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
}

.meta {
  color: var(--color-text-3);
  font-size: 12px;
}

.block {
  width: 100%;
}

.summary-text {
  white-space: pre-wrap;
  line-height: 1.6;
}

.footer-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 8px;
}

.empty-wrap {
  padding: 48px 0;
}
</style>
