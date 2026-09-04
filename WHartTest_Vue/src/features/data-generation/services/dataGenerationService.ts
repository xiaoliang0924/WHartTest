import axios from 'axios';
import { API_BASE_URL } from '@/config/api';
import { useAuthStore } from '@/store/authStore';
import { normalizeListPayload } from '@/features/api-testing/services/responseHelpers';

export type DataGenerationStepType =
  | 'api_call'
  | 'set_env_var'
  | 'set_public_data'
  | 'sql'
  | 'custom_function'
  | 'delay';

export interface DataGenerationStep {
  type: DataGenerationStepType;
  name?: string;
  interface_id?: number;
  environment_id?: number;
  database_config_id?: number;
  function_id?: number;
  sql?: string;
  method?: string;
  seconds?: number | string;
  output_var?: string;
  variables?: Record<string, unknown>;
  extract?: Record<string, string>;
  args?: Record<string, unknown>;
  items?: Array<{ key: string; value: unknown; type?: number }>;
}

export interface DataGenerationPlan {
  id: number;
  project: number;
  name: string;
  description?: string;
  target_type: 'api' | 'ui' | 'both';
  steps: DataGenerationStep[];
  cleanup_steps?: DataGenerationStep[];
  default_environment?: number | null;
  default_environment_name?: string | null;
  is_active: boolean;
  is_template?: boolean;
  template_key?: string;
  template_icon?: string;
  template_params_schema?: Record<string, unknown>;
  template_bindings?: Record<string, unknown>;
  suggested_input_params?: Record<string, unknown>;
  step_count?: number;
  cleanup_step_count?: number;
  created_by?: number;
  created_by_name?: string;
  created_at: string;
  updated_at: string;
}

export interface DataGenerationRun {
  id: number;
  plan: number;
  plan_name?: string;
  project: number;
  status: 'pending' | 'running' | 'success' | 'failed';
  trigger_type: 'manual' | 'suite_pre' | 'cleanup';
  test_execution?: number | null;
  input_params?: Record<string, unknown>;
  output_snapshot?: Record<string, unknown>;
  step_logs?: DataGenerationStepLog[];
  failed_step_index?: number | null;
  error_message?: string;
  is_cleaned?: boolean;
  cleanup_status?: string;
  cleanup_logs?: Array<Record<string, unknown>>;
  cleanup_error_message?: string;
  parent_run?: number | null;
  triggered_by?: number | null;
  triggered_by_name?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  duration?: number | null;
  created_at: string;
}

export interface DataGenerationTemplate {
  template_key: string;
  name: string;
  description?: string;
  target_type?: string;
  icon?: string;
  params_schema?: Record<string, ParamSchemaField>;
  steps?: DataGenerationStep[];
  cleanup_steps?: DataGenerationStep[];
  /** 项目内已保存的造数计划 ID（快速造数直接试跑） */
  plan_id?: number;
}

export interface ParamSchemaField {
  type?: string;
  label?: string;
  default?: unknown;
  required?: boolean;
}

export type GenerationMethod = 'rule_match' | 'llm' | 'fallback' | 'rules';

export interface GenerationSummary {
  mode: 'template' | 'custom';
  generation_method?: GenerationMethod;
  template_key?: string | null;
  template_name?: string | null;
  step_count?: number;
  input_params?: Record<string, unknown>;
}

export interface DataGenerationStepLog {
  index?: number;
  type?: string;
  name?: string;
  status?: 'success' | 'failed' | 'failed_continued';
  continued?: boolean;
  error?: string;
  context_before?: Record<string, unknown>;
  context_after?: Record<string, unknown>;
  extracted?: Record<string, unknown>;
  variables?: Array<{ name: string; value: string }> | Record<string, unknown>;
  interface_name?: string;
  status_code?: number;
}

export interface GeneratedDataGenerationPlan extends Partial<DataGenerationPlan> {
  source?: string;
  hint?: string;
  llm_used?: boolean;
  generation_method?: GenerationMethod;
  generation_summary?: GenerationSummary;
  suggested_input_params?: Record<string, unknown>;
}

