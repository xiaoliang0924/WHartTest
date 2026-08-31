<template>
  <div class="testcase-content">
    <div class="page-header">
      <div class="search-box">
        <a-input-search
          :placeholder="pageText.searchPlaceholder"
          allow-clear
          class="search-input"
          @search="onSearch"
          :style="{ width: isSmallScreen ? '70px' : '130px' }"
          v-model="localSearchKeyword"
        />
        <a-select
          v-model="selectedLevel"
          :placeholder="isSmallScreen ? pageText.priorityShort : pageText.priorityFilter"
          allow-clear
          class="level-filter"
          :style="{ width: isSmallScreen ? '90px' : '130px' }"
          @change="onLevelChange"
        >
          <a-option v-for="option in levelOptions" :key="option.value" :value="option.value">
            {{ isSmallScreen ? option.shortLabel : option.label }}
          </a-option>
        </a-select>
        <a-select
          v-model="selectedReviewStatuses"
          :placeholder="pageText.reviewStatusFilter"
          multiple
          allow-clear
          :max-tag-count="1"
          tag-nowrap
          class="review-status-filter"
          :style="{ width: '190px' }"
          @change="onReviewStatusChange"
        >
          <a-option v-for="option in reviewStatusOptions" :key="option.value" :value="option.value">
            {{ option.label }}
          </a-option>
        </a-select>
        <a-select
          v-model="selectedTestType"
          :placeholder="isSmallScreen ? pageText.typeShort : pageText.testTypeFilter"
          allow-clear
          class="test-type-filter"
          :style="{ width: isSmallScreen ? '90px' : '130px' }"
          @change="onTestTypeChange"
        >
          <a-option v-for="option in testTypeOptions" :key="option.value" :value="option.value">
            {{ option.label }}
          </a-option>
        </a-select>
        <a-button type="outline" class="io-btn" @click="handleExport">
          <template #icon>
            <icon-download />
          </template>
          <span class="io-btn-text">{{ pageText.export }}</span>
        </a-button>
        <a-button type="outline" class="io-btn" @click="handleImport">
          <template #icon>
            <icon-upload />
          </template>
          <span class="io-btn-text">{{ pageText.import }}</span>
        </a-button>
        <a-button
          v-if="selectedTestCaseIds.length > 0"
          type="outline"
          @click="openMoveModal(selectedTestCaseIds)"
        >
          <template #icon><icon-folder /></template>
          {{ pageText.batchMoveButton(selectedTestCaseIds.length) }}
        </a-button>
        <a-dropdown
          v-if="selectedTestCaseIds.length > 0"
          trigger="click"
          @select="handleBatchReviewStatusChange"
        >
          <a-button type="outline">
            {{ pageText.batchReviewStatusButton(selectedTestCaseIds.length) }}
            <icon-down style="margin-left: 4px;" />
          </a-button>
          <template #content>
            <a-doption
              v-for="option in reviewStatusOptions"
              :key="option.value"
              :value="option.value"
            >
              <a-tag :color="option.color" size="small">{{ option.label }}</a-tag>
            </a-doption>
          </template>
        </a-dropdown>
        <a-button
          v-if="selectedTestCaseIds.length > 0"
          type="primary"
          @click="assignSelectedCases"
        >
          <template #icon><icon-user-add /></template>
          分派执行 ({{ selectedTestCaseIds.length }})
        </a-button>
        <a-button
          v-if="selectedTestCaseIds.length > 0"
          type="primary"
          status="danger"
          @click="handleBatchDelete"
        >
          {{ pageText.batchDeleteButton(selectedTestCaseIds.length) }}
        </a-button>
      </div>
      <div class="action-buttons">
        <a-button type="primary" @click="handleGenerateTestCases">{{ pageText.generateCases }}</a-button>
        <a-button type="primary" @click="handleAddTestCase">{{ pageText.addCase }}</a-button>
      </div>
    </div>

    <div v-if="!currentProjectId" class="no-project-selected">
      <a-empty :description="pageText.noProjectSelected">
        <template #image>
          <icon-folder style="font-size: 48px; color: var(--theme-empty-icon);" />
        </template>
      </a-empty>
    </div>

    <a-table
      v-else
      :columns="columns"
      :data="testCaseData"
      :pagination="paginationConfig"
      :loading="loading"
      :scroll="tableScroll"
      :bordered="{ cell: true }"
      row-key="id"
      :draggable="{ type: 'handle', width: 32 }"
      column-resizable
      class="test-case-table"
      @page-change="onPageChange"
      @page-size-change="onPageSizeChange"
