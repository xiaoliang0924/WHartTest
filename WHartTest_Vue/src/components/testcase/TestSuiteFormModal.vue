<template>
  <a-modal
    v-model:visible="modalVisible"
    :title="isEditing ? modalText.editTitle : modalText.createTitle"
    :width="900"
    :mask-closable="false"
    @before-ok="handleSubmit"
    @cancel="handleCancel"
  >
    <a-form :model="formData" :rules="rules" ref="formRef" layout="vertical">
      <a-form-item :label="modalText.suiteName" field="name" required>
        <a-input
          v-model="formData.name"
          :placeholder="modalText.suiteNamePlaceholder"
          :max-length="100"
          show-word-limit
        />
      </a-form-item>

      <a-form-item :label="modalText.suiteDescription" field="description">
        <a-textarea
          v-model="formData.description"
          :placeholder="modalText.suiteDescriptionPlaceholder"
          :max-length="500"
          :auto-size="{ minRows: 3, maxRows: 6 }"
          show-word-limit
        />
      </a-form-item>

      <a-form-item :label="modalText.concurrentCount" field="max_concurrent_tasks" required>
        <a-input-number
          v-model="formData.max_concurrent_tasks"
          :min="1"
          :max="10"
          :default-value="1"
          :style="{ width: '200px' }"
        />
        <div class="field-hint">
          <icon-info-circle style="margin-right: 4px;" />
          {{ modalText.concurrentHint }}
        </div>
      </a-form-item>

      <a-divider orientation="left">套件跑前造数</a-divider>
      <a-row :gutter="16">
        <a-col :span="12">
          <a-form-item label="造数计划">
            <a-select
              v-model="formData.pre_data_plan"
              placeholder="可选：执行套件前自动造数"
              allow-clear
              :loading="planLoading"
            >
              <a-option v-for="plan in dataPlans" :key="plan.id" :value="plan.id">
                {{ plan.name }}
              </a-option>
            </a-select>
          </a-form-item>
        </a-col>
        <a-col :span="12">
          <a-form-item label="造数 API 环境">
            <a-select
              v-model="formData.pre_data_environment"
              placeholder="选择 API 环境"
              allow-clear
              :loading="envLoading"
            >
              <a-option v-for="env in environments" :key="env.id" :value="env.id">
                {{ env.name }}
              </a-option>
            </a-select>
          </a-form-item>
        </a-col>
      </a-row>
      <a-form-item label="造数参数 (JSON)">
        <a-textarea
          v-model="preDataParamsJson"
          :auto-size="{ minRows: 2, maxRows: 5 }"
          placeholder='例如 {"count": 1}'
        />
      </a-form-item>
      <a-form-item label="造数失败阻断执行">
        <a-switch v-model="formData.pre_data_fail_fast" />
      </a-form-item>
      <a-form-item label="套件完成后自动清理">
        <a-switch v-model="formData.post_data_cleanup" />
      </a-form-item>
      <a-alert
        v-if="formData.post_data_cleanup && selectedPlanCleanupCount === 0"
        type="warning"
        style="margin-bottom: 12px"
        title="当前造数计划未配置清理步骤"
      >
        开启自动清理后，套件跑完将尝试执行计划的 cleanup_steps；请为计划添加清理步骤或关闭此开关。
      </a-alert>
      <a-space v-if="isEditing && props.suiteId" style="margin-bottom: 12px">
        <a-button size="small" html-type="button" :loading="gapAnalyzing" @click="handleAnalyzeGaps">
          分析变量缺口
        </a-button>
        <a-button
          size="small"
          type="primary"
          html-type="button"
          :loading="bindingPlan"
          :disabled="!canOneClickBind"
          @click="handleGenerateAndBind"
        >
          一键生成并绑定
        </a-button>
      </a-space>
      <a-alert
        v-if="gapAnalysis"
        :type="missingVariableCount > 0 ? 'warning' : 'success'"
        style="margin-bottom: 12px"
        :title="gapAlertTitle"
      >
        <div v-if="missingVariableCount > 0">
          缺失：{{ gapAnalysis.missing_variables.join('、') }}
        </div>
        <div v-else>当前套件引用的变量已在公共数据/环境变量中找到。</div>
        <div v-if="gapAnalysis.suggestions?.length" class="gap-suggestions">
          <div v-for="(item, index) in gapAnalysis.suggestions" :key="index" class="gap-suggestion-item">
            {{ formatSuggestion(item) }}
          </div>
        </div>
        <div v-if="gapAnalysis.testcases?.length" class="gap-testcases">
          <a-collapse :bordered="false">
            <a-collapse-item header="按用例查看变量" key="cases">
              <div v-for="tc in gapAnalysis.testcases" :key="tc.id" class="gap-testcase-row">
                {{ tc.name }}：{{ tc.variables.join('、') }}
              </div>
            </a-collapse-item>
          </a-collapse>
        </div>
      </a-alert>

      <!-- 选择测试用例 -->
      <a-form-item required>
        <template #label>
          <div class="label-with-hint">
            <span>{{ modalText.selectCases }}</span>
            <a-tag v-if="selectedTestCaseIds.length === 0" color="orangered" size="small">
              {{ modalText.selectAtLeastOne }}
            </a-tag>
            <a-tag v-else color="green" size="small">
              {{ modalText.selectedCases(selectedTestCaseIds.length) }}
            </a-tag>
          </div>
        </template>

        <div class="content-selection">
          <a-alert v-if="selectedTestCaseIds.length > 0" type="info" style="margin-bottom: 12px;">
            {{ modalText.selectedFunctionalCasesPrefix }} <strong>{{ selectedTestCaseIds.length }}</strong> {{ modalText.selectedFunctionalCasesSuffix }}
          </a-alert>
          <a-button
            type="outline"
            @click="showTestCaseSelector = true"
            style="width: 100%; margin-bottom: 12px;"
          >
            <template #icon><icon-plus /></template>
            {{ selectedTestCaseIds.length > 0 ? modalText.reselectFunctionalCases : modalText.selectFunctionalCases }}
          </a-button>

          <div v-if="selectedTestCases.length > 0" class="selected-items">
            <div class="item-list-header">
              <span>{{ modalText.selectedFunctionalCases }}:</span>
              <a-button type="text" size="small" status="danger" @click="handleClearTestCases">{{ modalText.clear }}</a-button>
            </div>
            <a-list :max-height="200" :scrollbar="true">
              <a-list-item v-for="tc in selectedTestCases" :key="tc.id" class="item-row">
                <a-list-item-meta :title="tc.name" :description="`${modalText.priorityLabel}: ${tc.level}`" />
                <template #actions>
                  <a-button type="text" size="small" status="danger" @click="handleRemoveTestCase(tc.id)">
                    <icon-close />
                  </a-button>
                </template>
              </a-list-item>
            </a-list>
          </div>
        </div>
      </a-form-item>
    </a-form>

    <!-- 测试用例选择器模态框 -->
    <a-modal
      v-model:visible="showTestCaseSelector"
      :title="modalText.selectFunctionalCases"
      :width="1000"
      :footer="false"
      :mask-closable="false"
    >
      <TestCaseSelectorTable
        :current-project-id="currentProjectId"
        :initial-selected-ids="selectedTestCaseIds"
        @confirm="handleTestCaseSelect"
        @cancel="showTestCaseSelector = false"
      />
    </a-modal>
  </a-modal>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue';
