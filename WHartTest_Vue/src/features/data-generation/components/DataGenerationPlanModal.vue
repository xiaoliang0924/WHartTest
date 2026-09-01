<template>
  <a-modal
    v-model:visible="visibleProxy"
    :title="isEdit ? '编辑造数计划' : '新建造数计划'"
    :width="920"
    :mask-closable="false"
    @before-ok="handleSubmit"
    @cancel="handleCancel"
  >
    <a-form ref="formRef" :model="formData" :rules="rules" layout="vertical">
      <a-row :gutter="16">
        <a-col :span="12">
          <a-form-item label="计划名称" field="name" required>
            <a-input v-model="formData.name" placeholder="例如：创建待分配工单" />
          </a-form-item>
        </a-col>
        <a-col :span="12">
          <a-form-item label="目标类型" field="target_type">
            <a-select v-model="formData.target_type">
              <a-option value="api">API</a-option>
              <a-option value="ui">UI</a-option>
              <a-option value="both">API + UI</a-option>
            </a-select>
          </a-form-item>
        </a-col>
      </a-row>

      <a-form-item label="描述" field="description">
        <a-textarea v-model="formData.description" :auto-size="{ minRows: 2, maxRows: 4 }" />
      </a-form-item>

      <a-row :gutter="16">
        <a-col :span="12">
          <a-form-item label="默认 API 环境" field="default_environment">
            <a-select
              v-model="formData.default_environment"
              placeholder="选择环境"
              allow-clear
              :loading="envLoading"
            >
              <a-option v-for="env in environments" :key="env.id" :value="env.id">
                {{ env.name }}
              </a-option>
            </a-select>
          </a-form-item>
        </a-col>
        <a-col :span="12">
          <a-form-item label="启用" field="is_active">
            <a-switch v-model="formData.is_active" />
          </a-form-item>
        </a-col>
      </a-row>

      <a-form-item label="步骤配置 (JSON)" field="steps_json" required>
        <template #extra>
          Phase 1 支持 api_call / set_env_var / set_public_data。
          api_call 需填写 interface_id，extract 使用 JMESPath（如 data.id）。
        </template>
        <a-textarea
          v-model="formData.steps_json"
          :auto-size="{ minRows: 12, maxRows: 24 }"
          placeholder="请输入步骤 JSON 数组"
        />
      </a-form-item>

      <a-space>
        <a-button size="small" @click="applyExample">填入示例</a-button>
        <a-button size="small" @click="formatStepsJson">格式化 JSON</a-button>
      </a-space>
    </a-form>
  </a-modal>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue';
import { Message } from '@arco-design/web-vue';
import { getEnvironments } from '@/features/api-testing/services/environmentService';
import {
  createDataGenerationPlan,
  EXAMPLE_PLAN_STEPS,
  updateDataGenerationPlan,
  type DataGenerationPlan,
} from '@/features/data-generation/services/dataGenerationService';

const props = defineProps<{
  visible: boolean;
  projectId: number;
  plan?: DataGenerationPlan | null;
}>();

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void;
  (e: 'saved'): void;
}>();

const visibleProxy = computed({
  get: () => props.visible,
  set: (value: boolean) => emit('update:visible', value),
});

const isEdit = computed(() => !!props.plan?.id);
const formRef = ref();
const envLoading = ref(false);
const environments = ref<Array<{ id: number; name: string }>>([]);

const formData = reactive({
  name: '',
  description: '',
  target_type: 'both' as 'api' | 'ui' | 'both',
  default_environment: undefined as number | undefined,
  is_active: true,
  steps_json: '[]',
});

const rules = {
  name: [{ required: true, message: '请输入计划名称' }],
  steps_json: [{ required: true, message: '请输入步骤 JSON' }],
};

function resetForm(plan?: DataGenerationPlan | null) {
  formData.name = plan?.name || '';
  formData.description = plan?.description || '';
  formData.target_type = plan?.target_type || 'both';
  formData.default_environment = plan?.default_environment ?? undefined;
  formData.is_active = plan?.is_active ?? true;
  formData.steps_json = JSON.stringify(plan?.steps || [], null, 2);
}

async function loadEnvironments() {
  envLoading.value = true;
  try {
    const resp = await getEnvironments({ project: props.projectId });
    environments.value = (resp.results || resp.data || resp || []).map((item: any) => ({
      id: item.id,
      name: item.name,
    }));
  } catch {
    environments.value = [];
  } finally {
    envLoading.value = false;
  }
}

function applyExample() {
  formData.steps_json = JSON.stringify(EXAMPLE_PLAN_STEPS, null, 2);
}

function formatStepsJson() {
  try {
    const parsed = JSON.parse(formData.steps_json);
    formData.steps_json = JSON.stringify(parsed, null, 2);
  } catch {
    Message.error('JSON 格式无效');
  }
}

function parseSteps() {
  const parsed = JSON.parse(formData.steps_json);
  if (!Array.isArray(parsed)) {
    throw new Error('步骤必须是 JSON 数组');
  }
  return parsed;
}

async function handleSubmit() {
  const errors = await formRef.value?.validate();
  if (errors) return false;

  let steps;
  try {
    steps = parseSteps();
  } catch (error: any) {
    Message.error(error.message || '步骤 JSON 无效');
    return false;
  }

  const payload = {
    name: formData.name,
    description: formData.description,
    target_type: formData.target_type,
    default_environment: formData.default_environment ?? null,
    is_active: formData.is_active,
    steps,
  };

  try {
    if (isEdit.value && props.plan) {
      await updateDataGenerationPlan(props.projectId, props.plan.id, payload);
      Message.success('更新成功');
    } else {
      await createDataGenerationPlan(props.projectId, payload);
      Message.success('创建成功');
    }
    emit('saved');
    return true;
  } catch (error: any) {
    Message.error(error.response?.data?.message || error.message || '保存失败');
    return false;
  }
}

function handleCancel() {
  emit('update:visible', false);
}

watch(
  () => [props.visible, props.plan] as const,
  ([visible]) => {
    if (visible) {
      resetForm(props.plan);
      loadEnvironments();
    }
  },
);
</script>
