<template>
  <a-drawer
    v-model:visible="drawerVisible"
    :width="860"
    :footer="false"
    :closable="false"
    :mask-closable="true"
    unmount-on-close
    @cancel="handleClose"
  >
    <template #title>
      <div class="drawer-title">
        <span class="drawer-title-text">{{ text.title }} · {{ testCase?.name || '-' }}</span>
        <a-button type="text" class="drawer-close-btn" :aria-label="text.close" @click="handleClose">
          <template #icon><icon-close /></template>
        </a-button>
      </div>
    </template>

    <div v-if="!testCase" class="empty-wrap">
      <a-empty :description="text.noCase" />
    </div>
    <div v-else class="execution-drawer">
      <div class="status-bar">
        <div class="status-meta">
          <a-tag :color="statusColor">{{ statusLabel }}</a-tag>
          <span v-if="record?.started_at" class="meta">{{ text.startedAt }}：{{ formatTime(record.started_at) }}</span>
          <span v-if="record?.completed_at" class="meta">{{ text.completedAt }}：{{ formatTime(record.completed_at) }}</span>
          <span v-if="record?.execution_mode" class="meta">{{ text.selectedMode }}：{{ executionModeLabel(record.execution_mode) }}</span>
          <span v-if="record" class="meta">{{ text.actualPath }}：{{ actualPathLabel(record.actual_execution_path) }}</span>
          <a-spin v-if="isRunning" size="small" />
        </div>
        <a-space class="top-actions" :size="8">
          <a-button
            type="outline"
            :disabled="!effectiveSessionId"
            @click="openChatInCurrentPage"
          >
            {{ text.openChatCurrentPage }}
          </a-button>
          <a-button
            type="primary"
            :disabled="!effectiveSessionId"
            @click="openChatInNewWindow"
          >
            {{ text.openChatNewWindow }}
          </a-button>
        </a-space>
      </div>

      <a-alert v-if="streamError" type="error" :title="streamError" show-icon class="block" />
      <a-alert
        v-if="record?.status === 'fail' || record?.status === 'error'"
        type="error"
        :title="text.failedHint"
        show-icon
        class="block"
      >
        <div class="failure-alert-content">
          <div>{{ failReasonText }}</div>
          <div v-if="failReasonItems.length || failReasonDetail || failSuggestionText" class="failure-alert-extra">
            <a-popover
              v-if="failReasonItems.length || failReasonDetail"
              trigger="hover"
              position="top"
            >
              <a-button type="text" size="mini" class="actual-result-detail-trigger">
                {{ text.viewDetails }}
              </a-button>
              <template #content>
                <div class="actual-result-popover">
                  <div v-if="failReasonItems.length" class="actual-result-detail-title">
                    {{ text.detectedFields }}（{{ failReasonItems.length }}）
                  </div>
                  <div v-if="failReasonItems.length" class="actual-result-items">
                    <span v-for="(item, index) in failReasonItems" :key="`${index}-${item}`">{{ item }}</span>
                  </div>
                  <p v-if="failReasonDetail" class="actual-result-detail-text">{{ failReasonDetail }}</p>
                </div>
              </template>
            </a-popover>
            <div v-if="failSuggestionText" class="failure-suggestion">
              <strong>{{ text.recommendation }}：</strong>{{ failSuggestionText }}
            </div>
          </div>
        </div>
      </a-alert>
      <a-alert v-if="isRunning" type="info" :title="text.runningHint" show-icon class="block" />

      <a-card v-if="record?.summary" :title="text.summary" size="small" class="block">
        <div class="summary-text" v-html="summaryHtml"></div>
      </a-card>

      <a-card v-if="record?.data_usage" :title="text.dataUsage" size="small" class="block">
        <a-alert :type="dataUsageAlertType" :title="dataUsageConclusion" show-icon />
        <p v-if="dataUsageMessage" class="usage-message">{{ dataUsageMessage }}</p>
        <ol v-if="dataUsageEvidenceItems.length" class="usage-evidence-list">
          <li v-for="(item, index) in dataUsageEvidenceItems" :key="`${index}-${item}`">{{ item }}</li>
        </ol>
        <a-collapse v-if="record.data_usage.evidence" :default-active-key="[]" :bordered="false" class="usage-raw-collapse">
          <a-collapse-item :header="text.rawEvidence" key="raw-evidence">
            <pre class="usage-raw-evidence">{{ record.data_usage.evidence }}</pre>
          </a-collapse-item>
        </a-collapse>
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
          <template #actualResult="{ record: step }">
            <div class="actual-result-cell">
              <span>{{ step.actual_result_summary }}</span>
              <a-popover
                v-if="step.actual_result_items.length || step.actual_result_detail"
                trigger="hover"
                position="top"
              >
                <a-button type="text" size="mini" class="actual-result-detail-trigger">
                  {{ text.viewDetails }}
                </a-button>
                <template #content>
                  <div class="actual-result-popover">
                    <div v-if="step.actual_result_items.length" class="actual-result-detail-title">
                      {{ text.detectedFields }}（{{ step.actual_result_items.length }}）
                    </div>
                    <div v-if="step.actual_result_items.length" class="actual-result-items">
                      <span v-for="(item, index) in step.actual_result_items" :key="`${index}-${item}`">{{ item }}</span>
                    </div>
                    <p v-if="step.actual_result_detail" class="actual-result-detail-text">
                      {{ step.actual_result_detail }}
                    </p>
                  </div>
                </template>
              </a-popover>
            </div>
          </template>
        </a-table>
        <a-empty v-else :description="isRunning ? text.waitingSteps : isPreDataBlocked ? text.preDataNoSteps : text.noSteps" />
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
        <a-button @click="handleClose">{{ text.close }}</a-button>
        <a-button type="primary" @click="refreshRecord" :loading="loading">{{ text.refresh }}</a-button>
      </div>
    </div>
  </a-drawer>