import { Message } from '@arco-design/web-vue';
import { IconPlus, IconClose, IconInfoCircle } from '@arco-design/web-vue/es/icon';
import { useAppI18n } from '@/composables/useAppI18n';
import {
  createTestSuite,
  updateTestSuite,
  getTestSuiteDetail,
  type CreateTestSuiteRequest,
} from '@/services/testSuiteService';
import { getEnvironments } from '@/features/api-testing/services/environmentService';
import { getDataGenerationPlans, analyzeSuiteVariableGaps, generateAndBindSuitePreData, getDataGenerationPlan, type SuiteVariableGapAnalysis } from '@/features/data-generation/services/dataGenerationService';
import { getTestCaseDetail, type TestCase } from '@/services/testcaseService';
import TestCaseSelectorTable from './TestCaseSelectorTable.vue';

interface Props {
  visible: boolean;
  currentProjectId: number | null;
  suiteId?: number | null;
  initialTestCaseIds?: number[];
}

const props = withDefaults(defineProps<Props>(), {
  suiteId: null,
  initialTestCaseIds: () => [],
});

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void;
  (e: 'success'): void;
}>();

const modalVisible = computed({
  get: () => props.visible,
  set: (value) => emit('update:visible', value),
});

const isEditing = computed(() => !!props.suiteId);
const { isEnglish } = useAppI18n();

