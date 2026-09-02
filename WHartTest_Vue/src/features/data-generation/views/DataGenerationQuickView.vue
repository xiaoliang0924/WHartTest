<template>
  <div class="data-generation-quick-view">
    <div v-if="!currentProjectId" class="no-project-selected">
      <a-empty description="请先选择项目" />
    </div>

    <div v-else>
      <a-alert type="info" style="margin-bottom: 16px">
        选择常用模板一键造数，执行结果会写入环境变量与 UI 公共数据。
      </a-alert>

      <a-spin :loading="loading">
        <a-empty
          v-if="!loading && templates.length === 0"
          description="暂无可用模板，请刷新页面或前往「造数计划」新建计划"
        />
        <a-row v-else :gutter="16">
          <a-col v-for="template in templates" :key="template.template_key" :span="8">
            <a-card :title="template.name" class="template-card">
              <p class="template-desc">{{ template.description }}</p>
              <a-form layout="vertical" size="small">
                <a-form-item
                  v-for="(schema, key) in template.params_schema || {}"
                  :key="key"
                  :label="schema.label || key"
                >
                  <a-input
                    v-if="schema.type !== 'number'"
                    v-model="formValues[template.template_key][key]"
                  />
                  <a-input-number
                    v-else
                    v-model="formValues[template.template_key][key]"
                    style="width: 100%"
                  />
                </a-form-item>
              </a-form>
              <a-button
                type="primary"
                long
                :loading="runningKey === template.template_key"
                @click="handleRunTemplate(template)"
              >
                一键执行
              </a-button>
            </a-card>
          </a-col>
        </a-row>
      </a-spin>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue';
import { Message } from '@arco-design/web-vue';
import { useProjectStore } from '@/store/projectStore';
import {
  getDataGenerationTemplates,
  runDataGenerationPlan,
  runDataGenerationTemplate,
  type DataGenerationPlan,
  type DataGenerationTemplate,
} from '@/features/data-generation/services/dataGenerationService';

const projectStore = useProjectStore();
const currentProjectId = computed(() => projectStore.currentProjectId);

const loading = ref(false);
const runningKey = ref<string | null>(null);
const templates = ref<DataGenerationTemplate[]>([]);
const formValues = reactive<Record<string, Record<string, unknown>>>({});

const DEFAULT_PARAMS_SCHEMA = {
  summary: { type: 'string', label: '工单摘要', default: '快速造数测试' },
};

function ensureFormValues(template: DataGenerationTemplate) {
  if (!formValues[template.template_key]) {
    formValues[template.template_key] = {};
  }
  const schema = template.params_schema || DEFAULT_PARAMS_SCHEMA;
  Object.entries(schema).forEach(([key, field]) => {
    if (formValues[template.template_key][key] === undefined) {
      formValues[template.template_key][key] = field.default ?? '';
    }
  });
}

function mapSavedPlan(plan: DataGenerationPlan): DataGenerationTemplate {
  return {
    template_key: plan.template_key || `saved_plan_${plan.id}`,
    name: plan.name,
    description: plan.description || '项目内保存的造数模板',
    target_type: plan.target_type,
    params_schema: (plan.template_params_schema as DataGenerationTemplate['params_schema']) || DEFAULT_PARAMS_SCHEMA,
    steps: plan.steps,
    cleanup_steps: plan.cleanup_steps,
    plan_id: plan.id,
  };
}

function mergeTemplates(builtin: DataGenerationTemplate[], saved: DataGenerationPlan[]) {
  const withSteps = (items: DataGenerationTemplate[]) =>
    items.filter((item) => (item.steps || []).length > 0);

  const builtinList = withSteps(builtin || []);
  const savedList = withSteps((saved || []).map(mapSavedPlan));

  const builtinKeys = new Set(builtinList.map((item) => item.template_key));
  return [...builtinList, ...savedList.filter((item) => !builtinKeys.has(item.template_key))];
}

async function fetchTemplates() {
  if (!currentProjectId.value) return;
  loading.value = true;
  try {
    const data = await getDataGenerationTemplates(currentProjectId.value);
    templates.value = mergeTemplates(data.builtin || [], data.saved || []);
    templates.value.sort((a, b) => {
      const aIsStepTest = a.template_key.startsWith('test_step_') ? 0 : 1;
      const bIsStepTest = b.template_key.startsWith('test_step_') ? 0 : 1;
      if (aIsStepTest !== bIsStepTest) return aIsStepTest - bIsStepTest;
      return a.name.localeCompare(b.name, 'zh-CN');
    });
    templates.value.forEach(ensureFormValues);
  } catch (error: any) {
    Message.error(error.message || '加载模板失败');
  } finally {
    loading.value = false;
  }
}

async function handleRunTemplate(template: DataGenerationTemplate) {
  if (!currentProjectId.value) return;
  runningKey.value = template.template_key;
  const inputParams = formValues[template.template_key] || {};

  try {
    let resp;
    if (template.plan_id && !template.template_key.startsWith('create_')) {
      resp = await runDataGenerationPlan(currentProjectId.value, template.plan_id, inputParams);
    } else {
      resp = await runDataGenerationTemplate(
        currentProjectId.value,
        template.template_key,
        inputParams,
      );
    }

    if (resp.status === 'success') {
      Message.success('造数执行成功');
    } else {
      Message.error(resp.message || '造数执行失败');
    }
  } catch (error: any) {
    Message.error(error.response?.data?.message || error.message || '造数执行失败');
  } finally {
    runningKey.value = null;
  }
}

watch(currentProjectId, () => {
  fetchTemplates();
});

onMounted(fetchTemplates);
</script>

<style scoped>
.template-card {
  margin-bottom: 16px;
  min-height: 280px;
}
.template-desc {
  min-height: 48px;
  color: var(--color-text-2);
  font-size: 13px;
}
.no-project-selected {
  padding: 48px 0;
}
</style>
