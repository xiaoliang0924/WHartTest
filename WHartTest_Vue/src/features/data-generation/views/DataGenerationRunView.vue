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
          <a-button type="text" size="small" @click="openDetail(record)">详情</a-button>
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
        />

        <a-divider orientation="left">输出快照</a-divider>
        <pre class="json-block">{{ formatJson(selectedRun.output_snapshot) }}</pre>

        <a-divider orientation="left">步骤日志</a-divider>
        <pre class="json-block">{{ formatJson(selectedRun.step_logs) }}</pre>
      </template>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { Message } from '@arco-design/web-vue';
import { IconRefresh } from '@arco-design/web-vue/es/icon';
import { useProjectStore } from '@/store/projectStore';
import { formatDate } from '@/utils/formatters';
import {
  getDataGenerationRuns,
  type DataGenerationRun,
} from '@/features/data-generation/services/dataGenerationService';

const projectStore = useProjectStore();
const currentProjectId = computed(() => projectStore.currentProjectId);

const loading = ref(false);
const runs = ref<DataGenerationRun[]>([]);
const statusFilter = ref<string | undefined>();
const detailVisible = ref(false);
const selectedRun = ref<DataGenerationRun | null>(null);
const pagination = ref({ current: 1, pageSize: 10, total: 0 });

const columns = [
  { title: 'ID', dataIndex: 'id', width: 80 },
  { title: '计划', dataIndex: 'plan_name' },
  { title: '状态', slotName: 'status', width: 100 },
  { title: '触发方式', slotName: 'trigger_type', width: 120 },
  { title: '触发人', dataIndex: 'triggered_by_name', width: 120 },
  { title: '开始时间', slotName: 'started_at', width: 180 },
  { title: '操作', slotName: 'operations', width: 100 },
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

function openDetail(run: DataGenerationRun) {
  selectedRun.value = run;
  detailVisible.value = true;
}

watch(currentProjectId, () => {
  pagination.value.current = 1;
  fetchRuns();
});

onMounted(fetchRuns);
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
.no-project-selected {
  padding: 48px 0;
}
</style>
