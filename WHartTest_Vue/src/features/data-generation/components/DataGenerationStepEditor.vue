<template>
  <div class="step-editor">
    <div class="step-editor-toolbar">
      <a-select v-model="newStepType" style="width: 180px" placeholder="选择步骤类型">
        <a-option v-for="opt in STEP_TYPE_OPTIONS" :key="opt.value" :value="opt.value">
          {{ opt.label }}
        </a-option>
      </a-select>
      <a-button type="primary" @click="addStep">
        <template #icon><icon-plus /></template>
        添加步骤
      </a-button>
    </div>

    <a-empty v-if="!steps.length" description="暂无步骤，请添加" />

    <a-collapse v-else :bordered="false" expand-icon-position="right">
      <a-collapse-item
        v-for="(step, index) in steps"
        :key="index"
        :header="`${index + 1}. ${step.name || stepTypeLabel(step.type)} (${stepTypeLabel(step.type)})`"
      >
        <a-form layout="vertical" size="small">
          <a-row :gutter="12">
            <a-col :span="8">
              <a-form-item label="步骤类型">
                <a-select v-model="step.type" @change="handleTypeChange(step)">
                  <a-option v-for="opt in STEP_TYPE_OPTIONS" :key="opt.value" :value="opt.value">
                    {{ opt.label }}
                  </a-option>
                </a-select>
              </a-form-item>
            </a-col>
            <a-col :span="10">
              <a-form-item label="步骤名称">
                <a-input v-model="step.name" placeholder="步骤名称" />
              </a-form-item>
            </a-col>
            <a-col :span="6" class="step-actions">
              <a-space>
                <a-button size="mini" :disabled="index === 0" @click="moveStep(index, -1)">上移</a-button>
                <a-button size="mini" :disabled="index === steps.length - 1" @click="moveStep(index, 1)">下移</a-button>
                <a-button size="mini" status="danger" @click="removeStep(index)">删除</a-button>
              </a-space>
            </a-col>
          </a-row>

          <template v-if="step.type === 'api_call'">
            <a-row :gutter="12">
              <a-col :span="8">
                <a-form-item label="接口 ID">
                  <a-input-number v-model="step.interface_id" :min="1" style="width: 100%" />
                </a-form-item>
              </a-col>
              <a-col :span="8">
                <a-form-item label="环境 ID">
                  <a-input-number v-model="step.environment_id" :min="1" style="width: 100%" />
                </a-form-item>
              </a-col>
            </a-row>
            <a-form-item label="请求变量 (JSON)">
              <a-textarea v-model="stepJsonFields[index].variables" :auto-size="{ minRows: 3, maxRows: 8 }" />
            </a-form-item>
            <a-form-item label="提取映射 extract (JSON)">
              <a-textarea v-model="stepJsonFields[index].extract" :auto-size="{ minRows: 3, maxRows: 8 }" />
            </a-form-item>
          </template>

          <template v-else-if="step.type === 'set_env_var'">
            <a-form-item label="环境 ID">
              <a-input-number v-model="step.environment_id" :min="1" style="width: 100%" />
            </a-form-item>
            <a-form-item label="变量 variables (JSON)">
              <a-textarea v-model="stepJsonFields[index].variables" :auto-size="{ minRows: 3, maxRows: 8 }" />
            </a-form-item>
          </template>

          <template v-else-if="step.type === 'set_public_data'">
            <a-form-item label="items (JSON)">
              <a-textarea v-model="stepJsonFields[index].items" :auto-size="{ minRows: 4, maxRows: 10 }" />
            </a-form-item>
          </template>

          <template v-else-if="step.type === 'sql'">
            <a-row :gutter="12">
              <a-col :span="8">
                <a-form-item label="数据库配置 ID">
                  <a-input-number v-model="step.database_config_id" :min="1" style="width: 100%" />
                </a-form-item>
              </a-col>
              <a-col :span="8">
                <a-form-item label="方法">
                  <a-select v-model="step.method">
                    <a-option value="fetchone">fetchone</a-option>
                    <a-option value="fetchall">fetchall</a-option>
                    <a-option value="insert">insert</a-option>
                    <a-option value="update">update</a-option>
                    <a-option value="delete">delete</a-option>
                  </a-select>
                </a-form-item>
              </a-col>
              <a-col :span="8">
                <a-form-item label="输出变量">
                  <a-input v-model="step.output_var" placeholder="可选" />
                </a-form-item>
              </a-col>
            </a-row>
            <a-form-item label="SQL">
              <a-textarea v-model="step.sql" :auto-size="{ minRows: 3, maxRows: 8 }" />
            </a-form-item>
          </template>

          <template v-else-if="step.type === 'custom_function'">
            <a-row :gutter="12">
              <a-col :span="8">
                <a-form-item label="函数 ID">
                  <a-input-number v-model="step.function_id" :min="1" style="width: 100%" />
                </a-form-item>
              </a-col>
              <a-col :span="8">
                <a-form-item label="输出变量">
                  <a-input v-model="step.output_var" placeholder="可选" />
                </a-form-item>
              </a-col>
            </a-row>
            <a-form-item label="参数 args (JSON)">
              <a-textarea v-model="stepJsonFields[index].args" :auto-size="{ minRows: 3, maxRows: 8 }" />
            </a-form-item>
          </template>

          <template v-else-if="step.type === 'delay'">
            <a-form-item label="等待秒数">
              <a-input-number v-model="step.seconds" :min="0" :max="300" style="width: 100%" />
            </a-form-item>
          </template>
        </a-form>
      </a-collapse-item>
    </a-collapse>
  </div>
