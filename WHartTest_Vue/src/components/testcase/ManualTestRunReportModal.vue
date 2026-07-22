<template>
  <a-modal v-model:visible="modalVisible" title="人工执行报告" :width="1100" :footer="false" unmount-on-close @cancel="handleClose">
    <div v-if="loading" class="loading-state"><a-spin size="large" tip="正在加载报告..." /></div>
    <div v-else-if="error" class="error-state"><a-result status="error" :title="error" /></div>
    <div v-else-if="report" class="report-container">
      <div class="report-header">
        <h2>{{ report.name }}</h2>
        <div class="header-meta">
          <a-tag :color="statusColor(report.status)">{{ statusText(report.status) }}</a-tag>
          <span class="meta-item">测试人员：{{ report.assignee?.username || '-' }}</span>
          <span class="meta-item">创建人：{{ report.creator?.username || '-' }}</span>
          <span class="meta-item">环境 / 版本：{{ report.environment || '-' }} / {{ report.version || '-' }}</span>
          <span v-if="report.deadline" class="meta-item">截止：{{ formatRunDate(report.deadline) }}</span>
          <span v-if="report.test_suite_name" class="meta-item">来源套件：{{ report.test_suite_name }}</span>
          <span class="meta-item">创建时间：{{ formatRunDate(report.created_at) }}</span>
        </div>
        <p v-if="report.description" class="report-desc">{{ report.description }}</p>
      </div>

      <div class="statistics-grid">
        <a-card :bordered="false" class="stat-card"><a-statistic title="用例总数" :value="report.statistics.total" /></a-card>
        <a-card :bordered="false" class="stat-card passed"><a-statistic title="通过" :value="report.statistics.passed" /></a-card>
        <a-card :bordered="false" class="stat-card failed"><a-statistic title="不通过" :value="report.statistics.failed" /></a-card>
        <a-card :bordered="false" class="stat-card blocked"><a-statistic title="阻塞" :value="report.statistics.blocked || 0" /></a-card>
        <a-card :bordered="false" class="stat-card skip"><a-statistic title="跳过" :value="report.statistics.skip || 0" /></a-card>
        <a-card :bordered="false" class="stat-card pending"><a-statistic title="待执行" :value="report.statistics.pending" /></a-card>
        <a-card :bordered="false" class="stat-card"><a-statistic title="通过率" :value="report.statistics.pass_rate" suffix="%" :precision="1" /></a-card>
      </div>

      <div class="results-toolbar">
        <a-button type="outline" :loading="exporting" @click="handleExport"><template #icon><icon-download /></template>导出 Excel</a-button>
      </div>

      <a-table :data="report.results" :columns="resultColumns" row-key="assignment_id" :pagination="false" stripe>
        <template #status="{ record }"><a-tag :color="resultColor(record.status)">{{ resultText(record.status) }}</a-tag></template>
        <template #defect="{ record }">
          <a-link v-if="record.defect_url" :href="record.defect_url" target="_blank">{{ record.defect_title || '查看缺陷' }}</a-link>
          <span v-else-if="record.defect_title">{{ record.defect_title }}</span>
          <span v-else>-</span>
        </template>
        <template #executedAt="{ record }">{{ formatRunDate(record.executed_at) }}</template>
      </a-table>
    </div>
  </a-modal>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { Message } from '@arco-design/web-vue';
import type { TableColumnData } from '@arco-design/web-vue';
import { exportManualRunExcel, getManualRunReport, type ManualTestReport } from '@/services/manualTestExecutionService';

interface Props {
  visible: boolean;
  projectId: number | null;
  runId: number | null;
  runName?: string;
}
const props = defineProps<Props>();
const emit = defineEmits<{ (e: 'update:visible', value: boolean): void }>();

const loading = ref(false);
const exporting = ref(false);
const error = ref('');
const report = ref<ManualTestReport | null>(null);

const modalVisible = computed({
  get: () => props.visible,
  set: (value) => emit('update:visible', value),
});

const resultColumns: TableColumnData[] = [
  { title: '用例', dataIndex: 'testcase_name' },
  { title: '模块', dataIndex: 'module_name', width: 160 },
  { title: '结果', slotName: 'status', width: 100 },
  { title: '失败原因', dataIndex: 'failure_reason', ellipsis: true, tooltip: true },
  { title: '关联缺陷', slotName: 'defect', width: 140, ellipsis: true, tooltip: true },
  { title: '执行备注', dataIndex: 'comment', ellipsis: true, tooltip: true },
  { title: '执行时间', slotName: 'executedAt', width: 170 },
];

const statusText = (s: string) => ({ pending: '待执行', in_progress: '执行中', completed: '已完成' }[s] || s);
const statusColor = (s: string) => ({ pending: 'gray', in_progress: 'arcoblue', completed: 'green' }[s] || 'gray');
const resultText = (s: string) => ({ pending: '待执行', pass: '通过', fail: '不通过', blocked: '阻塞', skip: '跳过' }[s] || s);
const resultColor = (s: string) => ({ pending: 'gray', pass: 'green', fail: 'red', blocked: 'orangered', skip: 'arcoblue' }[s] || 'gray');

function formatRunDate(value?: string) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${date.getFullYear()}/${date.getMonth() + 1}/${date.getDate()} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}:${String(date.getSeconds()).padStart(2, '0')}`;
}

async function fetchReport() {
  if (!props.projectId || !props.runId) return;
  loading.value = true;
  error.value = '';
  try {
    report.value = await getManualRunReport(props.projectId, props.runId);
  } catch (e: any) {
    error.value = e.response?.data?.message || e.response?.data?.error || '加载报告失败';
  } finally {
    loading.value = false;
  }
}

async function handleExport() {
  if (!props.projectId || !props.runId) return;
  exporting.value = true;
  try {
    await exportManualRunExcel(props.projectId, props.runId, props.runName || `manual_run_${props.runId}`);
    Message.success('导出成功');
  } catch {
    Message.error('导出失败');
  } finally {
    exporting.value = false;
  }
}

function handleClose() {
  modalVisible.value = false;
}

watch(() => props.visible, (visible) => {
  if (visible) fetchReport();
  else report.value = null;
});
</script>

<style scoped>
.report-container { padding: 4px; }
.loading-state, .error-state { display: flex; justify-content: center; align-items: center; min-height: 320px; }
.report-header { margin-bottom: 20px; }
.report-header h2 { margin: 0 0 8px; font-size: 22px; }
.header-meta { display: flex; flex-wrap: wrap; gap: 12px; color: var(--color-text-3); font-size: 13px; }
.report-desc { margin: 10px 0 0; color: var(--color-text-2); }
.statistics-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 16px; }
.stat-card.passed :deep(.arco-statistic-value) { color: #00b42a; }
.stat-card.failed :deep(.arco-statistic-value) { color: #f53f3f; }
.stat-card.pending :deep(.arco-statistic-value) { color: #86909c; }
.results-toolbar { display: flex; justify-content: flex-end; margin-bottom: 12px; }
@media (max-width: 900px) {
  .statistics-grid { grid-template-columns: repeat(2, 1fr); }
}
</style>
