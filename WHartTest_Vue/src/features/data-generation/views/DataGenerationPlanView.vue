<template>
  <div class="data-generation-plan-view">
    <div v-if="!currentProjectId" class="no-project-selected">
      <a-empty description="请先选择项目" />
    </div>

    <div v-else>
      <a-alert type="info" class="page-hint">
        <strong>造数计划</strong>用于创建和维护完整计划；勾选「保存为模板」后，计划会出现在「快速造数」卡片墙供团队一键执行。
      </a-alert>

      <div class="toolbar">
        <a-space>
          <a-input-search
            v-model="searchKeyword"
            placeholder="搜索造数计划"
            allow-clear
            style="width: 280px"
            @search="fetchPlans"
            @clear="fetchPlans"
          />
          <a-radio-group v-model="planTypeFilter" type="button" @change="handlePlanTypeChange">
            <a-radio value="normal">普通计划</a-radio>
            <a-radio value="template">模板计划</a-radio>
            <a-radio value="all">全部</a-radio>
          </a-radio-group>
        </a-space>
        <a-button type="primary" @click="openCreate">
          <template #icon><icon-plus /></template>
          新建计划
        </a-button>
      </div>

      <a-table
        :columns="columns"
        :data="plans"
        :loading="loading"
        :pagination="pagination"
        row-key="id"
        @page-change="onPageChange"
        @page-size-change="onPageSizeChange"
      >
        <template #name="{ record }">
          <a-space>
            <span>{{ record.name }}</span>
            <a-tag v-if="record.is_template" color="arcoblue" size="small">模板</a-tag>
          </a-space>
        </template>
        <template #target_type="{ record }">
          <a-tag>{{ targetTypeLabel(record.target_type) }}</a-tag>
        </template>
        <template #is_active="{ record }">
          <a-tag :color="record.is_active ? 'green' : 'gray'">
            {{ record.is_active ? '启用' : '停用' }}
          </a-tag>
        </template>
        <template #updated_at="{ record }">
          {{ formatDate(record.updated_at) }}
        </template>
        <template #operations="{ record }">
          <a-space>
            <a-button type="text" size="small" @click="openEdit(record)">编辑</a-button>
            <a-button type="text" size="small" :loading="runningId === record.id" @click="openRunModal(record)">
              试跑
            </a-button>
            <a-popconfirm content="确定删除该计划？" @ok="handleDelete(record.id)">
              <a-button type="text" status="danger" size="small">删除</a-button>
            </a-popconfirm>
          </a-space>
        </template>
      </a-table>
    </div>

    <DataGenerationPlanModal
      v-model:visible="modalVisible"
      :plan="editingPlan"
      :project-id="currentProjectId!"
      @saved="fetchPlans"
    />

    <DataGenerationRunParamsModal
      v-model:visible="runModalVisible"
      :plan="runningPlan"
      :loading="runningId !== null"
      @confirm="handleRunConfirm"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { Message } from '@arco-design/web-vue';
import { IconPlus } from '@arco-design/web-vue/es/icon';
import { useProjectStore } from '@/store/projectStore';
import { formatDate } from '@/utils/formatters';
import DataGenerationPlanModal from '@/features/data-generation/components/DataGenerationPlanModal.vue';
import DataGenerationRunParamsModal from '@/features/data-generation/components/DataGenerationRunParamsModal.vue';
import {
  deleteDataGenerationPlan,
  getDataGenerationPlans,
  runDataGenerationPlan,
  validatePlanEnvironment,
  type DataGenerationPlan,
} from '@/features/data-generation/services/dataGenerationService';

const emit = defineEmits<{
  (event: 'run-completed'): void;
}>();

const projectStore = useProjectStore();
const currentProjectId = computed(() => projectStore.currentProjectId);