</template>

<script setup lang="ts">
import { nextTick, reactive, ref, watch } from 'vue';
import { IconPlus } from '@arco-design/web-vue/es/icon';
import {
  STEP_TYPE_OPTIONS,
  type DataGenerationStep,
  type DataGenerationStepType,
} from '@/features/data-generation/services/dataGenerationService';

const props = defineProps<{
  modelValue: DataGenerationStep[];
}>();

const emit = defineEmits<{
  (e: 'update:modelValue', value: DataGenerationStep[]): void;
}>();

const steps = reactive<DataGenerationStep[]>([]);
const stepJsonFields = reactive<Array<{ variables: string; extract: string; items: string; args: string }>>([]);
const newStepType = ref<DataGenerationStepType>('api_call');
/** 从父组件同步数据时跳过 emit，避免 watch 死循环导致页面卡死 */
const isSyncingFromParent = ref(false);

function stepTypeLabel(type: string) {
  return STEP_TYPE_OPTIONS.find((item) => item.value === type)?.label || type;
}

function defaultStep(type: DataGenerationStepType): DataGenerationStep {
  if (type === 'api_call') {
    return { type, name: '调用接口', interface_id: undefined, environment_id: undefined, variables: {}, extract: {} };
  }
  if (type === 'set_env_var') {
    return { type, name: '写入环境变量', environment_id: undefined, variables: {} };
  }
  if (type === 'set_public_data') {
    return { type, name: '写入 UI 公共数据', items: [] };
  }
  if (type === 'sql') {
    return { type, name: '执行 SQL', database_config_id: undefined, sql: '', method: 'fetchall' };
  }
  if (type === 'custom_function') {
    return { type, name: '自定义函数', function_id: undefined, args: {} };
  }
  return { type: 'delay', name: '等待', seconds: 1 };
}

function syncJsonFields() {
  while (stepJsonFields.length < steps.length) {
    stepJsonFields.push({ variables: '{}', extract: '{}', items: '[]', args: '{}' });
  }
  while (stepJsonFields.length > steps.length) {
    stepJsonFields.pop();
  }
  steps.forEach((step, index) => {
    stepJsonFields[index].variables = JSON.stringify(step.variables || {}, null, 2);
    stepJsonFields[index].extract = JSON.stringify(step.extract || {}, null, 2);
    stepJsonFields[index].items = JSON.stringify(step.items || [], null, 2);
    stepJsonFields[index].args = JSON.stringify(step.args || {}, null, 2);
  });
}

function emitSteps() {
  if (isSyncingFromParent.value) return;

  const cloned = steps.map((step, index) => {
    const next: DataGenerationStep = { ...step, type: step.type };
    if (['api_call', 'set_env_var'].includes(step.type)) {
      try {
        next.variables = JSON.parse(stepJsonFields[index].variables || '{}');
      } catch {
        next.variables = step.variables || {};
      }
    }
    if (step.type === 'api_call') {
      try {
        next.extract = JSON.parse(stepJsonFields[index].extract || '{}');
      } catch {
        next.extract = step.extract || {};
      }
    }
    if (step.type === 'set_public_data') {
      try {
        next.items = JSON.parse(stepJsonFields[index].items || '[]');
      } catch {
        next.items = step.items || [];
      }
    }
    if (step.type === 'custom_function') {
      try {
        next.args = JSON.parse(stepJsonFields[index].args || '{}');
      } catch {
        next.args = step.args || {};
      }
    }
    return next;
  });
  emit('update:modelValue', cloned);
}

function addStep() {
  steps.push(defaultStep(newStepType.value));
  syncJsonFields();
  emitSteps();
}

function removeStep(index: number) {
  steps.splice(index, 1);
  syncJsonFields();
  emitSteps();
}

function moveStep(index: number, offset: number) {
  const target = index + offset;
  if (target < 0 || target >= steps.length) return;
  const [item] = steps.splice(index, 1);
  steps.splice(target, 0, item);
  const [jsonItem] = stepJsonFields.splice(index, 1);
  stepJsonFields.splice(target, 0, jsonItem);
  emitSteps();
}

function handleTypeChange(step: DataGenerationStep) {
  Object.assign(step, defaultStep(step.type));
  syncJsonFields();
  emitSteps();
}

watch(
  () => props.modelValue,
  (value) => {
    isSyncingFromParent.value = true;
    steps.splice(0, steps.length, ...(value || []).map((item) => ({ ...item })));
    syncJsonFields();
    nextTick(() => {
      isSyncingFromParent.value = false;
    });
  },
  { immediate: true },
);

watch(stepJsonFields, emitSteps, { deep: true });
watch(steps, emitSteps, { deep: true });
</script>

<style scoped>
.step-editor-toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
}
.step-actions {
  display: flex;
  align-items: flex-end;
  padding-bottom: 8px;
}
</style>