@change="handleTableChange"
      @column-resize="handleColumnResize"
      @sorter-change="onSorterChange"


    >
      <template #selection="{ record }">
        <div data-checkbox>
          <a-checkbox
            :model-value="selectedTestCaseIds.includes(record.id)"
            @change="(checked: boolean) => handleCheckboxChange(record.id, checked)"
            @click.stop
          />
        </div>
      </template>
      <template #selectAll>
        <div data-checkbox>
          <a-checkbox
            :model-value="isCurrentPageAllSelected"
            :indeterminate="isCurrentPageIndeterminate"
            @change="handleSelectCurrentPage"
            @click.stop
          />
        </div>
      </template>
      <template #name="{ record }">
        <a-tooltip :content="record.name">
          <span
            class="testcase-name-link testcase-text-cell"
            :style="getTextCellStyle('name')"
            @click.stop="handleViewTestCase(record)"
          >
            {{ record.name }}
          </span>
        </a-tooltip>
      </template>
      <template #level="{ record }">
        <a-tag :color="getLevelColor(record.level)">{{ record.level }}</a-tag>
      </template>
      <template #testType="{ record }">
        <a-tag>{{ getTestTypeLabel(record.test_type) }}</a-tag>
      </template>
      <template #reviewStatus="{ record }">
        <a-dropdown trigger="click" @select="(value: string) => handleReviewStatusChange(record, value)">
          <a-tag
            :color="getReviewStatusColor(record.review_status)"
            style="cursor: pointer;"
          >
            {{ getReviewStatusLabel(record.review_status) }}
            <icon-down style="margin-left: 4px; font-size: 10px;" />
          </a-tag>
          <template #content>
            <a-doption v-for="option in reviewStatusOptions" :key="option.value" :value="option.value">
              <a-tag :color="option.color" size="small">{{ option.label }}</a-tag>
            </a-doption>
          </template>
        </a-dropdown>
      </template>
      <template #module="{ record }">
        <a-tooltip v-if="record.module_detail" :content="record.module_detail">
          <span class="testcase-text-cell" :style="getTextCellStyle('module_detail')">
            {{ record.module_detail }}
          </span>
        </a-tooltip>
        <span v-else class="text-gray testcase-text-cell" :style="getTextCellStyle('module_detail')">
          {{ pageText.unassigned }}
        </span>
      </template>
      <template #creator="{ record }">
        <a-tooltip :content="record.creator_detail?.username || '-'">
          <span class="testcase-text-cell" :style="getTextCellStyle('creator_detail')">
            {{ record.creator_detail?.username || '-' }}
          </span>
        </a-tooltip>
      </template>
      <template #lastExecution="{ record }">
        <a-tag
          v-if="record.latest_run"
          :color="getRunStatusColor(record.latest_run.status)"
          class="run-status-tag"
          @click.stop="handleViewExecutionReport(record)"
        >
          {{ getRunStatusLabel(record.latest_run.status) }}
        </a-tag>
        <span v-else>-</span>
      </template>
      <template #operations="{ record }">
        <a-space :size="4">
          <a-button type="primary" size="mini" @click.stop="handleViewTestCase(record)">{{ pageText.view }}</a-button>
          <a-button type="primary" size="mini" @click.stop="handleEditTestCase(record)">{{ pageText.edit }}</a-button>
          <a-button type="outline" size="mini" @click.stop="openMoveModal([record.id])">{{ pageText.move }}</a-button>
          <a-button type="outline" size="mini" @click.stop="handleExecuteTestCase(record)">{{ pageText.execute }}</a-button>
          <a-button
            v-if="record.latest_run"
            type="outline"
            size="mini"
            status="success"
            @click.stop="handleViewExecutionReport(record)"
          >
            {{ pageText.report }}
          </a-button>
          <a-button type="outline" size="mini" @click.stop="handleCopyTestCase(record)">{{ pageText.copy }}</a-button>
          <a-button type="primary" status="danger" size="mini" @click.stop="handleDeleteTestCase(record)">{{ pageText.delete }}</a-button>
        </a-space>
      </template>
    </a-table>

    <ImportModal
      v-if="currentProjectId"
      ref="importModalRef"
      :project-id="currentProjectId"
      @success="onImportSuccess"
    />

    <ExportModal
      v-if="currentProjectId"
      ref="exportModalRef"
      :project-id="currentProjectId"
      :selected-ids="selectedTestCaseIds"
      :module-tree="moduleTree"
    />

    <a-modal
      v-model:visible="moveModalVisible"
      :title="pageText.moveCasesTitle"
      :ok-text="pageText.move"
      :ok-loading="isMoving"
      @before-ok="handleConfirmMove"
    >
      <p class="move-modal-hint">{{ pageText.moveCasesHint(movingTestCaseIds.length) }}</p>
      <a-tree-select
        v-model="targetModuleId"
        :data="moduleTree || []"
        :placeholder="pageText.selectTargetModule"
        allow-search
        allow-clear
        :filter-tree-node="filterTreeNodeByName"
        :field-names="{ key: 'key', title: 'title', children: 'children' }"
      />
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, onUnmounted, computed, watch, toRefs } from 'vue';
import { useRouter } from 'vue-router';
import { Message, Modal } from '@arco-design/web-vue';
import { IconFolder, IconDownload, IconUpload, IconDown } from '@arco-design/web-vue/es/icon';
import { useAppI18n } from '@/composables/useAppI18n';
import ImportModal from '@/features/testcase-templates/components/ImportModal.vue';
import ExportModal from '@/features/testcase-templates/components/ExportModal.vue';
import {
  getTestCaseList,
  deleteTestCase as deleteTestCaseService,
  copyTestCase as copyTestCaseService,
  batchDeleteTestCases,
  batchMoveTestCases,
  batchUpdateTestCaseReviewStatus,
  reorderTestCases,
  updateTestCaseReviewStatus,
  type TestCase,
  type ReviewStatus,
} from '@/services/testcaseService';
import { formatDate, getLevelColor, getReviewStatusColor } from '@/utils/formatters';
import { filterTreeNodeByName } from '@/utils/filterTreeNode';
import type { TableColumnData, TreeNodeData } from '@arco-design/web-vue';

const router = useRouter();

const props = defineProps<{
  currentProjectId: number | null;
  selectedModuleId?: number | null; // 可选的模块ID，用于筛选
  moduleTree?: TreeNodeData[]; // 模块树数据
}>();

const emit = defineEmits<{
  (e: 'addTestCase'): void;
  (e: 'generate-test-cases'): void;
  (e: 'editTestCase', testCase: TestCase): void;
  (e: 'viewTestCase', testCase: TestCase): void;
  (e: 'testCaseDeleted'): void;
(e: 'testCasesMoved'): void;
  (e: 'testCaseCopied'): void;
  (e: 'executeTestCase', testCase: TestCase): void;
  (e: 'viewExecutionReport', testCase: TestCase): void;
  (e: 'module-filter-change', moduleId: number | null): void;
  (e: 'requestOptimization', testCase: TestCase): void;
}>();

const assignSelectedCases = () => {
  router.push({
    name: 'ManualTestExecution',
    query: { testcase_ids: selectedTestCaseIds.value.join(',') },
  });
};

const { currentProjectId, selectedModuleId } = toRefs(props);
const { isEnglish } = useAppI18n();