const modalText = computed(() => (
  isEnglish.value
    ? {
        editTitle: 'Edit Test Suite',
        createTitle: 'Create Test Suite',
        suiteName: 'Suite Name',
        suiteNamePlaceholder: 'Please enter test suite name',
        suiteDescription: 'Suite Description',
        suiteDescriptionPlaceholder: 'Please enter suite description (optional)',
        concurrentCount: 'Concurrent Tasks',
        concurrentHint: 'Set how many test cases run at the same time. 1 means serial; 2-10 means concurrent.',
        selectCases: 'Select Test Cases',
        selectAtLeastOne: 'Select at least one',
        selectedCases: (count: number) => `${count} selected`,
        selectedFunctionalCasesPrefix: 'Selected',
        selectedFunctionalCasesSuffix: 'functional test case(s)',
        reselectFunctionalCases: 'Reselect Functional Test Cases',
        selectFunctionalCases: 'Select Functional Test Cases',
        selectedFunctionalCases: 'Selected Functional Test Cases',
        clear: 'Clear',
        priorityLabel: 'Priority',
        nameRequired: 'Please enter suite name',
        nameMinLength: 'Suite name must be at least 2 characters',
        concurrentRequired: 'Please set concurrent tasks',
        concurrentRange: 'Concurrent tasks must be between 1 and 10',
        projectIdMissing: 'Missing project ID',
        caseRequired: 'Please select at least one functional test case',
        updateSuccess: 'Updated successfully',
        createSuccess: 'Created successfully',
        operationFailed: 'Operation failed',
        loadSuiteDetailFailed: 'Failed to load suite details',
        loadSuiteDetailError: 'An error occurred while loading suite details',
      }
    : {
        editTitle: '编辑测试套件',
        createTitle: '创建测试套件',
        suiteName: '套件名称',
        suiteNamePlaceholder: '请输入测试套件名称',
        suiteDescription: '套件描述',
        suiteDescriptionPlaceholder: '请输入套件描述(可选)',
        concurrentCount: '并发执行数',
        concurrentHint: '设置同时执行的测试用例数量。1表示串行执行，2-10表示并发执行。',
        selectCases: '选择测试用例',
        selectAtLeastOne: '请至少选择一个',
        selectedCases: (count: number) => `已选 ${count} 用例`,
        selectedFunctionalCasesPrefix: '已选择',
        selectedFunctionalCasesSuffix: '个功能用例',
        reselectFunctionalCases: '重新选择功能用例',
        selectFunctionalCases: '选择功能用例',
        selectedFunctionalCases: '已选择的功能用例',
        clear: '清空',
        priorityLabel: '优先级',
        nameRequired: '请输入套件名称',
        nameMinLength: '套件名称至少2个字符',
        concurrentRequired: '请设置并发执行数',
        concurrentRange: '并发数必须在1-10之间',
        projectIdMissing: '缺少项目ID',
        caseRequired: '请至少选择一个功能用例',
        updateSuccess: '更新成功',
        createSuccess: '创建成功',
        operationFailed: '操作失败',
        loadSuiteDetailFailed: '加载套件详情失败',
        loadSuiteDetailError: '加载套件详情时发生错误',
      }
));