</template>

<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue';
import { useRouter } from 'vue-router';
import type { TableColumnData } from '@arco-design/web-vue';
import DOMPurify from 'dompurify';
import { marked } from 'marked';
import { IconClose } from '@arco-design/web-vue/es/icon';
import {
  getLatestTestCaseRunRecord,
  getTestCaseScreenshots,
  type TestCase,
  type TestCaseRunRecord,
  type TestCaseRunStepResult,
  type TestCaseScreenshot,
} from '@/services/testcaseService';
import { useAppI18n } from '@/composables/useAppI18n';
import {
  openLangGraphChatInCurrentPage,
  openLangGraphChatInNewWindow,
} from '@/features/langgraph/utils/openLangGraphChat';
import { markDeferExecStreamToChat } from '@/features/langgraph/utils/execCaseStreamCoordinator';

const props = defineProps<{
  visible: boolean;
  projectId: number | null;
  testCase: TestCase | null;
  sessionId?: string | null;
  preparing?: boolean;
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
        selectedMode: 'Selected mode',
        actualPath: 'Actual path',
        executionModes: { hybrid: 'Smart dual-mode', script_only: 'UI script only', ai_only: 'AI exploration only' },
        executionPaths: { hybrid_script_pending: 'Hybrid: trying bound UI script', agent_browser: 'AI Agent browser execution', bound_ui_script: 'Bound UI script execution', agent_fallback: 'UI script failed; AI Agent took over', agent_no_bound_script: 'No bound script; AI Agent execution', unknown: 'Not recorded' },
        runningHint: 'Execution in progress. Results will refresh automatically.',
        failedHint: 'Execution failed',
        summary: 'Summary',
        dataUsage: 'Generated Data Usage',
        dataIdentifier: 'Matched identifier',
        dataConclusion: 'Conclusion',
        dataConclusionVerified: 'Generated data use was confirmed.',
        dataConclusionReferenced: 'The log mentions generated data, but actual use was not confirmed.',
        dataConclusionUnknown: 'Generated data use could not be confirmed.',
        dataConclusionNotApplicable: 'No generated data was used in this run.',
        rawEvidence: 'View raw execution log',
        rowVisible: 'Generated record was visible in the list',
        rowNotVisible: 'Generated record was not visible in the list',
        dialogOpened: 'The expected dialog opened',
        dialogNotOpened: 'The expected dialog did not open',
        evidenceUnavailable: 'No readable page operation evidence was found.',
        resultPass: 'Execution result',
        resultFail: 'Execution failure',
        stepResults: 'Step Results',
        waitingSteps: 'Waiting for execution results...',
        noSteps: 'No structured step results',
        preDataNoSteps: 'Prerequisite data failed; no test steps ran',
        preparing: 'Preparing test data',
        preDataFailed: 'Prerequisite data failed',
        screenshots: 'Screenshots',
        openChat: 'Open in LLM Chat',
        openChatCurrentPage: 'Open on this page',
        openChatNewWindow: 'Open in new window',
        refresh: 'Refresh',
        close: 'Close',
        step: 'Step',
        description: 'Description',
        expected: 'Expected',
        actual: 'Actual',
        viewDetails: 'Details',
        detectedFields: 'Detected fields',
        recommendation: 'Suggestion',
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
        selectedMode: '选择模式',
        actualPath: '实际执行路径',
        executionModes: { hybrid: '智能双模', script_only: '仅 UI 自动化脚本', ai_only: '纯 AI 探索' },
        executionPaths: { hybrid_script_pending: '智能双模：优先尝试绑定 UI 脚本', agent_browser: 'AI Agent 浏览器执行', bound_ui_script: '绑定的 UI 脚本执行', agent_fallback: 'UI 脚本失败，已由 AI Agent 接管', agent_no_bound_script: '未绑定 UI 脚本，由 AI Agent 执行', unknown: '历史记录未记录' },
        runningHint: '用例正在执行中，结果将自动刷新。',
        failedHint: '用例执行失败',
        summary: '结果摘要',
        dataUsage: '造数数据使用情况',
        dataIdentifier: '匹配标识',
        dataConclusion: '结论',
        dataConclusionVerified: '已确认使用本次造数数据。',
        dataConclusionReferenced: '日志提到了造数标识，但还未确认实际使用。',
        dataConclusionUnknown: '暂时无法确认是否使用了本次造数数据。',
        dataConclusionNotApplicable: '本次执行未使用自动造数数据。',
        rawEvidence: '查看原始执行日志',
        rowVisible: '本次造数记录已在列表中显示',
        rowNotVisible: '列表中未找到本次造数记录',
        dialogOpened: '目标弹窗已打开',
        dialogNotOpened: '目标弹窗未打开',
        evidenceUnavailable: '没有提取到可读的页面操作记录。',
        resultPass: '执行结果',
        resultFail: '执行失败',
        stepResults: '步骤结果',
        waitingSteps: '等待执行结果...',
        noSteps: '暂无结构化步骤结果',
        preDataNoSteps: '前置数据准备失败，尚未执行任何测试步骤',
        preparing: '正在准备前置数据',
        preDataFailed: '前置数据准备失败',
        screenshots: '执行截图',
        openChat: '在 LLM 对话中查看',
        openChatCurrentPage: '当前页打开',
        openChatNewWindow: '打开新窗口',
        refresh: '刷新',
        close: '关闭',
        step: '步骤',
        description: '描述',
        expected: '预期结果',
        actual: '实际结果',
        viewDetails: '查看详情',
        detectedFields: '实际检测到的字段',
        recommendation: '建议',
        status: '状态',
        running: '执行中',
        pass: '通过',
        fail: '失败',
        error: '错误',
        stopped: '已停止',
        unknown: '未知',
      }
));