const pageText = computed(() => (
  isEnglish.value
    ? {
        searchPlaceholder: 'Search case name/precondition',
        priorityShort: 'Priority',
        priorityFilter: 'Filter priority',
        reviewStatusFilter: 'Filter review status',
        typeShort: 'Type',
        testTypeFilter: 'Test type',
        updatedAt: 'Updated at',
        export: 'Export',
        import: 'Import',
        batchDeleteButton: (count: number) => `Batch delete (${count})`,
        batchMoveButton: (count: number) => `Move (${count})`,
        batchReviewStatusButton: (count: number) => `Review status (${count})`,
        batchReviewStatusSuccess: (count: number, status: string) => `Updated review status to ${status} for ${count} test case(s)`,
        batchReviewStatusFailed: 'Failed to batch update review status',
        batchReviewStatusError: 'An error occurred while batch updating review status',
        move: 'Move',
        moveCasesTitle: 'Move test cases',
        moveCasesHint: (count: number) => `Select a target module for ${count} test case(s).`,
        selectTargetModule: 'Select target module',
        selectTargetModuleFirst: 'Select a target module first',
        moveCasesSuccess: (count: number, moduleName: string) => `Moved ${count} test case(s) to ${moduleName}`,
        moveCasesFailed: 'Failed to move test cases',
        moveCasesError: 'An error occurred while moving test cases',
        generateCases: 'Generate cases',
        addCase: 'Add case',
        noProjectSelected: 'Select a project from the top bar',
        unassigned: 'Unassigned',
        view: 'View',
        edit: 'Edit',
        execute: 'Run',
        report: 'Report',
        delete: 'Delete',
        copy: 'Copy',
        select: 'Select',
        caseName: 'Case name',
        precondition: 'Precondition',
        priority: 'Priority',
        testType: 'Test type',
        reviewStatus: 'Review status',
        module: 'Module',
        creator: 'Created by',
        createdAt: 'Created at',
        actions: 'Actions',
        fetchCasesFailed: 'Failed to fetch test cases',
        fetchCasesError: 'An error occurred while fetching test cases',
        reviewStatusUpdated: 'Status updated',
        reviewStatusUpdateFailed: 'Failed to update status',
        reviewStatusUpdateError: 'An error occurred while updating the status',
        selectProjectFirst: 'Select a project first',
        confirmDeleteTitle: 'Confirm deletion',
        confirmDeleteContent: (name: string) => `Delete test case "${name}"? This action cannot be undone.`,
        confirmOk: 'Confirm',
        deleteCaseSuccess: 'Test case deleted successfully',
        deleteCaseFailed: 'Failed to delete test case',
        deleteCaseError: 'An error occurred while deleting the test case',
        copyCaseSuccess: 'Test case copied successfully',
        copyCaseFailed: 'Failed to copy test case',
        copyCaseError: 'An error occurred while copying the test case',
        confirmBatchDeleteTitle: 'Confirm batch deletion',
        confirmBatchDeleteContent: (count: number, names: string) => `Delete ${count} test case(s)? This action cannot be undone.\n\n${names}`,
        confirmBatchDeleteOk: 'Delete',
        batchDeleteSuccess: (count: number) => `Deleted ${count} test case(s)`,
        batchDeleteDetails: (details: string) => `Deletion details: ${details}`,
        batchDeleteFailed: 'Failed to batch delete test cases',
        batchDeleteError: 'An error occurred while batch deleting test cases',
        lastExecution: 'Last run',
        runRunning: 'Running',
        runPass: 'Pass',
        runFail: 'Fail',
        runError: 'Error',
        runStopped: 'Stopped',
      }
    : {
        searchPlaceholder: '搜索用例名称/前置条件',
        priorityShort: '优先级',
        priorityFilter: '筛选优先级',
        reviewStatusFilter: '筛选审核状态',
        typeShort: '类型',
        testTypeFilter: '筛选测试类型',
        updatedAt: '修改时间',
        export: '导出',
        import: '导入',
        batchDeleteButton: (count: number) => `批量删除 (${count})`,
        batchMoveButton: (count: number) => `移动 (${count})`,
        batchReviewStatusButton: (count: number) => `修改审核状态 (${count})`,
        batchReviewStatusSuccess: (count: number, status: string) => `已将 ${count} 条用例的审核状态更新为「${status}」`,
        batchReviewStatusFailed: '批量更新审核状态失败',
        batchReviewStatusError: '批量更新审核状态时发生错误',
        move: '移动',
        moveCasesTitle: '移动测试用例',
        moveCasesHint: (count: number) => `请选择 ${count} 条测试用例的目标模块`,
        selectTargetModule: '请选择目标模块',
        selectTargetModuleFirst: '请先选择目标模块',
        moveCasesSuccess: (count: number, moduleName: string) => `已将 ${count} 条测试用例移动到 ${moduleName}`,
        moveCasesFailed: '移动测试用例失败',
        moveCasesError: '移动测试用例时发生错误',
        generateCases: '生成用例',
        addCase: '添加用例',
        noProjectSelected: '请在顶部选择一个项目',
        unassigned: '未分配',
        view: '查看',
        edit: '编辑',
        execute: '执行',
        report: '报告',
        delete: '删除',
        copy: '复制',
        select: '选择',
        caseName: '用例名称',
        precondition: '前置条件',
        priority: '优先级',
        testType: '测试类型',
        reviewStatus: '审核状态',
        module: '所属模块',
        creator: '创建者',
        createdAt: '创建时间',
        actions: '操作',
        fetchCasesFailed: '获取测试用例列表失败',
        fetchCasesError: '获取测试用例列表时发生错误',
        reviewStatusUpdated: '状态更新成功',
        reviewStatusUpdateFailed: '状态更新失败',
        reviewStatusUpdateError: '状态更新时发生错误',
        selectProjectFirst: '请先选择一个项目',
        confirmDeleteTitle: '确认删除',
        confirmDeleteContent: (name: string) => `确定要删除测试用例 "${name}" 吗？此操作不可恢复。`,
        confirmOk: '确认',
        deleteCaseSuccess: '测试用例删除成功',
        deleteCaseFailed: '删除测试用例失败',
        deleteCaseError: '删除测试用例时发生错误',
        copyCaseSuccess: '测试用例复制成功',
        copyCaseFailed: '复制测试用例失败',
        copyCaseError: '复制测试用例时发生错误',
        confirmBatchDeleteTitle: '确认批量删除',
        confirmBatchDeleteContent: (count: number, names: string) => `确定要删除以下 ${count} 个测试用例吗？此操作不可恢复。\n\n${names}`,
        confirmBatchDeleteOk: '确认删除',
        batchDeleteSuccess: (count: number) => `成功删除 ${count} 个测试用例`,
        batchDeleteDetails: (details: string) => `删除详情: ${details}`,
        batchDeleteFailed: '批量删除测试用例失败',
        batchDeleteError: '批量删除测试用例时发生错误',
        lastExecution: '最近执行',
        runRunning: '执行中',
        runPass: '通过',
        runFail: '失败',
        runError: '错误',
        runStopped: '已停止',
      }
));

