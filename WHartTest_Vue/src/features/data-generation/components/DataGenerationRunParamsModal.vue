<template>
  <a-modal
    v-model:visible="visibleProxy"
    :title="modalTitle"
    :ok-loading="loading"
    :ok-text="progressRun ? '执行中…' : '开始试跑'"
    :ok-button-props="{ disabled: !!progressRun }"
    :cancel-text="progressRun ? '关闭' : '取消'"
    :width="560"
    :mask-closable="!progressRun"
    :closable="!progressRun"
    @before-ok="handleOk"
    @cancel="handleCancel"
  >
    <template v-if="progressRun">
      <a-alert type="info" style="margin-bottom: 12px">
        造数任务执行中，已完成 {{ completedStepCount }} / {{ totalStepCount || '?' }} 步
      </a-alert>
      <a-table
        :columns="stepColumns"
        :data="progressRun.step_logs || []"
        :pagination="false"
        size="small"
        row-key="index"
      >
        <template #status="{ record }">
          <a-tag :color="stepStatusColor(record.status)" size="small">
            {{ stepStatusLabel(record.status) }}
          </a-tag>
        </template>
      </a-table>
    </template>

    <template v-else>
      <a-alert type="info" style="margin-bottom: 12px">
        填写本次试跑参数，与「快速造数」一键执行的行为一致。
      </a-alert>

      <a-form v-if="paramEntries.length" layout="vertical" size="medium">
        <a-form-item
          v-for="[key, schema] in paramEntries"
          :key="key"
          :label="schema.label || key"
        >
          <a-input
            v-if="schema.type !== 'number'"
            :model-value="String(formValues[key] ?? '')"
            @update:model-value="(val) => updateField(key, val)"
          />
          <a-input-number
            v-else
            :model-value="Number(formValues[key] ?? 0)"
            style="width: 100%"
            @update:model-value="(val) => updateField(key, val ?? 0)"
          />
        </a-form-item>
      </a-form>
      <a-empty v-else description="该计划无需额外参数，直接试跑即可" />
    </template>
  </a-modal>
</template>

<script setup lang="ts">
import { computed, reactive, watch } from 'vue';
import {
  buildDefaultInputParams,
  getParamSchemaEntries,
  type DataGenerationPlan,
  type DataGenerationRun,
  type ParamSchemaField,
} from '@/features/data-generation/services/dataGenerationService';

const props = defineProps<{
  visible: boolean;
  plan: DataGenerationPlan | null;
  loading?: boolean;
  progressRun?: DataGenerationRun | null;
}>();

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void;
  (e: 'confirm', inputParams: Record<string, unknown>): void;
  (e: 'cancel-waiting'): void;
}>();

const visibleProxy = computed({
  get: () => props.visible,
  set: (value: boolean) => emit('update:visible', value),
});

const modalTitle = computed(() => {
  if (props.progressRun) {
    return `试跑进度：${props.plan?.name || '造数计划'}`;
  }
  return `试跑：${props.plan?.name || '造数计划'}`;
});

const paramEntries = computed(() =>
  getParamSchemaEntries(props.plan?.template_params_schema as Record<string, ParamSchemaField>),
);

const totalStepCount = computed(() => props.plan?.step_count ?? props.plan?.steps?.length ?? 0);

const completedStepCount = computed(() => (props.progressRun?.step_logs || []).length);

const stepColumns = [
  { title: '#', dataIndex: 'index', width: 48 },
  { title: '步骤', dataIndex: 'name' },
  { title: '状态', slotName: 'status', width: 110 },
];

const formValues = reactive<Record<string, unknown>>({});

function stepStatusLabel(status?: string) {
  if (status === 'failed_continued') return '失败(已忽略)';
  if (status === 'failed') return '失败';
  if (status === 'success') return '成功';
  return status || '-';
}

function stepStatusColor(status?: string) {
  if (status === 'failed_continued') return 'orange';
  if (status === 'failed') return 'red';
  if (status === 'success') return 'green';
  return 'gray';
}

function resetFormValues() {
  Object.keys(formValues).forEach((key) => delete formValues[key]);
  const defaults = buildDefaultInputParams(
    props.plan?.template_params_schema as Record<string, ParamSchemaField>,
  );
  Object.assign(formValues, defaults);
}

function updateField(key: string, value: unknown) {
  formValues[key] = value;
}

function handleOk() {
  emit('confirm', { ...formValues });
  return false;
}

function handleCancel() {
  if (props.progressRun) {
    emit('cancel-waiting');
    return;
  }
  emit('update:visible', false);
}

watch(
  () => [props.visible, props.plan?.id] as const,
  ([visible]) => {
    if (visible && !props.progressRun) {
      resetFormValues();
    }
  },
);
</script>