export interface SuiteVariableGapAnalysis {
  suite_id: number;
  suite_name: string;
  required_variables: string[];
  available_variables: string[];
  missing_variables: string[];
  testcases: Array<{ id: number; name: string; variables: string[] }>;
  suggestions: Array<Record<string, unknown>>;
  recommended_template_key?: string | null;
  recommended_description?: string;
}

export interface GenerateAndBindSuiteResult {
  plan: DataGenerationPlan;
  gap_analysis: SuiteVariableGapAnalysis;
  bound: boolean;
  created?: boolean;
  post_data_cleanup_enabled: boolean;
  generation_method?: GenerationMethod;
}

function authHeaders() {
  const authStore = useAuthStore();
  const token = authStore.getAccessToken;
  if (!token) {
    throw new Error('未登录或会话已过期');
  }
  return {
    Authorization: `Bearer ${token}`,
    'Content-Type': 'application/json',
    Accept: 'application/json',
  };
}

export async function getDataGenerationPlans(projectId: number, params?: Record<string, unknown>) {
  const response = await axios.get(`${API_BASE_URL}/projects/${projectId}/data-generation-plans/`, {
    headers: authHeaders(),
    params,
  });
  const { results, count } = normalizeListPayload(response.data?.data ?? response.data);
  return { results: results as DataGenerationPlan[], count };
}

export async function getDataGenerationPlan(projectId: number, planId: number) {
  const response = await axios.get(
    `${API_BASE_URL}/projects/${projectId}/data-generation-plans/${planId}/`,
    { headers: authHeaders() },
  );
  return (response.data?.data ?? response.data) as DataGenerationPlan;
}

export async function createDataGenerationPlan(
  projectId: number,
  payload: Partial<DataGenerationPlan>,
) {
  const response = await axios.post(
    `${API_BASE_URL}/projects/${projectId}/data-generation-plans/`,
    payload,
    { headers: authHeaders() },
  );
  return response.data;
}

export async function updateDataGenerationPlan(
  projectId: number,
  planId: number,
  payload: Partial<DataGenerationPlan>,
) {
  const response = await axios.patch(
    `${API_BASE_URL}/projects/${projectId}/data-generation-plans/${planId}/`,
    payload,
    { headers: authHeaders() },
  );
  return response.data;
}

export async function deleteDataGenerationPlan(projectId: number, planId: number) {
  const response = await axios.delete(
    `${API_BASE_URL}/projects/${projectId}/data-generation-plans/${planId}/`,
    { headers: authHeaders() },
  );
  return response.data;
}

export async function runDataGenerationPlan(
  projectId: number,
  planId: number,
  inputParams?: Record<string, unknown>,
  options?: { runAsync?: boolean },
) {
  const response = await axios.post(
    `${API_BASE_URL}/projects/${projectId}/data-generation-plans/${planId}/run/`,
    {
      input_params: inputParams || {},
      run_async: options?.runAsync ?? false,
    },
    { headers: authHeaders() },
  );
  return response.data;
}

const TERMINAL_RUN_STATUSES = new Set<DataGenerationRun['status']>(['success', 'failed']);

export function isDataGenerationRunTerminal(status?: string) {
  return TERMINAL_RUN_STATUSES.has(status as DataGenerationRun['status']);
}

function extractRunFromPayload(payload: unknown): DataGenerationRun {
  let current: unknown = payload;
  for (let i = 0; i < 4; i += 1) {
    if (!current || typeof current !== 'object') break;
    const record = current as Record<string, unknown>;
    if ('id' in record && 'status' in record) {
      return record as unknown as DataGenerationRun;
    }
    if (record.data !== undefined && record.data !== null && typeof record.data === 'object') {
      current = record.data;
      continue;
    }
    break;
  }
  throw new Error('无法解析造数执行记录');
}

function createAbortError() {
  return new DOMException('已取消等待', 'AbortError');
}

function throwIfAborted(signal?: AbortSignal) {
  if (signal?.aborted) {
    throw createAbortError();
  }
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  throwIfAborted(signal);
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(createAbortError());
    };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