const levelOptions = computed(() => (
  isEnglish.value
    ? [
        { value: 'P0', shortLabel: 'P0', label: 'P0 - Highest' },
        { value: 'P1', shortLabel: 'P1', label: 'P1 - High' },
        { value: 'P2', shortLabel: 'P2', label: 'P2 - Medium' },
        { value: 'P3', shortLabel: 'P3', label: 'P3 - Low' },
      ]
    : [
        { value: 'P0', shortLabel: 'P0', label: 'P0 - 最高' },
        { value: 'P1', shortLabel: 'P1', label: 'P1 - 高' },
        { value: 'P2', shortLabel: 'P2', label: 'P2 - 中' },
        { value: 'P3', shortLabel: 'P3', label: 'P3 - 低' },
      ]
));

const reviewStatusOptions = computed(() => (
  isEnglish.value
    ? [
        { value: 'pending_review', label: 'Pending', color: 'orange' },
        { value: 'approved', label: 'Approved', color: 'green' },
        { value: 'needs_optimization', label: 'Optimize', color: 'blue' },
        { value: 'optimization_pending_review', label: 'Re-review', color: 'purple' },
        { value: 'pending_product_confirmation', label: 'Product confirmation', color: 'cyan' },
        { value: 'unavailable', label: 'N/A', color: 'red' },
      ]
    : [
        { value: 'pending_review', label: '待审核', color: 'orange' },
        { value: 'approved', label: '通过', color: 'green' },
        { value: 'needs_optimization', label: '优化', color: 'blue' },
        { value: 'optimization_pending_review', label: '优化待审核', color: 'purple' },
        { value: 'pending_product_confirmation', label: '待产品确认', color: 'cyan' },
        { value: 'unavailable', label: '不可用', color: 'red' },
      ]
));

const testTypeOptions = computed(() => (
  isEnglish.value
    ? [
        { value: 'smoke', label: 'Smoke' },
        { value: 'functional', label: 'Functional' },
        { value: 'boundary', label: 'Boundary' },
        { value: 'exception', label: 'Exception' },
        { value: 'permission', label: 'Permission' },
        { value: 'security', label: 'Security' },
        { value: 'compatibility', label: 'Compatibility' },
      ]
    : [
        { value: 'smoke', label: '冒烟测试' },
        { value: 'functional', label: '功能测试' },
        { value: 'boundary', label: '边界测试' },
        { value: 'exception', label: '异常测试' },
        { value: 'permission', label: '权限测试' },
        { value: 'security', label: '安全测试' },
        { value: 'compatibility', label: '兼容性测试' },
      ]
));

const getReviewStatusLabel = (status?: string): string => {
  if (!status) {
    return reviewStatusOptions.value[0]?.label || '';
  }
  return reviewStatusOptions.value.find(option => option.value === status)?.label || status;
};

const getTestTypeLabel = (testType?: string): string => {
  if (!testType) {
    return testTypeOptions.value.find(option => option.value === 'functional')?.label || '';
  }
  return testTypeOptions.value.find(option => option.value === testType)?.label || testType;
};

// 本地模块选择（与外部 selectedModuleId 同步）
const localSelectedModuleId = ref<number | null>(props.selectedModuleId || null);

const loading = ref(false);
const localSearchKeyword = ref('');
const selectedLevel = ref<string>('');
const selectedTestType = ref<string>('');
const selectedOrdering = ref<string>('created_at');
// 默认选中除"不可用"之外的所有状态
const DEFAULT_REVIEW_STATUSES: ReviewStatus[] = ['pending_review', 'approved', 'needs_optimization', 'optimization_pending_review', 'pending_product_confirmation'];
const selectedReviewStatuses = ref<ReviewStatus[]>([...DEFAULT_REVIEW_STATUSES]);
const testCaseData = ref<TestCase[]>([]);
const selectedTestCaseIds = ref<number[]>([]);
const movingTestCaseIds = ref<number[]>([]);
const targetModuleId = ref<number | undefined>();
const moveModalVisible = ref(false);
const isMoving = ref(false);
const importModalRef = ref<InstanceType<typeof ImportModal> | null>(null);
const exportModalRef = ref<InstanceType<typeof ExportModal> | null>(null);
const COLUMN_WIDTH_STORAGE_KEY = 'wharttest:testcase-list-column-widths';

const defaultColumnWidths: Record<string, number> = {
  id: 50,
  name: 180,
  precondition: 120,
  level: 80,
  test_type: 90,
  review_status: 120,
  module_detail: 100,
  creator_detail: 80,
  created_at: 130,
  updated_at: 130,
};

const getStoredColumnWidths = (): Record<string, number> => {
  try {
    const stored = localStorage.getItem(COLUMN_WIDTH_STORAGE_KEY);
    if (!stored) return {};
    const parsed = JSON.parse(stored) as Record<string, number>;
    return Object.fromEntries(
      Object.entries(parsed).filter(([, width]) => Number.isFinite(width) && width >= 40)
    );
  } catch {
    return {};
  }
};

const columnWidths = reactive<Record<string, number>>({
  ...defaultColumnWidths,
  ...getStoredColumnWidths(),
});

const getColumnWidth = (key: string): number => columnWidths[key] ?? defaultColumnWidths[key] ?? 100;

const getTextCellStyle = (key: string) => ({
  maxWidth: `${Math.max(24, getColumnWidth(key) - 24)}px`,
});

