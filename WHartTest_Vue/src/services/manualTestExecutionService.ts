import axios from 'axios';
import { API_BASE_URL } from '@/config/api';
import { useAuthStore } from '@/store/authStore';
import { normalizeListPayload } from '@/features/api-testing/services/responseHelpers';

export type ManualResultStatus = 'pending' | 'pass' | 'fail' | 'blocked' | 'skip';
export type ManualRunStatus = 'pending' | 'in_progress' | 'completed';

export interface ManualStepResult {
  step_number: number;
  status: ManualResultStatus;
  comment?: string;
}

export interface ManualEvidenceFile {
  name: string;
  url: string;
  uploaded_at?: string;
}

export interface ManualTestAssignment {
  id: number;
  run: number;
  run_name: string;
  run_status?: ManualRunStatus;
  run_total_count?: number;
  run_pending_count?: number;
  run_passed_count?: number;
  run_failed_count?: number;
  run_created_at?: string;
  testcase: number;
  testcase_detail: any;
  assignee: number;
  assignee_detail: { id: number; username: string; email: string };
  status: ManualResultStatus;
  failure_reason?: string;
  comment?: string;
  step_results?: ManualStepResult[];
  evidence_files?: ManualEvidenceFile[];
  defect_title?: string;
  defect_url?: string;
  executed_at?: string;
  created_at: string;
}

export interface ManualTodoSummary {
  pending_count: number;
  run_count: number;
  today_completed_count?: number;
  overdue_count?: number;
  level_counts?: Record<string, number>;
  earliest_pending_at?: string | null;
  runs: Array<{
    id: number;
    name: string;
    status: ManualRunStatus;
    total_count: number;
    pending_count: number;
    passed_count: number;
    failed_count: number;
    created_at: string;
  }>;
}

export interface ManualTeamTodoSummary {
  pending_count: number;
  run_count: number;
  overdue_count?: number;
  members: Array<{ assignee_id: number; username: string; pending_count: number }>;
}

export interface ManualTestRunListItem {
  id: number;
  name: string;
  description?: string;
  environment?: string;
  version?: string;
  deadline?: string | null;
  test_suite?: number | null;
  test_suite_name?: string | null;
  is_overdue?: boolean;
  status: ManualRunStatus;
  creator_detail?: { id: number; username: string; email: string };
  assignee_detail?: { id: number; username: string; email: string };
  total_count: number;
  passed_count: number;
  failed_count: number;
  blocked_count?: number;
  skip_count?: number;
  pending_count: number;
  pass_rate: number;
  created_at: string;
  updated_at: string;
}

export interface ManualTestRunDetail extends ManualTestRunListItem {
  assignments: ManualTestAssignment[];
}

export interface ManualTestReport {
  run_id: number;
  name: string;
  description?: string;
  environment?: string;
  version?: string;
  deadline?: string | null;
  test_suite_name?: string;
  status: ManualRunStatus;
  creator?: { id: number; username: string };
  assignee?: { id: number; username: string };
  created_at: string;
  updated_at: string;
  statistics: {
    total: number;
    passed: number;
    failed: number;
    blocked?: number;
    skip?: number;
    pending: number;
    executed: number;
    pass_rate: number;
  };
  results: Array<{
    assignment_id: number;
    testcase_id: number;
    testcase_name: string;
    module_name: string;
    status: ManualResultStatus;
    failure_reason: string;
    comment: string;
    defect_title?: string;
    defect_url?: string;
    step_results?: ManualStepResult[];
    evidence_files?: ManualEvidenceFile[];
    executed_at?: string;
    assignee: string;
  }>;
}

const headers = () => ({ Authorization: `Bearer ${useAuthStore().getAccessToken}` });
const dataOf = (response: any) => response.data?.data ?? response.data;

function listFromResponse(response: any) {
  const data = dataOf(response);
  if (Array.isArray(data)) {
    return { results: data, count: data.length };
  }
  return normalizeListPayload(data, data?.count);
}

export async function getManualRuns(projectId: number, params: Record<string, any> = {}) {
  const response = await axios.get(`${API_BASE_URL}/projects/${projectId}/manual-test-runs/`, {
    params: {
      ...params,
      page: params.page,
      page_size: params.pageSize ?? params.page_size,
    },
    headers: headers(),
  });
  const { results, count } = listFromResponse(response);
  return { results, total: count };
}

export async function getManualRun(projectId: number, runId: number) {
  const response = await axios.get(`${API_BASE_URL}/projects/${projectId}/manual-test-runs/${runId}/`, { headers: headers() });
  return dataOf(response) as ManualTestRunDetail;
}

export async function getManualTodoSummary(projectId: number, params: Record<string, any> = {}) {
  const response = await axios.get(`${API_BASE_URL}/projects/${projectId}/manual-test-assignments/todo-summary/`, { params, headers: headers() });
  return dataOf(response) as ManualTodoSummary;
}