const isRunning = computed(() => record.value?.status === 'running' || (props.preparing && !record.value));
const isPreDataBlocked = computed(() => record.value?.summary?.startsWith('测试数据准备失败') || false);
const summaryHtml = computed(() => {
  const summary = record.value?.summary || '';
  if (!summary) return '';
  return DOMPurify.sanitize(marked.parse(summary) as string);
});

const dataUsageMessage = computed(() => record.value?.data_usage?.message || '');
const dataUsageConclusion = computed(() => {
  const status = record.value?.data_usage?.status;
  if (status === 'verified_used') return `${text.value.dataConclusion}：${text.value.dataConclusionVerified}`;
  if (status === 'referenced') return `${text.value.dataConclusion}：${text.value.dataConclusionReferenced}`;
  if (status === 'not_applicable') return `${text.value.dataConclusion}：${text.value.dataConclusionNotApplicable}`;
  return `${text.value.dataConclusion}：${text.value.dataConclusionUnknown}`;
});

const dataUsageEvidenceItems = computed(() => {
  const usage = record.value?.data_usage;
  if (!usage) return [];

  const items: string[] = [];
  const matchedIdentifier = usage.matched_identifier;
  if (matchedIdentifier?.value) {
    const identifierKey = isEnglish.value
      ? matchedIdentifier.key
      : ({ ticketNo: '工单号', ticketId: '工单 ID', work_order_id: '工单 ID', processingTicketId: '处理工单 ID' } as Record<string, string>)[matchedIdentifier.key]
        || matchedIdentifier.key;
    items.push(`${text.value.dataIdentifier}：${identifierKey} ${matchedIdentifier.value}`);
  }

  const evidence = (usage.evidence || '').replace(/\\r\\n|\\n/g, '\n').replace(/\r/g, '\n');
  const urls = [...evidence.matchAll(/\bURL\s*=\s*(https?:\/\/[^\s"'`,}\\]+)/gi)]
    .map((match) => {
      try {
        const parsedUrl = new URL(match[1].replace(/[\])}>.;，。]+$/, ''));
        return `${parsedUrl.origin}${parsedUrl.pathname}`;
      } catch {
        return match[1].replace(/[\])}>.;，。]+$/, '');
      }
    });
  const uniqueUrls = [...new Set(urls)];
  uniqueUrls.slice(0, 2).forEach((url) => items.push(`${isEnglish.value ? 'Page' : '页面'}：${url}`));

  const rowVisible = evidence.match(/\bROW_VISIBLE\s*=\s*(true|false)\b/i)?.[1]?.toLowerCase();
  if (rowVisible) items.push(rowVisible === 'true' ? text.value.rowVisible : text.value.rowNotVisible);

  const dialogOpened = evidence.match(/\bDIALOG\s*=\s*(true|false)\b/i)?.[1]?.toLowerCase();
  if (dialogOpened) items.push(dialogOpened === 'true' ? text.value.dialogOpened : text.value.dialogNotOpened);

  const results = [...evidence.matchAll(/\bRESULT\s*=\s*(PASS|FAIL)\s*:\s*([^\n]+)/gi)];
  const uniqueResults = [...new Set(results.map((match) => {
    const description = match[2]
      .replace(/\\[nrt]/g, ' ')
      .replace(/["'}\],]+\s*$/, '')
      .replace(/\s+/g, ' ')
      .trim();
    const label = match[1].toUpperCase() === 'PASS' ? text.value.resultPass : text.value.resultFail;
    return description ? `${label}：${description}` : label;
  }))];
  uniqueResults.slice(0, 3).forEach((result) => items.push(result));

  if (items.length === 0) items.push(text.value.evidenceUnavailable);
  return items;
});
const dataUsageAlertType = computed(() => {
  const status = record.value?.data_usage?.status;
  if (status === 'verified_used') return 'success';
  if (status === 'referenced') return 'warning';
  return 'info';
});