const saveColumnWidths = () => {
  localStorage.setItem(COLUMN_WIDTH_STORAGE_KEY, JSON.stringify(columnWidths));
};

// 响应式屏幕宽度检测
const isSmallScreen = ref(window.innerWidth < 1222);
const tableContainerHeight = ref(400); // 默认高度
const handleResize = () => {
  isSmallScreen.value = window.innerWidth < 1222;
  // 计算表格容器高度：视口高度 - 头部(56) - 边距(86) - 搜索栏(60) - 分页(50) - 其他间距(40)
  tableContainerHeight.value = Math.max(300, window.innerHeight - 56 - 86 - 60 - 50 - 40);
};

// 表格滚动配置
const tableScroll = computed(() => ({
x: Math.max(
    1070,
    32 + 36 + Object.keys(defaultColumnWidths).reduce((total, key) => total + getColumnWidth(key), 0) + 240
  ),
  y: tableContainerHeight.value,
}));

const paginationConfig = reactive({
  total: 0,
  current: 1,
  pageSize: 10,
  showTotal: true,
  showJumper: true,
  showPageSize: true,
  pageSizeOptions: [10, 20, 50, 100],
});

// 复选框选择相关的计算属性和方法
// 获取当前页实际显示的数据
const getCurrentPageData = () => {
  const startIndex = (paginationConfig.current - 1) * paginationConfig.pageSize;
  const endIndex = startIndex + paginationConfig.pageSize;
  return testCaseData.value.slice(startIndex, endIndex);
};

// 当前页是否全选
const isCurrentPageAllSelected = computed(() => {
  const currentPageData = getCurrentPageData();
  if (currentPageData.length === 0) return false;
  return currentPageData.every(item => selectedTestCaseIds.value.includes(item.id));
});

// 当前页是否半选状态
const isCurrentPageIndeterminate = computed(() => {
  const currentPageData = getCurrentPageData();
  const currentPageSelectedCount = currentPageData.filter(item =>
    selectedTestCaseIds.value.includes(item.id)
  ).length;
  return currentPageSelectedCount > 0 && currentPageSelectedCount < currentPageData.length;
});

// 处理单个复选框变化
const handleCheckboxChange = (id: number, checked: boolean) => {
  if (checked) {
    if (!selectedTestCaseIds.value.includes(id)) {
      selectedTestCaseIds.value.push(id);
    }
  } else {
    const index = selectedTestCaseIds.value.indexOf(id);
    if (index > -1) {
      selectedTestCaseIds.value.splice(index, 1);
    }
  }
};

// 处理当前页全选/取消全选
const handleSelectCurrentPage = (checked: boolean) => {
  // 获取当前表格实际显示的数据
  // Arco Table 会根据 pagination 配置自动切分数据显示
  const startIndex = (paginationConfig.current - 1) * paginationConfig.pageSize;
  const endIndex = startIndex + paginationConfig.pageSize;
  const currentPageData = testCaseData.value.slice(startIndex, endIndex);
  
  if (checked) {
    // 选中当前页所有项目
    const currentPageIds = currentPageData.map(item => item.id);
    currentPageIds.forEach(id => {
      if (!selectedTestCaseIds.value.includes(id)) {
        selectedTestCaseIds.value.push(id);
      }
    });
  } else {
    // 取消选中当前页所有项目
    const currentPageIds = currentPageData.map(item => item.id);
    selectedTestCaseIds.value = selectedTestCaseIds.value.filter(id =>
      !currentPageIds.includes(id)
    );
  }
};

const getSortOrder = (field: string): 'ascend' | 'descend' | '' => {
  if (selectedOrdering.value === field) {
    return 'ascend';
  }
  if (selectedOrdering.value === `-${field}`) {
    return 'descend';
  }
  return '';
};

const columns = computed(() => ([
  {
    title: pageText.value.select,
    slotName: 'selection',
    width: 36,
    titleSlotName: 'selectAll',
    align: 'center'
  },
  {
    title: 'ID',
    dataIndex: 'id',
    width: getColumnWidth('id'),
    align: 'center',
    sortable: { sortDirections: ['ascend', 'descend'], sortOrder: getSortOrder('id') },
  },
  { title: pageText.value.caseName, dataIndex: 'name', slotName: 'name', width: getColumnWidth('name'), ellipsis: true, tooltip: false, align: 'center' },
  { title: pageText.value.precondition, dataIndex: 'precondition', width: getColumnWidth('precondition'), ellipsis: true, tooltip: true, align: 'center' },
  { title: pageText.value.priority, dataIndex: 'level', slotName: 'level', width: getColumnWidth('level'), align: 'center' },
  { title: pageText.value.testType, dataIndex: 'test_type', slotName: 'testType', width: getColumnWidth('test_type'), align: 'center' },
  { title: pageText.value.reviewStatus, dataIndex: 'review_status', slotName: 'reviewStatus', width: getColumnWidth('review_status'), align: 'center' },
  { title: pageText.value.lastExecution, slotName: 'lastExecution', width: 100, align: 'center' },
  { title: pageText.value.module, dataIndex: 'module_detail', slotName: 'module', width: getColumnWidth('module_detail'), ellipsis: true, tooltip: true, align: 'center' },
  {
    title: pageText.value.creator,
    dataIndex: 'creator_detail',
    slotName: 'creator',
    width: getColumnWidth('creator_detail'),
    align: 'center',
  },
  {
    title: pageText.value.createdAt,
    dataIndex: 'created_at',
    render: ({ record }: { record: TestCase }) => formatDate(record.created_at),
    width: getColumnWidth('created_at'),
    align: 'center',
    sortable: { sortDirections: ['ascend', 'descend'], sortOrder: getSortOrder('created_at') },
  },
  {
    title: pageText.value.updatedAt,
    dataIndex: 'updated_at',
    render: ({ record }: { record: TestCase }) => formatDate(record.updated_at),
    width: getColumnWidth('updated_at') || 130,
    align: 'center',
    sortable: { sortDirections: ['ascend', 'descend'], sortOrder: getSortOrder('updated_at') },
  },
  { title: pageText.value.actions, slotName: 'operations', width: 300, fixed: 'right', align: 'center' },
]) as TableColumnData[]);

