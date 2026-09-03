<template>
  <div class="data-generation-run-view">
    <div v-if="!currentProjectId" class="no-project-selected">
      <a-empty description="请先选择项目" />
    </div>

    <div v-else>
      <div class="toolbar">
        <a-select
          v-model="statusFilter"
          placeholder="状态筛选"
          allow-clear
          style="width: 160px"
          @change="handleStatusFilterChange"
          @clear="handleStatusFilterChange"
        >
          <a-option value="success">成功</a-option>
          <a-option value="failed">失败</a-option>
          <a-option value="running">执行中</a-option>
        </a-select>
        <a-button @click="fetchRuns">
          <template #icon><icon-refresh /></template>
          刷新
        </a-button>
      </div>

      <a-table
        :columns="columns"
        :data="runs"
        :loading="loading"
        :pagination="pagination"
        row-key="id"
        @page-change="onPageChange"
        @page-size-change="onPageSizeChange"
      >
        <template #status="{ record }">
          <a-tag :color="statusColor(record.status)">{{ statusLabel(record.status) }}</a-tag>
        </template>
        <template #trigger_type="{ record }">
          {{ record.trigger_type === 'suite_pre' ? '套件前置' : '手动执行' }}
        </template>
        <template #started_at="{ record }">
          {{ formatDate(record.started_at) }}
        </template>
        <template #operations="{ record }">
          <a-space>
            <a-button type="text" size="small" @click="openDetail(record)">详情</a-button>
            <a-button
              type="text"
              size="small"
              :loading="rerunningId === record.id"
              @click="handleRerun(record)"
            >
              重跑
            </a-button>
            <a-button
              type="text"
              size="small"
              :disabled="record.is_cleaned"
              :loading="cleaningId === record.id"
              @click="handleCleanup(record)"
            >
              清理
            </a-button>
          </a-space>
        </template>
      </a-table>
    </div>

    <a-modal v-model:visible="detailVisible" title="造数执行详情" :width="860" :footer="false">
      <template v-if="selectedRun">
        <a-descriptions :column="2" bordered size="small">
          <a-descriptions-item label="计划">{{ selectedRun.plan_name }}</a-descriptions-item>
          <a-descriptions-item label="状态">
            <a-tag :color="statusColor(selectedRun.status)">{{ statusLabel(selectedRun.status) }}</a-tag>
          </a-descriptions-item>
          <a-descriptions-item label="触发方式">
            {{ selectedRun.trigger_type === 'suite_pre' ? '套件前置' : '手动执行' }}
          </a-descriptions-item>
          <a-descriptions-item label="耗时">{{ selectedRun.duration ?? '-' }} 秒</a-descriptions-item>
        </a-descriptions>

        <a-alert
          v-if="selectedRun.error_message"
          type="error"
          :title="selectedRun.error_message"
          style="margin-top: 12px"
        >
          <template v-if="selectedRun.failed_step_index">
            失败步骤：第 {{ selectedRun.failed_step_index }} 步
          </template>
        </a-alert>

        <a-divider orientation="left">步骤执行</a-divider>
        <a-table
          :columns="stepLogColumns"
          :data="selectedRun.step_logs || []"
          :pagination="false"
          row-key="index"
          size="small"
          :row-class="stepRowClass"
        >
          <template #status="{ record }">
            <a-tag :color="record.status === 'failed' ? 'red' : 'green'">
              {{ record.status === 'failed' ? '失败' : '成功' }}
            </a-tag>
          </template>
          <template #detail="{ record }">
            <div v-if="record.error" class="step-error">{{ record.error }}</div>
            <div v-if="record.extracted && Object.keys(record.extracted).length" class="step-detail">
              提取：{{ formatInlineJson(record.extracted) }}
            </div>
            <div v-if="record.context_after && Object.keys(record.context_after).length" class="step-detail">
              变量：{{ formatInlineJson(record.context_after) }}
            </div>
          </template>
        </a-table>

        <a-divider orientation="left">最终输出快照</a-divider>
        <pre class="json-block">{{ formatJson(selectedRun.output_snapshot) }}</pre>

        <template v-if="selectedRun.cleanup_logs?.length || selectedRun.cleanup_status">
          <a-divider orientation="left">清理日志</a-divider>
          <a-tag :color="selectedRun.is_cleaned ? 'green' : 'orange'" style="margin-bottom: 8px">
            {{ cleanupStatusLabel(selectedRun.cleanup_status) }}
          </a-tag>
          <a-alert
            v-if="selectedRun.cleanup_error_message"
            type="error"
            :title="selectedRun.cleanup_error_message"
            style="margin-bottom: 8px"
          />
          <pre class="json-block">{{ formatJson(selectedRun.cleanup_logs) }}</pre>
        </template>
      </template>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { Message } from '@arco-design/web-vue';
import { IconRefresh } from '@arco-design/web-vue/es/icon';
import { useProjectStore } from '@/store/projectStore';
import { formatDate } from '@/utils/formatters';
import {
  cleanupDataGenerationRun,
  getDataGenerationRuns,
  rerunDataGenerationRun,
  type DataGenerationRun,
  type DataGenerationStepLog,
} from '@/features/data-generation/services/dataGenerationService';

const projectStore = useProjectStore();
const currentProjectId = computed(() => projectStore.currentProjectId);