export async function getTeamTodoSummary(projectId: number, params: Record<string, any> = {}) {
  const response = await axios.get(`${API_BASE_URL}/projects/${projectId}/manual-test-assignments/team-todo-summary/`, { params, headers: headers() });
  return dataOf(response) as ManualTeamTodoSummary;
}

export async function getNextPendingAssignment(projectId: number, params: Record<string, any> = {}) {
  const response = await axios.get(`${API_BASE_URL}/projects/${projectId}/manual-test-assignments/next-pending/`, { params, headers: headers() });
  const data = dataOf(response);
  return (data || null) as ManualTestAssignment | null;
}

export async function getManualAssignments(projectId: number, params: Record<string, any> = {}) {
  const response = await axios.get(`${API_BASE_URL}/projects/${projectId}/manual-test-assignments/`, {
    params: {
      ...params,
      page: params.page,
      page_size: params.pageSize ?? params.page_size,
    },
    headers: headers(),
  });
  const { results, count } = listFromResponse(response);
  return { results: results as ManualTestAssignment[], total: count };
}

export async function getManualRunReport(projectId: number, runId: number) {
  const response = await axios.get(`${API_BASE_URL}/projects/${projectId}/manual-test-runs/${runId}/report/`, { headers: headers() });
  return dataOf(response) as ManualTestReport;
}

export async function exportManualRunExcel(projectId: number, runId: number, filename: string) {
  const response = await axios.get(`${API_BASE_URL}/projects/${projectId}/manual-test-runs/${runId}/export-excel/`, {
    headers: headers(),
    responseType: 'blob',
  });
  const blob = new Blob([response.data], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
  const url = window.URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename.endsWith('.xlsx') ? filename : `${filename}.xlsx`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  window.URL.revokeObjectURL(url);
}

export async function reassignManualRun(projectId: number, runId: number, assigneeId: number) {
  const response = await axios.post(`${API_BASE_URL}/projects/${projectId}/manual-test-runs/${runId}/reassign/`, { assignee_id: assigneeId }, { headers: headers() });
  return dataOf(response) as ManualTestRunDetail;
}

export async function updateManualRun(projectId: number, runId: number, payload: {
  name?: string;
  description?: string;
  environment?: string;
  version?: string;
  deadline?: string | null;
}) {
  const response = await axios.patch(`${API_BASE_URL}/projects/${projectId}/manual-test-runs/${runId}/`, payload, { headers: headers() });
  return dataOf(response);
}

export async function deleteManualRun(projectId: number, runId: number) {
  await axios.delete(`${API_BASE_URL}/projects/${projectId}/manual-test-runs/${runId}/`, { headers: headers() });
}

export async function addManualRunCases(projectId: number, runId: number, payload: { testcase_ids: number[]; assignee_id: number }) {
  const response = await axios.post(`${API_BASE_URL}/projects/${projectId}/manual-test-runs/${runId}/assign/`, payload, { headers: headers() });
  return dataOf(response);
}

export async function removeManualRunCase(projectId: number, runId: number, testcaseId: number) {
  const response = await axios.post(`${API_BASE_URL}/projects/${projectId}/manual-test-runs/${runId}/remove-assignment/`, { testcase_id: testcaseId }, { headers: headers() });
  return dataOf(response);
}

export async function createManualTestRun(projectId: number, payload: {
  name: string;
  description?: string;
  environment?: string;
  version?: string;
  deadline?: string | null;
  testsuite_id?: number;
  testcase_ids?: number[];
  assignee_id: number;
}) {
  const response = await axios.post(`${API_BASE_URL}/projects/${projectId}/manual-test-runs/`, payload, { headers: headers() });
  return dataOf(response);
}

export async function submitManualResult(
  projectId: number,
  assignmentId: number,
  payload: {
    status: ManualResultStatus;
    failure_reason?: string;
    comment?: string;
    step_results?: ManualStepResult[];
    evidence_files?: ManualEvidenceFile[];
    defect_title?: string;
    defect_url?: string;
  },
) {
  const response = await axios.post(`${API_BASE_URL}/projects/${projectId}/manual-test-assignments/${assignmentId}/result/`, payload, { headers: headers() });
  return dataOf(response);
}

export async function uploadManualEvidence(projectId: number, assignmentId: number, files: File[]) {
  const formData = new FormData();
  files.forEach(file => formData.append('files', file));
  const response = await axios.post(
    `${API_BASE_URL}/projects/${projectId}/manual-test-assignments/${assignmentId}/upload-evidence/`,
    formData,
    { headers: { ...headers(), 'Content-Type': 'multipart/form-data' } },
  );
  return dataOf(response) as ManualTestAssignment;
}
