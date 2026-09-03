<template>
  <a-modal
    v-model:visible="visibleProxy"
    :title="isEdit ? '编辑造数计划' : '新建造数计划'"
    :width="980"
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
        <a-col :span="8">
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
        <a-col :span="8">
          <a-form-item label="启用" field="is_active">
            <a-switch v-model="formData.is_active" />
          </a-form-item>
        </a-col>
        <a-col :span="8">
          <a-form-item label="保存为模板">
            <a-switch v-model="formData.is_template" />
          </a-form-item>
        </a-col>
      </a-row>

      <a-space style="margin-bottom: 12px">
        <a-button size="small" @click="applyExample">填入示例</a-button>
        <a-button size="small" :loading="generating" :disabled="generating" @click="handleAiGenerate">
          {{ generating ? 'AI 正在生成...' : 'AI 生成计划' }}
        </a-button>
      </a-space>

      <a-tabs v-model:active-key="editorMode">
        <a-tab-pane key="visual" title="可视化编排">
          <a-divider orientation="left">执行步骤</a-divider>
          <DataGenerationStepEditor v-model="formData.steps" />
          <a-divider orientation="left">清理步骤</a-divider>
          <DataGenerationStepEditor v-model="formData.cleanup_steps" />
        </a-tab-pane>
        <a-tab-pane key="json" title="JSON 编辑">
          <a-form-item label="执行步骤 JSON" field="steps_json" required>
            <a-textarea v-model="formData.steps_json" :auto-size="{ minRows: 10, maxRows: 20 }" />
          </a-form-item>
          <a-form-item label="清理步骤 JSON">
            <a-textarea v-model="formData.cleanup_steps_json" :auto-size="{ minRows: 6, maxRows: 14 }" />
          </a-form-item>
          <a-button size="small" @click="syncJsonFromVisual">从可视化同步到 JSON</a-button>
          <a-button size="small" style="margin-left: 8px" @click="syncVisualFromJson">从 JSON 同步到可视化</a-button>
        </a-tab-pane>
      </a-tabs>
    </a-form>
  </a-modal>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue';