const loading = ref(false);
const plans = ref<DataGenerationPlan[]>([]);
const searchKeyword = ref('');
const planTypeFilter = ref<'normal' | 'template' | 'all'>('normal');
const modalVisible = ref(false);
const editingPlan = ref<DataGenerationPlan | null>(null);
const runModalVisible = ref(false);
const runningPlan = ref<DataGenerationPlan | null>(null);
const runningId = ref<number | null>(null);

const pagination = ref({ current: 1, pageSize: 10, total: 0 });

const columns = [
  { title: '计划名称', slotName: 'name' },
  { title: '目标类型', slotName: 'target_type', width: 120 },
  { title: '步骤数', dataIndex: 'step_count', width: 90 },
  { title: '默认环境', dataIndex: 'default_environment_name', width: 140 },
  { title: '状态', slotName: 'is_active', width: 90 },
  { title: '更新时间', slotName: 'updated_at', width: 180 },
  { title: '操作', slotName: 'operations', width: 220 },
];

function targetTypeLabel(value: string) {
  if (value === 'api') return 'API';
  if (value === 'ui') return 'UI';
  return 'API + UI';
}

function buildListParams() {
  const params: Record<string, unknown> = {
    search: searchKeyword.value || undefined,
    page: pagination.value.current,
    page_size: pagination.value.pageSize,
  };
  if (planTypeFilter.value === 'normal') {
    params.is_template = false;
  } else if (planTypeFilter.value === 'template') {
    params.is_template = true;
  }
  return params;
}

async function fetchPlans() {
  if (!currentProjectId.value) return;
  loading.value = true;
  try {
    const { results, count } = await getDataGenerationPlans(
      currentProjectId.value,
      buildListParams(),
    );
    plans.value = results;
    pagination.value.total = count;
  } catch (error: any) {
    Message.error(error.message || '加载造数计划失败');
  } finally {
    loading.value = false;
  }
}

function handlePlanTypeChange() {
  pagination.value.current = 1;
  fetchPlans();
}

function onPageChange(page: number) {
  pagination.value.current = page;
  fetchPlans();
}

function onPageSizeChange(size: number) {
  pagination.value.pageSize = size;
  pagination.value.current = 1;
  fetchPlans();
}

function openCreate() {
  editingPlan.value = null;
  modalVisible.value = true;
}

function openEdit(plan: DataGenerationPlan) {
  editingPlan.value = JSON.parse(JSON.stringify(plan));
  modalVisible.value = true;
}

function openRunModal(plan: DataGenerationPlan) {
  const envError = validatePlanEnvironment(plan);
  if (envError) {
    Message.warning(envError);
    return;
  }
  runningPlan.value = plan;
  runModalVisible.value = true;
}

async function handleRunConfirm(inputParams: Record<string, unknown>) {
  if (!currentProjectId.value || !runningPlan.value) return;

  runningId.value = runningPlan.value.id;
  try {
    const resp = await runDataGenerationPlan(
      currentProjectId.value,
      runningPlan.value.id,
      inputParams,
    );
    emit('run-completed');
    runModalVisible.value = false;
    if (resp.status === 'success') {
      Message.success('造数试跑成功');
    } else {
      Message.error(resp.message || '造数试跑失败');
    }
  } catch (error: any) {
    emit('run-completed');
    Message.error(error.response?.data?.message || error.message || '造数试跑失败');
  } finally {
    runningId.value = null;
  }
}

async function handleDelete(planId: number) {
  if (!currentProjectId.value) return;
  try {
    await deleteDataGenerationPlan(currentProjectId.value, planId);
    Message.success('删除成功');
    fetchPlans();
  } catch (error: any) {
    Message.error(error.message || '删除失败');
  }
}

watch(currentProjectId, () => {
  pagination.value.current = 1;
  fetchPlans();
});

onMounted(fetchPlans);
</script>

<style scoped>
.page-hint {
  margin-bottom: 16px;
}

.toolbar {
  display: flex;
  justify-content: space-between;
  margin-bottom: 16px;
}

.no-project-selected {
  padding: 48px 0;
}
</style>