const getRunStatusLabel = (status?: string) => {
  const map: Record<string, string> = {
    running: pageText.value.runRunning,
    pass: pageText.value.runPass,
    fail: pageText.value.runFail,
    error: pageText.value.runError,
    stopped: pageText.value.runStopped,
  };
  return map[status || ''] || '-';
};

const getRunStatusColor = (status?: string) => {
  return ({ running: 'arcoblue', pass: 'green', fail: 'red', error: 'orangered', stopped: 'gray' } as Record<string, string>)[status || ''] || 'gray';
};

const handleViewExecutionReport = (testCase: TestCase) => {
  emit('viewExecutionReport', testCase);
};

const handleColumnResize = (dataIndex: string, width: number) => {
  if (!(dataIndex in defaultColumnWidths) && dataIndex !== 'updated_at') return;
  columnWidths[dataIndex] = Math.max(40, Math.round(width));
  saveColumnWidths();
};

const handleTableChange = async (
  data: TestCase[],
  extra: { type?: string }
) => {
  if (extra.type !== 'drag' || !currentProjectId.value) return;

  const orderedIds = data.map(item => item.id);
  if (orderedIds.length === 0) return;

  testCaseData.value = [...data];

  try {
    const response = await reorderTestCases(currentProjectId.value, orderedIds);
    if (response.success) {
      Message.success(response.message || '测试用例顺序已保存');
    } else {
      Message.error(response.error || '保存测试用例顺序失败');
      fetchTestCases();
    }
  } catch (error) {
    Message.error('保存测试用例顺序时发生错误');
    fetchTestCases();
  }
};

const fetchTestCases = async () => {
  if (!currentProjectId.value) {
    testCaseData.value = [];
    paginationConfig.total = 0;
    selectedTestCaseIds.value = []; // 清空选中状态
    return;
  }
  loading.value = true;
  try {
    const response = await getTestCaseList(currentProjectId.value, {
      page: paginationConfig.current,
      pageSize: paginationConfig.pageSize,
      search: localSearchKeyword.value,
      module_id: localSelectedModuleId.value || undefined, // 使用本地模块筛选
      level: selectedLevel.value || undefined, // 添加优先级筛选
      test_type: selectedTestType.value || undefined, // 添加测试类型筛选
      // 多选审核状态筛选：有选中项则传递，否则不限制（显示全部）
      review_status_in: selectedReviewStatuses.value.length > 0 ? selectedReviewStatuses.value : undefined,
      ordering: selectedOrdering.value || undefined,
    });
    if (response.success && response.data) {
      testCaseData.value = response.data;
      paginationConfig.total = response.total || response.data.length;
      // 清空之前页面的选中状态
      selectedTestCaseIds.value = [];
    } else {
      Message.error(response.error || pageText.value.fetchCasesFailed);
      testCaseData.value = [];
      paginationConfig.total = 0;
      selectedTestCaseIds.value = [];
    }
  } catch (error) {
    console.error('获取测试用例列表出错:', error);
    Message.error(pageText.value.fetchCasesError);
    testCaseData.value = [];
    paginationConfig.total = 0;
    selectedTestCaseIds.value = [];
  } finally {
    loading.value = false;
  }
};

const onSearch = (value: string) => {
  localSearchKeyword.value = value;
  paginationConfig.current = 1;
  fetchTestCases();
};

const onLevelChange = (value: string) => {
  selectedLevel.value = value;
  paginationConfig.current = 1;
  fetchTestCases();
};

const onReviewStatusChange = (value: ReviewStatus[]) => {
  selectedReviewStatuses.value = value;
  paginationConfig.current = 1;
  fetchTestCases();
};

const onTestTypeChange = (value: string) => {
  selectedTestType.value = value;
  paginationConfig.current = 1;
  fetchTestCases();
};

const onSorterChange = (dataIndex: string, direction: string) => {
  const sortableFields = ['id', 'created_at', 'updated_at'];
  if (!sortableFields.includes(dataIndex)) {
    return;
  }

  // Arco 表格默认会在“升序/降序”后进入清空态。
  // 用例列表需要更顺手的两态切换：升序 <-> 降序。
  if (direction === 'ascend') {
    selectedOrdering.value = dataIndex;
  } else if (direction === 'descend') {
    selectedOrdering.value = `-${dataIndex}`;
  } else if (selectedOrdering.value === `-${dataIndex}`) {
    selectedOrdering.value = dataIndex;
  } else {
    selectedOrdering.value = `-${dataIndex}`;
  }

  paginationConfig.current = 1;
  fetchTestCases();
};

const handleReviewStatusChange = async (record: TestCase, newStatus: string) => {
  if (!currentProjectId.value) return;

  // 如果选择"优化"，触发优化弹窗
  if (newStatus === 'needs_optimization') {
    emit('requestOptimization', record);
    return;
  }

  // 其他状态直接更新
  try {
    const response = await updateTestCaseReviewStatus(
      currentProjectId.value,
      record.id,
      newStatus as ReviewStatus
    );
    if (response.success) {
      Message.success(pageText.value.reviewStatusUpdated);
      // 更新本地数据
      const index = testCaseData.value.findIndex(tc => tc.id === record.id);
      if (index !== -1) {
        testCaseData.value[index].review_status = newStatus as ReviewStatus;
      }
    } else {
      Message.error(response.error || pageText.value.reviewStatusUpdateFailed);
    }
  } catch (error) {
    Message.error(pageText.value.reviewStatusUpdateError);
  }
};

