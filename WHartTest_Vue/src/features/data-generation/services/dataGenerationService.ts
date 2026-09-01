import axios from 'axios';
import { API_BASE_URL } from '@/config/api';
import { useAuthStore } from '@/store/authStore';
import { normalizeListPayload } from '@/features/api-testing/services/responseHelpers';

export type DataGenerationStepType = 'api_call' | 'set_env_var' | 'set_public_data';

export interface DataGenerationStep {
  type: DataGenerationStepType;
  name?: string;
  interface_id?: number;
  environment_id?: number;
  variables?: Record<string, unknown>;
  extract?: Record<string, string>;
  items?: Array<{ key: string; value: unknown; type?: number }>;
}

export interface DataGenerationPlan {
  id: number;
  project: number;
  name: string;
  description?: string;
  target_type: 'api' | 'ui' | 'both';
  steps: DataGenerationStep[];
  default_environment?: number | null;
  default_environment_name?: string | null;
  is_active: boolean;
  step_count?: number;
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
  trigger_type: 'manual' | 'suite_pre';
  test_execution?: number | null;
  input_params?: Record<string, unknown>;
  output_snapshot?: Record<string, unknown>;
  step_logs?: Array<Record<string, unknown>>;
  error_message?: string;
  triggered_by?: number | null;
  triggered_by_name?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  duration?: number | null;
  created_at: string;
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
) {
  const response = await axios.post(
    `${API_BASE_URL}/projects/${projectId}/data-generation-plans/${planId}/run/`,
    { input_params: inputParams || {} },
    { headers: authHeaders() },
  );
  return response.data;
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

export const DEFAULT_API_CALL_STEP: DataGenerationStep = {
  type: 'api_call',
  name: '调用接口',
  interface_id: undefined,
  extract: {},
};

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