const formRef = ref();
const showTestCaseSelector = ref(false);
const selectedTestCaseIds = ref<number[]>([]);
const selectedTestCases = ref<TestCase[]>([]);
const loading = ref(false);
const planLoading = ref(false);
const envLoading = ref(false);
const dataPlans = ref<Array<{ id: number; name: string; cleanup_step_count?: number }>>([]);
const environments = ref<Array<{ id: number; name: string }>>([]);
const preDataParamsJson = ref('{}');
const gapAnalyzing = ref(false);
const bindingPlan = ref(false);
const gapAnalysis = ref<SuiteVariableGapAnalysis | null>(null);
const selectedPlanCleanupCount = ref<number | null>(null);

const canOneClickBind = computed(() => Boolean(isEditing.value && props.suiteId && props.currentProjectId));

const missingVariableCount = computed(
  () => gapAnalysis.value?.missing_variables?.length ?? 0,
);

const gapAlertTitle = computed(() => {
  if (!gapAnalysis.value) return '';
  if (missingVariableCount.value > 0) return `缺失变量 ${missingVariableCount.value} 个`;
  return '变量缺口已满足';
});

const formData = ref<CreateTestSuiteRequest>({
  name: '',
  description: '',
  testcase_ids: [],
  max_concurrent_tasks: 1,
  pre_data_plan: null,
  pre_data_environment: null,
  pre_data_params: {},
  pre_data_fail_fast: true,
  post_data_cleanup: false,
});

const rules = computed(() => ({
  name: [
    { required: true, message: modalText.value.nameRequired },
    { minLength: 2, message: modalText.value.nameMinLength },
  ],
  max_concurrent_tasks: [
    { required: true, message: modalText.value.concurrentRequired },
    { type: 'number', min: 1, max: 10, message: modalText.value.concurrentRange },
  ],
}));

// 加载已选择的测试用例详情
const loadSelectedTestCases = async () => {
  if (!props.currentProjectId || selectedTestCaseIds.value.length === 0) {
    selectedTestCases.value = [];
    return;
  }
  try {
    const promises = selectedTestCaseIds.value.map((id) =>
      getTestCaseDetail(props.currentProjectId!, id)
    );
    const responses = await Promise.all(promises);
    selectedTestCases.value = responses.filter((r) => r.success && r.data).map((r) => r.data!);
  } catch (error) {
    console.error('加载测试用例详情失败:', error);
  }
};

// 处理测试用例选择
const handleTestCaseSelect = (testcaseIds: number[]) => {
  selectedTestCaseIds.value = testcaseIds;
  loadSelectedTestCases();
  showTestCaseSelector.value = false;
};

// 移除单个测试用例
const handleRemoveTestCase = (id: number) => {
  selectedTestCaseIds.value = selectedTestCaseIds.value.filter((tcId) => tcId !== id);
  selectedTestCases.value = selectedTestCases.value.filter((tc) => tc.id !== id);
};

// 清空用例选择
const handleClearTestCases = () => {
  selectedTestCaseIds.value = [];
  selectedTestCases.value = [];
};

// 提交表单
const handleSubmit = async () => {
  if (!props.currentProjectId) {
    Message.error(modalText.value.projectIdMissing);
    return false;
  }

  // 自定义验证：至少选择一个用例
  if (selectedTestCaseIds.value.length === 0) {
    Message.error(modalText.value.caseRequired);
    return false;
  }

  try {
    await formRef.value?.validate();

    try {
      formData.value.pre_data_params = preDataParamsJson.value
        ? JSON.parse(preDataParamsJson.value)
        : {};
    } catch {
      Message.error('造数参数 JSON 格式无效');
      return false;
    }

    loading.value = true;
    formData.value.testcase_ids = selectedTestCaseIds.value;

    const response = isEditing.value
      ? await updateTestSuite(props.currentProjectId, props.suiteId!, formData.value)
      : await createTestSuite(props.currentProjectId, formData.value);

    if (response.success) {
      Message.success(response.message || (isEditing.value ? modalText.value.updateSuccess : modalText.value.createSuccess));
      emit('success');
      handleCancel();
      return true;
    } else {
      Message.error(response.error || modalText.value.operationFailed);
      return false;
    }
  } catch (error) {
    console.error('提交表单失败:', error);
    return false;
  } finally {
    loading.value = false;
  }
};