const handleBatchReviewStatusChange = async (newStatus: string | number | Record<string, unknown> | undefined) => {
  if (!currentProjectId.value || selectedTestCaseIds.value.length === 0) return;

  const reviewStatus = String(newStatus) as ReviewStatus;
  if (reviewStatus === 'needs_optimization') {
    Message.warning(isEnglish.value
      ? 'Batch update to "Optimize" is not supported. Please update cases individually.'
      : '批量修改为「优化」暂不支持，请逐条修改并填写优化说明。');
    return;
  }

  try {
    const response = await batchUpdateTestCaseReviewStatus(
      currentProjectId.value,
      selectedTestCaseIds.value,
      reviewStatus,
    );
    if (!response.success) {
      Message.error(response.error || pageText.value.batchReviewStatusFailed);
      return;
    }

    const updatedCount = response.data?.updated_count ?? selectedTestCaseIds.value.length;
    const statusLabel = getReviewStatusLabel(reviewStatus);
    Message.success(pageText.value.batchReviewStatusSuccess(updatedCount, statusLabel));

    const selectedIdSet = new Set(selectedTestCaseIds.value);
    testCaseData.value.forEach((testCase) => {
      if (selectedIdSet.has(testCase.id)) {
        testCase.review_status = reviewStatus;
      }
    });
  } catch (error) {
    Message.error(pageText.value.batchReviewStatusError);
  }
};

const onPageChange = (page: number) => {
  paginationConfig.current = page;
  fetchTestCases();
};

const onPageSizeChange = (pageSize: number) => {
  paginationConfig.pageSize = pageSize;
  paginationConfig.current = 1;
  fetchTestCases();
};

const handleAddTestCase = () => {
  if (!currentProjectId.value) {
    Message.warning(pageText.value.selectProjectFirst);
    return;
  }
  emit('addTestCase');
};

const handleGenerateTestCases = () => {
  if (!currentProjectId.value) {
    Message.warning(pageText.value.selectProjectFirst);
    return;
  }
  emit('generate-test-cases');
};

const handleViewTestCase = (testCase: TestCase) => {
  emit('viewTestCase', testCase);
};

const handleEditTestCase = (testCase: TestCase) => {
  emit('editTestCase', testCase);
};

const handleCopyTestCase = async (testCase: TestCase) => {
  if (!currentProjectId.value) return;

  try {
    loading.value = true;
    const response = await copyTestCaseService(currentProjectId.value, testCase.id);
    if (response.success) {
      Message.success(pageText.value.copyCaseSuccess);
      await fetchTestCases();
      emit('testCaseCopied');
    } else {
      Message.error(response.error || pageText.value.copyCaseFailed);
    }
  } catch (error) {
    console.error('复制测试用例出错:', error);
    Message.error(pageText.value.copyCaseError);
  } finally {
    loading.value = false;
  }
};

const handleDeleteTestCase = (testCase: TestCase) => {
  if (!currentProjectId.value) return;
  Modal.warning({
    title: pageText.value.confirmDeleteTitle,
    content: pageText.value.confirmDeleteContent(testCase.name),
    okText: pageText.value.confirmOk,
    onOk: async () => {
      try {
        const response = await deleteTestCaseService(currentProjectId.value!, testCase.id);
        if (response.success) {
          Message.success(pageText.value.deleteCaseSuccess);
          fetchTestCases(); // 重新加载列表
          emit('testCaseDeleted');
        } else {
          Message.error(response.error || pageText.value.deleteCaseFailed);
        }
      } catch (error) {
        Message.error(pageText.value.deleteCaseError);
      }
    },
  });
};

const handleExecuteTestCase = (testCase: TestCase) => {
  emit('executeTestCase', testCase);
};

// 批量删除处理函数
const handleBatchDelete = () => {
  if (!currentProjectId.value || selectedTestCaseIds.value.length === 0) return;

  // 获取选中的测试用例信息用于显示
  const selectedTestCases = testCaseData.value.filter(testCase =>
    selectedTestCaseIds.value.includes(testCase.id)
  );

  const testCaseNames = selectedTestCases.map(tc => tc.name).join('、');
  const displayNames = testCaseNames.length > 100 ?
    testCaseNames.substring(0, 100) + '...' : testCaseNames;

  Modal.warning({
    title: pageText.value.confirmBatchDeleteTitle,
    content: pageText.value.confirmBatchDeleteContent(selectedTestCaseIds.value.length, displayNames),
    okText: pageText.value.confirmBatchDeleteOk,
    width: 500,
    onOk: async () => {
      try {
        const response = await batchDeleteTestCases(currentProjectId.value!, selectedTestCaseIds.value);
        if (response.success && response.data) {
          // 显示详细的删除结果
          const { deleted_count, deletion_details } = response.data;

          let detailMessage = pageText.value.batchDeleteSuccess(deleted_count);
          if (deletion_details) {
            const details = Object.entries(deletion_details)
              .map(([key, count]) => `${key}: ${count}`)
              .join(', ');
            detailMessage += `\n${pageText.value.batchDeleteDetails(details)}`;
          }

          Message.success(detailMessage);

          // 清空选中状态并重新加载列表
          selectedTestCaseIds.value = [];
          fetchTestCases();
          emit('testCaseDeleted');
        } else {
          Message.error(response.error || pageText.value.batchDeleteFailed);
        }
      } catch (error) {
        console.error('批量删除测试用例出错:', error);
        Message.error(pageText.value.batchDeleteError);
      }
    },
  });
};

const openMoveModal = (testCaseIds: number[]) => {
  if (!currentProjectId.value || testCaseIds.length === 0) return;
  movingTestCaseIds.value = [...new Set(testCaseIds)];
  targetModuleId.value = undefined;
  moveModalVisible.value = true;
};

const handleConfirmMove = async (): Promise<boolean> => {
  if (!currentProjectId.value || !targetModuleId.value) {
    Message.warning(pageText.value.selectTargetModuleFirst);
    return false;
  }

  isMoving.value = true;
  try {
    const response = await batchMoveTestCases(
      currentProjectId.value,
      movingTestCaseIds.value,
      targetModuleId.value,
    );
    if (!response.success) {
      Message.error(response.error || pageText.value.moveCasesFailed);
      return false;
    }

    const movedCount = response.data?.moved_count ?? movingTestCaseIds.value.length;
    const moduleName = response.data?.target_module?.name || '';
    Message.success(pageText.value.moveCasesSuccess(movedCount, moduleName));
    selectedTestCaseIds.value = selectedTestCaseIds.value.filter(
      (testCaseId) => !movingTestCaseIds.value.includes(testCaseId),
    );
    fetchTestCases();
    emit('testCasesMoved');
    return true;
  } catch (error) {
    Message.error(pageText.value.moveCasesError);
    return false;
  } finally {
    isMoving.value = false;
  }
};