export async function waitForDataGenerationRun(
  projectId: number,
  runId: number,
  options?: {
    onProgress?: (run: DataGenerationRun) => void;
    pollIntervalMs?: number;
    pollTimeoutMs?: number;
    signal?: AbortSignal;
  },
): Promise<DataGenerationRun> {
  const pollIntervalMs = options?.pollIntervalMs ?? 1000;
  const pollTimeoutMs = options?.pollTimeoutMs ?? 10 * 60 * 1000;
  const startedAt = Date.now();

  while (Date.now() - startedAt < pollTimeoutMs) {
    throwIfAborted(options?.signal);
    const run = await getDataGenerationRun(projectId, runId);
    options?.onProgress?.(run);
    if (isDataGenerationRunTerminal(run.status)) {
      return run;
    }
    await sleep(pollIntervalMs, options?.signal);
  }

  throw new Error('造数执行超时，请稍后在执行记录中查看结果');
}

export async function runDataGenerationPlanWithProgress(
  projectId: number,
  planId: number,
  inputParams?: Record<string, unknown>,
  options?: {
    onProgress?: (run: DataGenerationRun) => void;
    pollIntervalMs?: number;
    pollTimeoutMs?: number;
    signal?: AbortSignal;
  },
) {
  const response = await runDataGenerationPlan(projectId, planId, inputParams, { runAsync: true });
  const run = extractRunFromPayload(response);
  options?.onProgress?.(run);
  if (isDataGenerationRunTerminal(run.status)) {
    return { response, run };
  }
  const finalRun = await waitForDataGenerationRun(projectId, run.id, options);
  return {
    response: {
      ...response,
      status: finalRun.status === 'success' ? 'success' : 'error',
      message: finalRun.error_message,
      data: finalRun,
    },
    run: finalRun,
  };
}

export async function getDataGenerationRuns(projectId: number, params?: Record<string, unknown>) {
  const response = await axios.get(`${API_BASE_URL}/projects/${projectId}/data-generation-runs/`, {
    headers: authHeaders(),
    params,
  });
  const { results, count } = normalizeListPayload(response.data?.data ?? response.data);
  return { results: results as DataGenerationRun[], count };
}

export async function getDataGenerationRun(projectId: number, runId: number) {
  const response = await axios.get(
    `${API_BASE_URL}/projects/${projectId}/data-generation-runs/${runId}/`,
    { headers: authHeaders() },
  );
  return (response.data?.data ?? response.data) as DataGenerationRun;
}

export async function rerunDataGenerationRun(
  projectId: number,
  runId: number,
  options?: { runAsync?: boolean },
) {
  const response = await axios.post(
    `${API_BASE_URL}/projects/${projectId}/data-generation-runs/${runId}/rerun/`,
    { run_async: options?.runAsync ?? false },
    { headers: authHeaders() },
  );
  return response.data;
}

export async function rerunDataGenerationRunWithProgress(
  projectId: number,
  runId: number,
  options?: {
    onProgress?: (run: DataGenerationRun) => void;
    pollIntervalMs?: number;
    pollTimeoutMs?: number;
    signal?: AbortSignal;
  },
) {
  const response = await rerunDataGenerationRun(projectId, runId, { runAsync: true });
  const run = extractRunFromPayload(response);
  options?.onProgress?.(run);
  if (isDataGenerationRunTerminal(run.status)) {
    return { response, run };
  }
  const finalRun = await waitForDataGenerationRun(projectId, run.id, options);
  return {
    response: {
      ...response,
      status: finalRun.status === 'success' ? 'success' : 'error',
      message: finalRun.error_message,
      data: finalRun,
    },
    run: finalRun,
  };
}

export async function cleanupDataGenerationRun(projectId: number, runId: number) {
  const response = await axios.post(
    `${API_BASE_URL}/projects/${projectId}/data-generation-runs/${runId}/cleanup/`,
    {},
    { headers: authHeaders() },
  );
  return response.data;
}

/** 解包 Django 统一响应 + DRF action 可能的双层 { status, data } 结构。 */
function unwrapApiData<T>(payload: unknown): T {
  let current: unknown = payload;
  for (let i = 0; i < 4; i += 1) {
    if (!current || typeof current !== 'object') break;
    const record = current as Record<string, unknown>;
    if (record.data !== undefined && record.data !== null && typeof record.data === 'object') {
      current = record.data;
      continue;
    }
    break;
  }
  return current as T;
}