// 取消
const handleCancel = () => {
  formRef.value?.resetFields();
  selectedTestCaseIds.value = [];
  selectedTestCases.value = [];
  preDataParamsJson.value = '{}';
  gapAnalysis.value = null;
  selectedPlanCleanupCount.value = null;
  emit('update:visible', false);
};

function formatSuggestion(item: Record<string, unknown>) {
  if (item.template_key) {
    return `建议模板：${item.template_key}（${item.reason || ''}）`;
  }
  if (item.action === 'bind_pre_data_plan') {
    return String(item.reason || '建议绑定前置造数计划');
  }
  return String(item.reason || JSON.stringify(item));
}

async function refreshSelectedPlanCleanupCount(planId?: number | null) {
  if (!props.currentProjectId || !planId) {
    selectedPlanCleanupCount.value = null;
    return;
  }
  try {
    const plan = await getDataGenerationPlan(props.currentProjectId, planId);
    selectedPlanCleanupCount.value = plan.cleanup_step_count ?? (plan.cleanup_steps?.length || 0);
  } catch {
    selectedPlanCleanupCount.value = null;
  }
}

async function handleGenerateAndBind() {
  if (!props.currentProjectId || !props.suiteId) return;
  bindingPlan.value = true;
  try {
    const result = await generateAndBindSuitePreData(props.currentProjectId, props.suiteId, {
      environmentId: formData.value.pre_data_environment ?? null,
      useLlm: !gapAnalysis.value?.recommended_template_key,
      enablePostCleanup: true,
      description: gapAnalysis.value?.recommended_description,
    });
    const plan = result.plan;
    formData.value.pre_data_plan = plan.id;
    if (plan.default_environment) {
      formData.value.pre_data_environment = plan.default_environment;
    }
    if (result.post_data_cleanup_enabled) {
      formData.value.post_data_cleanup = true;
    }
    gapAnalysis.value = result.gap_analysis;
    selectedPlanCleanupCount.value = plan.cleanup_step_count ?? (plan.cleanup_steps?.length || 0);
    if (!dataPlans.value.some((item) => item.id === plan.id)) {
      dataPlans.value.unshift({
        id: plan.id,
        name: plan.name,
        cleanup_step_count: selectedPlanCleanupCount.value ?? 0,
      });
    }
    Message.success(
      result.created === false
        ? `已更新并绑定造数计划「${plan.name}」`
        : `已生成并绑定造数计划「${plan.name}」`,
    );
  } catch (error: any) {
    const payload = error.response?.data;
    const fieldErrors = payload?.errors;
    let detail = '';
    if (fieldErrors && typeof fieldErrors === 'object') {
      detail = Object.entries(fieldErrors)
        .map(([key, value]) => {
          const text = Array.isArray(value) ? value.join('、') : String(value);
          return `${key}: ${text}`;
        })
        .join('；');
    }
    Message.error(detail || payload?.message || error.message || '生成并绑定失败');
  } finally {
    bindingPlan.value = false;
  }
}

async function handleAnalyzeGaps() {
  if (!props.currentProjectId || !props.suiteId) return;
  gapAnalyzing.value = true;
  try {
    gapAnalysis.value = await analyzeSuiteVariableGaps(
      props.currentProjectId,
      props.suiteId,
      formData.value.pre_data_environment ?? null,
    );
  } catch (error: any) {
    Message.error(error.message || '分析失败');
  } finally {
    gapAnalyzing.value = false;
  }
}