// 导出处理函数
const handleExport = () => {
  if (!currentProjectId.value) {
    Message.warning(pageText.value.selectProjectFirst);
    return;
  }
  exportModalRef.value?.open();
};

const handleImport = () => {
  if (!currentProjectId.value) {
    Message.warning(pageText.value.selectProjectFirst);
    return;
  }
  importModalRef.value?.open();
};

const onImportSuccess = () => {
  fetchTestCases();
};

onMounted(() => {
  handleResize(); // 初始化表格高度
  fetchTestCases();
  window.addEventListener('resize', handleResize);
});

onUnmounted(() => {
  window.removeEventListener('resize', handleResize);
});

watch(currentProjectId, () => {
  paginationConfig.current = 1;
  localSearchKeyword.value = '';
  selectedLevel.value = ''; // 项目切换时清空优先级筛选
  selectedTestType.value = ''; // 项目切换时清空测试类型筛选
  selectedOrdering.value = 'created_at'; // 项目切换时重置为最早创建在前
  selectedReviewStatuses.value = [...DEFAULT_REVIEW_STATUSES]; // 项目切换时重置审核状态筛选
  fetchTestCases();
});

// 监听外部模块选择变化（来自左侧模块管理面板）
watch(selectedModuleId, (newVal) => {
  if (newVal !== localSelectedModuleId.value) {
    localSelectedModuleId.value = newVal || null;
    paginationConfig.current = 1;
    fetchTestCases();
  }
});

// 暴露给父组件的方法
defineExpose({
  refreshTestCases: fetchTestCases,
  // 获取当前筛选后的用例ID列表（用于编辑页面导航）
  getTestCaseIds: () => testCaseData.value.map(tc => tc.id),
});

</script>

<style scoped>
.testcase-content {
  flex: 1;
  background-color: var(--theme-card-bg);
  color: var(--theme-page-text);
  border: 1px solid var(--theme-card-border);
  border-radius: 8px;
  padding: 16px;
  box-shadow: var(--theme-card-shadow);
  height: 100%;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-height: 0;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  flex-shrink: 0;
  flex-wrap: wrap;
  gap: 10px;
}

.search-box {
  display: flex;
  flex-wrap: nowrap;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
  overflow: hidden;
}

.search-box > * {
  margin-left: 0 !important;
  flex-shrink: 1;
}

.action-buttons {
  display: flex;
  flex-wrap: nowrap;
  gap: 8px;
  flex-shrink: 0;
}

.action-buttons > * {
  margin-right: 0 !important;
}

.search-input {
  width: 200px;
  min-width: 120px;
  flex-shrink: 1;
}

.level-filter {
  flex-shrink: 1;
}

.review-status-filter {
  width: 10px;
  flex-shrink: 0;
}

.test-type-filter {
  flex-shrink: 1;
}

.review-status-filter :deep(.arco-select-view-multiple) {
  flex-wrap: nowrap;
  overflow: hidden;
}

/* 导入导出按钮响应式 */
.io-btn {
  flex-shrink: 0;
}

@media (max-width: 1200px) {
  .io-btn-text {
    display: none;
  }
  .io-btn {
    width: 32px;
    padding: 0;
  }
  .io-btn :deep(.arco-btn-icon) {
    margin-right: 0;
  }
}

.no-project-selected {
  display: flex;
  justify-content: center;
  align-items: center;
  height: calc(100% - 60px); /* 减去头部高度 */
  flex-grow: 1;
}

.test-case-table {
  flex: 1;
  overflow: hidden;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

:deep(.test-case-table .arco-table) {
  width: 100%;
}

:deep(.test-case-table .arco-table-content-scroll) {
  overflow-x: auto !important;
}

.text-gray {
  color: var(--theme-text-tertiary);
}

:deep(.test-case-table .arco-table-td) {
  white-space: nowrap;
}

:deep(.test-case-table .arco-table-container) {
  height: 100% !important;
  display: flex;
  flex-direction: column;
}

/* 强制显示单元格下边框 */
:deep(.test-case-table .arco-table-td) {
  border-bottom: 1px solid var(--color-neutral-3) !important;
}

:deep(.test-case-table .arco-table-header) {
  flex-shrink: 0;
}

:deep(.test-case-table .arco-table-body) {
  flex: 1;
  min-height: 0;
  padding-bottom: 16px;
}

:deep(.test-case-table .arco-pagination) {
  flex-shrink: 0;
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  align-items: center;
  gap: 6px;
  position: sticky;
  bottom: 16px;
  background-color: var(--theme-card-bg);
  z-index: 1;
  padding: 8px 0;
  box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.04);
}

:deep(.test-case-table .arco-table-cell-fixed-right) {
  padding: 6px 4px;
}

:deep(.test-case-table .arco-space-compact) {
  display: flex;
  flex-wrap: nowrap;
}

:deep(.test-case-table .arco-btn-size-mini) {
  padding: 0 6px;
  font-size: 12px;
  min-width: 36px;
}

/* 勾选框居中显示 */
:deep(.test-case-table [data-checkbox]) {
  display: flex;
  justify-content: center;
  align-items: center;
  width: 100%;
  height: 100%;
}

:deep(.test-case-table .arco-table-tr-draggable) {
  cursor: grab;
}

:deep(.test-case-table .arco-table-tr-drag) {
  opacity: 0.72;
}

:deep(.test-case-table .arco-table-operation .arco-icon) {
  cursor: grab;
}

.testcase-name-link {
  color: var(--theme-accent);
  cursor: pointer;
  text-decoration: none;
  transition: color 0.2s;
}

.testcase-text-cell {
  display: inline-block;
  width: 100%;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  vertical-align: middle;
}

.testcase-name-link:hover {
  color: var(--theme-accent-hover);
  text-decoration: underline;
}

.run-status-tag {
  cursor: pointer;
}

/* 移除重复的样式定义 */
</style>