function unwrapTemplatePayload(payload: unknown): {
  builtin: DataGenerationTemplate[];
  saved: DataGenerationPlan[];
} {
  let current: any = payload;
  for (let i = 0; i < 5; i += 1) {
    if (!current || typeof current !== 'object') break;
    if (Array.isArray(current.builtin) || Array.isArray(current.saved)) {
      return {
        builtin: current.builtin || [],
        saved: current.saved || [],
      };
    }
    if (current.data && typeof current.data === 'object') {
      current = current.data;
      continue;
    }
    break;
  }
  return { builtin: [], saved: [] };
}

export async function getDataGenerationTemplates(projectId: number) {
  const response = await axios.get(
    `${API_BASE_URL}/projects/${projectId}/data-generation-plans/templates/`,
    { headers: authHeaders() },
  );
  return unwrapTemplatePayload(response.data);
}

export async function runDataGenerationTemplate(
  projectId: number,
  templateKey: string,
  inputParams?: Record<string, unknown>,
  defaultEnvironment?: number | null,
  options?: { runAsync?: boolean },
) {
  const response = await axios.post(
    `${API_BASE_URL}/projects/${projectId}/data-generation-plans/run_template/`,
    {
      template_key: templateKey,
      input_params: inputParams || {},
      default_environment: defaultEnvironment ?? null,
      run_async: options?.runAsync ?? false,
    },
    { headers: authHeaders() },
  );
  return response.data;
}

export async function runDataGenerationTemplateWithProgress(
  projectId: number,
  templateKey: string,
  inputParams?: Record<string, unknown>,
  defaultEnvironment?: number | null,
  options?: {
    onProgress?: (run: DataGenerationRun) => void;
    pollIntervalMs?: number;
    pollTimeoutMs?: number;
    signal?: AbortSignal;
  },
) {
  const response = await runDataGenerationTemplate(
    projectId,
    templateKey,
    inputParams,
    defaultEnvironment,
    { runAsync: true },
  );
  const run = extractRunFromPayload(response);
  options?.onProgress?.(run);
  if (isDataGenerationRunTerminal(run.status)) {
    return { response, run };
  }
  const finalRun = await waitForDataGenerationRun(projectId, run.id, options);
  return {
    response: {
      ...response,
      status: finalRun.status === 'success' ? 'success' : 'error',
      message: finalRun.error_message,
      data: finalRun,
    },
    run: finalRun,
  };
}

export async function generateDataGenerationPlan(
  projectId: number,
  description: string,
  defaultEnvironment?: number | null,
  options?: { useLlm?: boolean; suiteId?: number | null },
) {
  const response = await axios.post(
    `${API_BASE_URL}/projects/${projectId}/data-generation-plans/generate/`,
    {
      description,
      default_environment: defaultEnvironment ?? null,
      use_llm: options?.useLlm ?? true,
      suite_id: options?.suiteId ?? null,
    },
    { headers: authHeaders() },
  );
  return response.data?.data?.data ?? response.data?.data ?? response.data;
}

export async function analyzeSuiteVariableGaps(
  projectId: number,
  suiteId: number,
  environmentId?: number | null,
) {
  const response = await axios.post(
    `${API_BASE_URL}/projects/${projectId}/data-generation-plans/analyze_suite/`,
    {
      suite_id: suiteId,
      environment_id: environmentId ?? null,
    },
    { headers: authHeaders() },
  );
  return unwrapApiData<SuiteVariableGapAnalysis>(response.data);
}

export async function generateAndBindSuitePreData(
  projectId: number,
  suiteId: number,
  options?: {
    environmentId?: number | null;
    useLlm?: boolean;
    enablePostCleanup?: boolean;
    description?: string;
  },
) {
  const response = await axios.post(
    `${API_BASE_URL}/projects/${projectId}/data-generation-plans/generate_and_bind_suite/`,
    {
      suite_id: suiteId,
      environment_id: options?.environmentId ?? null,
      use_llm: options?.useLlm ?? true,
      enable_post_cleanup: options?.enablePostCleanup ?? true,
      description: options?.description || '',
    },
    { headers: authHeaders(), timeout: 90000 },
  );
  return unwrapApiData<GenerateAndBindSuiteResult>(response.data);
}

export const STEP_TYPE_OPTIONS = [
  { value: 'api_call', label: 'API 调用' },
  { value: 'set_env_var', label: '写入环境变量' },
  { value: 'set_public_data', label: '写入 UI 公共数据' },
  { value: 'sql', label: 'SQL 执行' },
  { value: 'custom_function', label: '自定义函数' },
  { value: 'delay', label: '等待' },
];

