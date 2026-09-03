<template>
  <a-modal
    v-model:visible="visibleProxy"
    :title="modalTitle"
    :ok-loading="loading"
    ok-text="开始试跑"
    cancel-text="取消"
    :width="520"
    @before-ok="handleOk"
    @cancel="handleCancel"
  >
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
  </a-modal>
</template>

<script setup lang="ts">
import { computed, reactive, watch } from 'vue';
import {
  buildDefaultInputParams,
  getParamSchemaEntries,
  type DataGenerationPlan,
  type ParamSchemaField,
} from '@/features/data-generation/services/dataGenerationService';

const props = defineProps<{
  visible: boolean;
  plan: DataGenerationPlan | null;
  loading?: boolean;
}>();

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void;
  (e: 'confirm', inputParams: Record<string, unknown>): void;
}>();

const visibleProxy = computed({
  get: () => props.visible,
  set: (value: boolean) => emit('update:visible', value),
});

const modalTitle = computed(() => `试跑：${props.plan?.name || '造数计划'}`);

const paramEntries = computed(() =>
  getParamSchemaEntries(props.plan?.template_params_schema as Record<string, ParamSchemaField>),
);

const formValues = reactive<Record<string, unknown>>({});

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
  emit('update:visible', false);
}

watch(
  () => [props.visible, props.plan?.id] as const,
  ([visible]) => {
    if (visible) {
      resetFormValues();
    }
  },
);
</script>
