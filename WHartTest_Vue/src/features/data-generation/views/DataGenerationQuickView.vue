<template>
  <div class="data-generation-quick-view">
    <div v-if="!currentProjectId" class="no-project-selected">
      <a-empty description="请先选择项目" />
    </div>

    <div v-else class="quick-view-body">
      <a-alert type="info" class="quick-view-alert">
        此处展示<strong>内置模板</strong>与勾选「保存为模板」的计划，供日常<strong>一键造数</strong>。
        完整计划的创建、编辑与 AI 生成请前往「造数计划」。
      </a-alert>

      <a-spin :loading="loading" class="quick-view-spin">
        <a-empty
          v-if="!loading && templates.length === 0"
          description="暂无可用模板，请刷新页面或前往「造数计划」新建计划"
        />

        <template v-else>
          <section v-if="stepTestTemplates.length" class="template-section">
            <div class="section-header">
              <span class="section-title">步骤能力测试</span>
              <span class="section-subtitle">每种执行步骤类型各一条，便于逐项验证</span>
            </div>
            <div class="template-grid">
              <TemplateCard
                v-for="template in stepTestTemplates"
                :key="template.template_key"
                :template="template"
                :form-values="formValues[template.template_key]"
                :loading="runningKey === template.template_key"
                @run="handleRunTemplate(template)"
              />
            </div>
          </section>

          <section v-if="businessTemplates.length" class="template-section">
            <div class="section-header">
              <span class="section-title">业务造数模板</span>
              <span class="section-subtitle">常用工单造数场景</span>
            </div>
            <div class="template-grid">
              <TemplateCard
                v-for="template in businessTemplates"
                :key="template.template_key"
                :template="template"
                :form-values="formValues[template.template_key]"
                :loading="runningKey === template.template_key"
                @run="handleRunTemplate(template)"
              />
            </div>
          </section>
        </template>
      </a-spin>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue';
import { Message } from '@arco-design/web-vue';
import { useProjectStore } from '@/store/projectStore';
import TemplateCard from '@/features/data-generation/components/DataGenerationTemplateCard.vue';
import {
  getDataGenerationTemplates,
  runDataGenerationPlanWithProgress,
  runDataGenerationTemplateWithProgress,
  type DataGenerationPlan,
  type DataGenerationTemplate,
} from '@/features/data-generation/services/dataGenerationService';

const emit = defineEmits<{
  (event: 'run-completed'): void;
}>();

const projectStore = useProjectStore();
const currentProjectId = computed(() => projectStore.currentProjectId);

const loading = ref(false);
const runningKey = ref<string | null>(null);
const templates = ref<DataGenerationTemplate[]>([]);
const formValues = reactive<Record<string, Record<string, unknown>>>({});

const stepTestTemplates = computed(() =>
  templates.value.filter((item) => item.template_key.startsWith('test_step_')),
);

const businessTemplates = computed(() =>
  templates.value.filter((item) => item.template_key.startsWith('biz_')),
);

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
  const savedList = withSteps(
    (saved || [])
      .map(mapSavedPlan)
      .filter((item) => item.template_key.startsWith('biz_') || item.template_key.startsWith('test_step_')),
  );

  const builtinKeys = new Set(builtinList.map((item) => item.template_key));
  return [...builtinList, ...savedList.filter((item) => !builtinKeys.has(item.template_key))];
}

function sortTemplates(list: DataGenerationTemplate[]) {
  return [...list].sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'));
}

async function fetchTemplates() {
  if (!currentProjectId.value) return;
  loading.value = true;
  try {
    const data = await getDataGenerationTemplates(currentProjectId.value);
    const merged = mergeTemplates(data.builtin || [], data.saved || []);
    const stepTests = sortTemplates(merged.filter((item) => item.template_key.startsWith('test_step_')));
    const business = sortTemplates(merged.filter((item) => item.template_key.startsWith('biz_')));
    templates.value = [...stepTests, ...business];
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
    let result;
    if (template.plan_id && !template.template_key.startsWith('create_')) {
      result = await runDataGenerationPlanWithProgress(
        currentProjectId.value,
        template.plan_id,
        inputParams,
      );
    } else {
      result = await runDataGenerationTemplateWithProgress(
        currentProjectId.value,
        template.template_key,
        inputParams,
      );
    }

    const { response, run } = result;
    emit('run-completed');
    if (response.status === 'success') {
      const hasContinued = (run.step_logs || []).some((step) => step.status === 'failed_continued');
      if (hasContinued || (run.error_message || '').includes('部分步骤失败')) {
        Message.warning(run.error_message || '造数执行部分成功');
      } else {
        Message.success('造数执行成功');
      }
    } else {
      Message.error(response.message || run.error_message || '造数执行失败');
    }
  } catch (error: any) {
    emit('run-completed');
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
.data-generation-quick-view {
  height: 100%;
  overflow: auto;
}

.quick-view-body {
  min-height: 100%;
}

.quick-view-alert {
  margin-bottom: 16px;
}

.quick-view-spin {
  width: 100%;
}

.template-section + .template-section {
  margin-top: 24px;
}

.section-header {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--color-border-2);
}

.section-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text-1);
}

.section-subtitle {
  font-size: 13px;
  color: var(--color-text-3);
}

.template-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
}

@media (max-width: 1400px) {
  .template-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 900px) {
  .template-grid {
    grid-template-columns: 1fr;
  }
}

.no-project-selected {
  padding: 48px 0;
}
</style>