const executionModeLabel = (mode?: string) => {
  const labels = text.value.executionModes as Record<string, string>;
  return labels[mode || ''] || mode || text.value.executionPaths.unknown;
};

const actualPathLabel = (path?: string) => {
  const labels = text.value.executionPaths as Record<string, string>;
  return labels[path || ''] || labels.unknown;
};

const resolveSessionId = (): string | null => {
  if (props.preparing) return props.sessionId?.trim() || null;
  const candidates = [
    props.sessionId,
    record.value?.session_id,
    props.testCase?.latest_run?.session_id,
  ];
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.trim()) {
      return candidate.trim();
    }
  }
  return null;
};

const effectiveSessionId = computed(() => resolveSessionId());

const failureAnalysis = computed(() => {
  const summary = record.value?.summary || '';
  const reasonMatch = summary.match(/失败原因[:：]\s*([\s\S]+?)(?=(?:\r?\n|\\n)\s*(?:[-*]\s*)?(?:建议|###|结论)|$)/);
  const suggestionMatch = summary.match(/(?:^|\r?\n|\\n)\s*[-*]?\s*建议[:：]\s*([^\r\n]+)/);
  const rawReason = (reasonMatch?.[1] || summary)
    .replace(/\\r\\n|\\n/g, '\n')
    .replace(/\r?\n/g, ' ')
    .replace(/[#|*`]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  const formatted = formatActualResult(rawReason);
  return {
    reason: formatted.summary.slice(0, 240),
    items: formatted.items,
    detail: formatted.detail,
    suggestion: (suggestionMatch?.[1] || '').replace(/\\n/g, ' ').replace(/\s+/g, ' ').trim(),
  };
});
const failReasonText = computed(() => failureAnalysis.value.reason);
const failReasonItems = computed(() => failureAnalysis.value.items);
const failReasonDetail = computed(() => failureAnalysis.value.detail);
const failSuggestionText = computed(() => failureAnalysis.value.suggestion);

const statusLabel = computed(() => {
  if (isPreDataBlocked.value) return text.value.preDataFailed;
  if (props.preparing && !record.value) return text.value.preparing;
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
  if (isPreDataBlocked.value) return 'orangered';
  if (props.preparing && !record.value) return 'arcoblue';
  const status = record.value?.status;
  return ({ running: 'arcoblue', pass: 'green', fail: 'red', error: 'orangered', stopped: 'gray' } as Record<string, string>)[status || ''] || 'gray';
});

const stepColumns = computed<TableColumnData[]>(() => [
  { title: text.value.step, dataIndex: 'step_number', width: 70, align: 'center' },
  { title: text.value.description, dataIndex: 'description', ellipsis: true, tooltip: true },
  { title: text.value.expected, dataIndex: 'expected_result', ellipsis: true, tooltip: true },
  { title: text.value.actual, dataIndex: 'actual_result_summary', slotName: 'actualResult' },
  { title: text.value.status, slotName: 'status', width: 90, align: 'center' },
]);

const formatActualResult = (value: string) => {
  const normalized = value.replace(/\\r\\n|\\n/g, '\n').replace(/\\r/g, '\n').trim();
  const arrayMatch = normalized.match(/\[[\s\S]*\]/);
  if (arrayMatch && arrayMatch.index !== undefined) {
    const candidates = [arrayMatch[0], arrayMatch[0].replace(/\\"/g, '"').replace(/\\\\/g, '\\')];
    for (const candidate of candidates) {
      try {
        const parsed: unknown = JSON.parse(candidate);
        if (Array.isArray(parsed) && parsed.every((item) => typeof item === 'string' || typeof item === 'number')) {
          const items = parsed.map(String);
          const prefix = normalized.slice(0, arrayMatch.index)
            .replace(/(?:实际(?:结果)?|actual(?:\s+result)?)\s*[:=]\s*$/i, '')
            .replace(/[：:=|\s]+$/, '')
            .trim();
          const fieldCount = isEnglish.value ? `${items.length} fields detected` : `检测到 ${items.length} 个字段`;
          return {
            summary: prefix ? `${prefix} · ${fieldCount}` : fieldCount,
            items,
            detail: '',
          };
        }
      } catch {
        // Continue with the normalized text when the embedded value is not valid JSON.
      }
    }
  }

  const compact = normalized.replace(/\s+/g, ' ');
  const maxLength = 96;
  return {
    summary: compact.length > maxLength ? `${compact.slice(0, maxLength)}…` : compact || '-',
    items: [],
    detail: compact.length > maxLength ? compact : '',
  };
};

const displaySteps = computed(() => {
  const steps = record.value?.step_results || [];
  return steps.map((step: TestCaseRunStepResult, index: number) => {
    const actualResult = formatActualResult(step.actual_result || step.error || '-');
    return {
      key: `${step.step_number || index + 1}-${index}`,
      step_number: step.step_number || index + 1,
      description: step.description || '-',
      expected_result: step.expected_result || '-',
      actual_result_summary: actualResult.summary,
      actual_result_items: actualResult.items,
      actual_result_detail: actualResult.detail,
      status: step.status || 'unknown',
    };
  });
});

const stepStatusLabel = (status?: string) => {
  if (status === 'pass') return text.value.pass;
  if (status === 'fail') return text.value.fail;
  if (status === 'skip' || status === 'not_executed') {
    return isEnglish.value ? 'Not executed' : '未执行';
  }
  return text.value.unknown;
};

const stepStatusColor = (status?: string) => (
  status === 'pass' ? 'green' : status === 'fail' ? 'red' : 'gray'
);

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
  if (props.preparing && !props.sessionId) return;
  loading.value = true;
  try {
    const preferredSessionId = resolveSessionId() || undefined;
    let response = await getLatestTestCaseRunRecord(
      props.projectId,
      props.testCase.id,
      preferredSessionId,
    );
    if (!response.success && preferredSessionId && !props.preparing) {
      response = await getLatestTestCaseRunRecord(
        props.projectId,
        props.testCase.id,
      );
    }
    if (response.success && response.data) {
      record.value = response.data;
      if (response.data.status !== 'running') {
        stopPolling();
        emit('finished');
      }
    }
    if (!props.preparing || record.value?.status !== 'running') await loadScreenshots();
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
  () => [props.visible, props.projectId, props.testCase?.id, props.sessionId, props.preparing] as const,
  async ([visible, , testCaseId]) => {
    if (!visible) {
      stopPolling();
      return;
    }
    streamError.value = '';
    const shouldResetRecord = props.preparing
      ? !props.sessionId || record.value?.session_id !== props.sessionId
      : !record.value || record.value.testcase !== testCaseId;
    if (shouldResetRecord) {
      record.value = props.preparing ? null : props.testCase?.latest_run || null;
      if (props.preparing) screenshots.value = [];
    }
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

const openChatInCurrentPage = () => {
  const sid = effectiveSessionId.value;
  if (!sid) {
    return;
  }
  markDeferExecStreamToChat(sid);
  openLangGraphChatInCurrentPage(router, sid, props.projectId, {
    execAutostart: true,
    testCaseId: props.testCase?.id,
    executionMode: record.value?.execution_mode,
    uiTestCaseId: record.value?.ui_test_case_id_snapshot ?? props.testCase?.ui_test_case ?? null,
  });
};

const openChatInNewWindow = () => {
  const sid = effectiveSessionId.value;
  if (!sid) {
    return;
  }
  markDeferExecStreamToChat(sid);
  openLangGraphChatInNewWindow(router, sid, props.projectId, {
    execAutostart: true,
    testCaseId: props.testCase?.id,
    executionMode: record.value?.execution_mode,
    uiTestCaseId: record.value?.ui_test_case_id_snapshot ?? props.testCase?.ui_test_case ?? null,
  });
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

.usage-message {
  margin-top: 8px;
  line-height: 1.6;
  color: var(--color-text-2);
}

.usage-evidence-list {
  margin: 8px 0 0;
  padding-left: 24px;
  color: var(--color-text-1);
  line-height: 1.8;
}

.usage-evidence-list li {
  padding-left: 4px;
  overflow-wrap: anywhere;
}

.usage-raw-collapse {
  margin-top: 8px;
}

.usage-raw-evidence {
  max-height: 240px;
  margin: 0;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--color-text-2);
  font: inherit;
  font-size: 12px;
}

.status-bar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.status-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  min-width: 0;
  flex: 1;
}

.meta {
  color: var(--color-text-3);
  font-size: 12px;
}

.top-actions {
  flex-shrink: 0;
}

.block {
  width: 100%;
}

.summary-text {
  line-height: 1.6;
  min-width: 0;
  overflow-wrap: anywhere;
}

.summary-text :deep(h1),
.summary-text :deep(h2),
.summary-text :deep(h3),
.summary-text :deep(h4) {
  margin: 16px 0 8px;
  color: var(--color-text-1);
  font-weight: 600;
  line-height: 1.4;
}

.summary-text :deep(h1:first-child),
.summary-text :deep(h2:first-child),
.summary-text :deep(h3:first-child),
.summary-text :deep(h4:first-child) {
  margin-top: 0;
}

.summary-text :deep(p) {
  margin: 6px 0;
}

.summary-text :deep(ul),
.summary-text :deep(ol) {
  margin: 6px 0;
  padding-left: 22px;
}

.summary-text :deep(table) {
  display: block;
  width: 100%;
  max-width: 100%;
  margin: 10px 0 14px;
  overflow-x: auto;
  border-collapse: collapse;
  table-layout: auto;
  font-size: 13px;
}

.summary-text :deep(th),
.summary-text :deep(td) {
  min-width: 76px;
  max-width: 360px;
  padding: 8px 10px;
  border: 1px solid var(--color-border-2);
  text-align: left;
  vertical-align: top;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.summary-text :deep(th) {
  background: var(--color-fill-2);
  font-weight: 600;
}

.summary-text :deep(tr:nth-child(even) td) {
  background: var(--color-fill-1);
}

.summary-text :deep(pre),
.summary-text :deep(code) {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.summary-text :deep(pre) {
  max-height: 320px;
  overflow: auto;
  padding: 10px 12px;
  border-radius: 6px;
  background: var(--color-fill-1);
}

.actual-result-cell {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  min-width: 0;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.actual-result-detail-trigger {
  flex: 0 0 auto;
  height: auto;
  padding: 0 2px;
  line-height: 1.5;
}

.actual-result-popover {
  max-width: min(520px, 72vw);
  max-height: 320px;
  overflow: auto;
  overflow-wrap: anywhere;
  line-height: 1.6;
}

.actual-result-detail-title {
  margin-bottom: 8px;
  font-weight: 600;
}

.actual-result-items {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.actual-result-items span {
  padding: 2px 8px;
  border: 1px solid var(--color-border-2);
  border-radius: 4px;
  background: var(--color-fill-1);
  overflow-wrap: anywhere;
}

.actual-result-detail-text {
  max-width: 520px;
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.failure-alert-content {
  min-width: 0;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.failure-alert-extra {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 6px 12px;
  margin-top: 4px;
}

.failure-suggestion {
  color: var(--color-text-2);
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

.drawer-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
  min-width: 0;
}

.drawer-title-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.drawer-close-btn {
  flex-shrink: 0;
  color: var(--color-text-2);
}
</style>