async function loadDataGenerationOptions() {
  if (!props.currentProjectId) return;
  planLoading.value = true;
  envLoading.value = true;
  try {
    const [planResp, envResp] = await Promise.all([
      getDataGenerationPlans(props.currentProjectId, { is_active: true, page_size: 200 }),
      getEnvironments({ project_id: props.currentProjectId }),
    ]);
    dataPlans.value = planResp.results.map((item) => ({
      id: item.id,
      name: item.name,
      cleanup_step_count: item.cleanup_step_count,
    }));
    environments.value = (envResp.results || envResp.data || envResp || []).map((item: any) => ({
      id: item.id,
      name: item.name,
    }));
  } catch (error) {
    console.error('加载造数配置选项失败:', error);
  } finally {
    planLoading.value = false;
    envLoading.value = false;
  }
}

// 加载套件详情
const loadSuiteDetail = async () => {
  if (!props.currentProjectId || !props.suiteId) {
    return;
  }

  loading.value = true;
  try {
    const response = await getTestSuiteDetail(props.currentProjectId, props.suiteId);

    if (response.success && response.data) {
      const suite = response.data;

      formData.value.name = suite.name;
      formData.value.description = suite.description || '';
      formData.value.max_concurrent_tasks = suite.max_concurrent_tasks || 1;
      formData.value.pre_data_plan = suite.pre_data_plan ?? null;
      formData.value.pre_data_environment = suite.pre_data_environment ?? null;
      formData.value.pre_data_fail_fast = suite.pre_data_fail_fast ?? true;
      formData.value.post_data_cleanup = suite.post_data_cleanup ?? false;
      formData.value.pre_data_params = suite.pre_data_params || {};
      preDataParamsJson.value = JSON.stringify(suite.pre_data_params || {}, null, 2);
      await refreshSelectedPlanCleanupCount(suite.pre_data_plan ?? null);

      // 获取用例ID列表
      if (suite.testcases_detail && suite.testcases_detail.length > 0) {
        selectedTestCaseIds.value = suite.testcases_detail.map((tc) => tc.id);
        selectedTestCases.value = [...suite.testcases_detail];
      }
    } else {
      Message.error(response.error || modalText.value.loadSuiteDetailFailed);
    }
  } catch (error) {
    console.error('加载套件详情失败:', error);
    Message.error(modalText.value.loadSuiteDetailError);
  } finally {
    loading.value = false;
  }
};

// 监听visible变化，初始化数据
watch(
  () => formData.value.pre_data_plan,
  (planId) => {
    refreshSelectedPlanCleanupCount(planId ?? null);
  },
);

watch(
  () => props.visible,
  async (newVal) => {
    if (newVal) {
      await loadDataGenerationOptions();
      if (isEditing.value && props.suiteId) {
        await loadSuiteDetail();
      } else {
        selectedTestCaseIds.value = [...props.initialTestCaseIds];
        loadSelectedTestCases();
      }
    }
  }
);
</script>

<style scoped>
.content-selection {
  width: 100%;
}

.selected-items {
  border: 1px solid var(--color-border);
  border-radius: 4px;
  padding: 12px;
}

.item-list-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  font-weight: 500;
}

.item-row {
  padding: 8px 0;
}

.item-row:not(:last-child) {
  border-bottom: 1px solid var(--color-border-1);
}

.field-hint {
  margin-top: 8px;
  font-size: 12px;
  color: var(--color-text-3);
  display: flex;
  align-items: flex-start;
  line-height: 1.5;
}

.label-with-hint {
  display: flex;
  align-items: center;
  gap: 8px;
}

.gap-suggestions {
  margin-top: 8px;
  font-size: 12px;
  color: var(--color-text-2);
}

.gap-suggestion-item + .gap-suggestion-item {
  margin-top: 4px;
}

.gap-testcases {
  margin-top: 8px;
}

.gap-testcase-row {
  font-size: 12px;
  line-height: 1.6;
  color: var(--color-text-2);
}
</style>