const loading = ref(false);
const runs = ref<DataGenerationRun[]>([]);
const statusFilter = ref<string | undefined>();
const detailVisible = ref(false);
const selectedRun = ref<DataGenerationRun | null>(null);
const rerunningId = ref<number | null>(null);
const cleaningId = ref<number | null>(null);
const pagination = ref({ current: 1, pageSize: 10, total: 0 });

const columns = [
  { title: 'ID', dataIndex: 'id', width: 80 },
  { title: '计划', dataIndex: 'plan_name' },
  { title: '状态', slotName: 'status', width: 100 },
  { title: '触发方式', slotName: 'trigger_type', width: 120 },
  { title: '触发人', dataIndex: 'triggered_by_name', width: 120 },
  { title: '开始时间', slotName: 'started_at', width: 180 },
  { title: '操作', slotName: 'operations', width: 200 },
];

const stepLogColumns = [
  { title: '#', dataIndex: 'index', width: 56 },
  { title: '步骤', dataIndex: 'name' },
  { title: '类型', dataIndex: 'type', width: 120 },
  { title: '状态', slotName: 'status', width: 90 },
  { title: '结果 / 变量', slotName: 'detail' },
];

function statusColor(status: string) {
  if (status === 'success') return 'green';
  if (status === 'failed') return 'red';
  if (status === 'running') return 'blue';
  return 'gray';
}

function statusLabel(status: string) {
  const map: Record<string, string> = {
    success: '成功',
    failed: '失败',
    running: '执行中',
    pending: '等待中',
  };
  return map[status] || status;
}

function formatJson(value: unknown) {
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch {
    return String(value ?? '');
  }
}

function formatInlineJson(value: unknown) {
  try {
    return JSON.stringify(value ?? {});
  } catch {
    return String(value ?? '');
  }
}

function stepRowClass(record: DataGenerationStepLog) {
  if (record.status === 'failed') return 'step-row-failed';
  if (selectedRun.value?.failed_step_index === record.index) return 'step-row-failed';
  return '';
}

async function fetchRuns() {
  if (!currentProjectId.value) return;
  loading.value = true;
  try {
    const params: Record<string, unknown> = {
      page: pagination.value.current,
      page_size: pagination.value.pageSize,
    };
    if (statusFilter.value) {
      params.status = statusFilter.value;
    }
    const { results, count } = await getDataGenerationRuns(currentProjectId.value, params);
    runs.value = results;
    pagination.value.total = count;
  } catch (error: any) {
    Message.error(error.message || '加载执行记录失败');
  } finally {
    loading.value = false;
  }
}

async function refresh(options?: { resetPage?: boolean }) {
  if (options?.resetPage) {
    pagination.value.current = 1;
  }
  await fetchRuns();
}

defineExpose({ refresh });

function handleStatusFilterChange() {
  pagination.value.current = 1;
  fetchRuns();
}

function onPageChange(page: number) {
  pagination.value.current = page;
  fetchRuns();
}

function onPageSizeChange(size: number) {
  pagination.value.pageSize = size;
  pagination.value.current = 1;
  fetchRuns();
}

function cleanupStatusLabel(status?: string) {
  const map: Record<string, string> = {
    success: '清理成功',
    failed: '清理失败',
    skipped: '未配置清理',
  };
  return map[status || ''] || status || '-';
}

async function handleRerun(run: DataGenerationRun) {
  if (!currentProjectId.value) return;
  rerunningId.value = run.id;
  try {
    const resp = await rerunDataGenerationRun(currentProjectId.value, run.id);
    if (resp.status === 'success') {
      Message.success('重跑成功');
      fetchRuns();
    } else {
      Message.error(resp.message || '重跑失败');
    }
  } catch (error: any) {
    Message.error(error.response?.data?.message || error.message || '重跑失败');
  } finally {
    rerunningId.value = null;
  }
}

async function handleCleanup(run: DataGenerationRun) {
  if (!currentProjectId.value) return;
  cleaningId.value = run.id;
  try {
    const resp = await cleanupDataGenerationRun(currentProjectId.value, run.id);
    if (resp.status === 'success') {
      Message.success(resp.message || '清理完成');
      fetchRuns();
      if (selectedRun.value?.id === run.id) {
        selectedRun.value = { ...selectedRun.value, ...(resp.data || {}) };
      }
    } else {
      Message.error(resp.message || '清理失败');
    }
  } catch (error: any) {
    Message.error(error.response?.data?.message || error.message || '清理失败');
  } finally {
    cleaningId.value = null;
  }
}

function openDetail(run: DataGenerationRun) {
  selectedRun.value = run;
  detailVisible.value = true;
}

watch(currentProjectId, () => {
  pagination.value.current = 1;
  fetchRuns();
});
</script>

<style scoped>
.toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}
.json-block {
  background: var(--color-fill-2);
  padding: 12px;
  border-radius: 6px;
  max-height: 260px;
  overflow: auto;
  font-size: 12px;
}
.step-error {
  color: rgb(var(--red-6));
  margin-bottom: 4px;
}
.step-detail {
  color: var(--color-text-2);
  font-size: 12px;
  word-break: break-all;
}
:deep(.step-row-failed td) {
  background: rgba(var(--red-1), 0.45);
}
.no-project-selected {
  padding: 48px 0;
}
</style>