export const EXAMPLE_PLAN_STEPS: DataGenerationStep[] = [
  {
    type: 'api_call',
    name: '创建工单 TYPE_A',
    interface_id: 445,
    environment_id: 4,
    variables: { summary: '{{summary}}' },
    extract: { ticketId: 'ticketId', ticketNo: 'ticketNo' },
  },
  {
    type: 'set_env_var',
    name: '写入环境变量',
    environment_id: 4,
    variables: {
      ticketId: '{{ticketId}}',
      ticketNo: '{{ticketNo}}',
      processingTicketId: '{{ticketId}}',
    },
  },
  {
    type: 'set_public_data',
    name: '写入UI公共数据',
    items: [
      { key: 'ticketId', value: '{{ticketId}}', type: 0 },
      { key: 'ticketNo', value: '{{ticketNo}}', type: 0 },
      { key: 'work_order_id', value: '{{ticketId}}', type: 0 },
    ],
  },
];

export const EXAMPLE_CLEANUP_STEPS: DataGenerationStep[] = [
  {
    type: 'sql',
    name: '删除测试工单',
    database_config_id: 1,
    sql: 'DELETE FROM ticket WHERE id = {{ticketId}}',
    method: 'delete',
  },
];

const ENV_REQUIRED_STEP_TYPES: DataGenerationStepType[] = ['api_call', 'set_env_var'];

export const PLAN_ENV_REQUIRED_MESSAGE =
  '请选择默认 API 环境（存在未指定环境的 API / 写入环境变量步骤）';

export function planRequiresDefaultEnvironment(
  steps: DataGenerationStep[] = [],
  cleanupSteps: DataGenerationStep[] = [],
): boolean {
  return [...steps, ...cleanupSteps].some(
    (step) =>
      ENV_REQUIRED_STEP_TYPES.includes(step.type) && !step.environment_id,
  );
}

export function validatePlanEnvironment(payload: {
  default_environment?: number | null;
  steps?: DataGenerationStep[];
  cleanup_steps?: DataGenerationStep[];
}): string | null {
  if (!planRequiresDefaultEnvironment(payload.steps, payload.cleanup_steps)) {
    return null;
  }
  if (!payload.default_environment) {
    return PLAN_ENV_REQUIRED_MESSAGE;
  }
  return null;
}

export function getParamSchemaEntries(
  schema?: Record<string, ParamSchemaField>,
): Array<[string, ParamSchemaField]> {
  if (!schema || typeof schema !== 'object') {
    return [];
  }
  return Object.entries(schema).filter(
    (entry): entry is [string, ParamSchemaField] =>
      typeof entry[1] === 'object' && entry[1] !== null,
  );
}

export function buildDefaultInputParams(
  schema?: Record<string, ParamSchemaField>,
): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  getParamSchemaEntries(schema).forEach(([key, field]) => {
    result[key] = field.default ?? (field.type === 'number' ? 0 : '');
  });
  return result;
}

export function getGenerationMethodLabel(method?: GenerationMethod): string {
  const map: Record<GenerationMethod, string> = {
    rule_match: '规则匹配',
    llm: 'LLM 生成',
    fallback: 'LLM 失败已回退',
    rules: '规则引擎',
  };
  return method ? map[method] : '';
}

export function formatGenerationSummary(summary?: GenerationSummary): string {
  if (!summary) return '';
  const methodLabel = getGenerationMethodLabel(summary.generation_method);
  const prefix = methodLabel ? `【${methodLabel}】` : '';
  if (summary.mode === 'template') {
    const params = summary.input_params || {};
    const paramText = Object.entries(params)
      .map(([key, value]) => `${key}=${value}`)
      .join('，');
    return [
      `${prefix}匹配模板：${summary.template_name || summary.template_key || '未知'}`,
      `步骤数：${summary.step_count ?? '-'}`,
      paramText ? `参数：${paramText}` : '',
    ]
      .filter(Boolean)
      .join('；');
  }
  return `${prefix}自定义计划，共 ${summary.step_count ?? 0} 步（请检查步骤与环境配置）`;
}