import { Message } from '@arco-design/web-vue';
import { getEnvironments } from '@/features/api-testing/services/environmentService';
import DataGenerationStepEditor from '@/features/data-generation/components/DataGenerationStepEditor.vue';
import {
  createDataGenerationPlan,
  EXAMPLE_CLEANUP_STEPS,
  EXAMPLE_PLAN_STEPS,
  generateDataGenerationPlan,
  updateDataGenerationPlan,
  type DataGenerationPlan,
  type DataGenerationStep,
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
const generating = ref(false);
const editorMode = ref('visual');
const environments = ref<Array<{ id: number; name: string }>>([]);

const formData = reactive({
  name: '',
  description: '',
  target_type: 'both' as 'api' | 'ui' | 'both',
  default_environment: undefined as number | undefined,
  is_active: true,
  is_template: false,
  template_key: '' as string,
  template_params_schema: {} as Record<string, unknown>,
  steps: [] as DataGenerationStep[],
  cleanup_steps: [] as DataGenerationStep[],
  steps_json: '[]',
  cleanup_steps_json: '[]',
});

const rules = {
  name: [{ required: true, message: '请输入计划名称' }],
};

function resetForm(plan?: DataGenerationPlan | null) {
  formData.name = plan?.name || '';
  formData.description = plan?.description || '';
  formData.target_type = plan?.target_type || 'both';
  formData.default_environment = plan?.default_environment ?? undefined;
  formData.is_active = plan?.is_active ?? true;
  formData.is_template = plan?.is_template ?? false;
  formData.template_key = plan?.template_key || '';
  formData.template_params_schema = JSON.parse(JSON.stringify(plan?.template_params_schema || {}));
  formData.steps = JSON.parse(JSON.stringify(plan?.steps || []));
  formData.cleanup_steps = JSON.parse(JSON.stringify(plan?.cleanup_steps || []));
  formData.steps_json = JSON.stringify(plan?.steps || [], null, 2);
  formData.cleanup_steps_json = JSON.stringify(plan?.cleanup_steps || [], null, 2);
}

async function loadEnvironments() {
  envLoading.value = true;
  try {
    const resp = await getEnvironments({ project_id: props.projectId });
    const items = resp?.data?.results || resp?.results || [];
    environments.value = items.map((item: any) => ({
      id: item.id,
      name: item.name,
    }));
  } catch (error: any) {
    environments.value = [];
    Message.error(error?.error || error?.message || '加载 API 环境失败');
  } finally {
    envLoading.value = false;
  }
}

function applyExample() {
  formData.steps = JSON.parse(JSON.stringify(EXAMPLE_PLAN_STEPS));
  formData.cleanup_steps = JSON.parse(JSON.stringify(EXAMPLE_CLEANUP_STEPS));
  formData.steps_json = JSON.stringify(EXAMPLE_PLAN_STEPS, null, 2);
  formData.cleanup_steps_json = JSON.stringify(EXAMPLE_CLEANUP_STEPS, null, 2);
}

function syncJsonFromVisual() {
  formData.steps_json = JSON.stringify(formData.steps, null, 2);
  formData.cleanup_steps_json = JSON.stringify(formData.cleanup_steps, null, 2);
}

function syncVisualFromJson() {
  try {
    formData.steps = JSON.parse(formData.steps_json);
    formData.cleanup_steps = JSON.parse(formData.cleanup_steps_json || '[]');
  } catch {
    Message.error('JSON 格式无效');
  }
}

async function handleAiGenerate() {
  if (!formData.description?.trim()) {
    Message.warning('请先填写描述，再使用 AI 生成');
    return;
  }
  generating.value = true;
  try {
    const resp = await generateDataGenerationPlan(
      props.projectId,
      formData.description,
      formData.default_environment ?? null,
    );
    const generated = resp;
    formData.name = formData.name.trim() || generated.name || '';
    formData.target_type = generated.target_type || formData.target_type;
    formData.steps = generated.steps || [];
    formData.cleanup_steps = generated.cleanup_steps || [];
    formData.template_key = generated.template_key || '';
    formData.template_params_schema = generated.template_params_schema || {};
    formData.steps_json = JSON.stringify(formData.steps, null, 2);
    formData.cleanup_steps_json = JSON.stringify(formData.cleanup_steps, null, 2);
    if (generated.hint) {
      Message.info(generated.hint);
    } else if (generated.llm_used) {
      Message.success('LLM 已生成造数计划');
    } else {
      Message.success('已生成造数计划');
    }
  } catch (error: any) {
    Message.error(resolveApiError(error, '生成失败'));
  } finally {
    generating.value = false;
  }
}

function resolveStepsForSubmit() {
  if (editorMode.value === 'json') {
    const steps = JSON.parse(formData.steps_json);
    const cleanupSteps = JSON.parse(formData.cleanup_steps_json || '[]');
    if (!Array.isArray(steps)) throw new Error('步骤必须是 JSON 数组');
    if (!Array.isArray(cleanupSteps)) throw new Error('清理步骤必须是 JSON 数组');
    return { steps, cleanup_steps: cleanupSteps };
  }
  return { steps: formData.steps, cleanup_steps: formData.cleanup_steps };
}

function resolveApiError(error: any, fallback: string) {
  const response = error?.response?.data;
  const errors = response?.errors;
  if (errors && typeof errors === 'object') {
    const first = Object.values(errors).flat()[0];
    if (first) return String(first);
  }
  return response?.message || error?.message || fallback;
}

async function handleSubmit() {
  const errors = await formRef.value?.validate();
  if (errors) return false;

  let parsed;
  try {
    parsed = resolveStepsForSubmit();
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
    is_template: formData.is_template,
    template_key: formData.template_key || undefined,
    template_params_schema: formData.template_params_schema,
    steps: parsed.steps,
    cleanup_steps: parsed.cleanup_steps,
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
    Message.error(resolveApiError(error, '保存失败'));
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
