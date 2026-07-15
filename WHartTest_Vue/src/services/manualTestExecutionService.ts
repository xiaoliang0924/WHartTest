import axios from 'axios';
import { API_BASE_URL } from '@/config/api';
import { useAuthStore } from '@/store/authStore';

export type ManualResultStatus = 'pending' | 'pass' | 'fail';

export interface ManualTestAssignment {
  id: number;
  run: number;
  run_name: string;
  testcase: number;
  testcase_detail: any;
  assignee: number;
  assignee_detail: { id: number; username: string; email: string };
  status: ManualResultStatus;
  failure_reason?: string;
  comment?: string;
  executed_at?: string;
  created_at: string;
}

const headers = () => ({ Authorization: `Bearer ${useAuthStore().getAccessToken}` });
const dataOf = (response: any) => response.data?.data ?? response.data;

export async function getManualAssignments(projectId: number, params: Record<string, any> = {}) {
  const response = await axios.get(`${API_BASE_URL}/projects/${projectId}/manual-test-assignments/`, { params, headers: headers() });
  const data = dataOf(response);
  return Array.isArray(data) ? data : data.results || [];
}

export async function getManualRuns(projectId: number, params: Record<string, any> = {}) {
  const response = await axios.get(`${API_BASE_URL}/projects/${projectId}/manual-test-runs/`, { params, headers: headers() });
  const data = dataOf(response);
  return Array.isArray(data) ? data : data.results || [];
}

export async function updateManualRun(projectId: number, runId: number, payload: { name?: string; description?: string }) {
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

export async function createManualTestRun(projectId: number, payload: { name: string; description?: string; testcase_ids: number[]; assignee_id: number }) {
  const response = await axios.post(`${API_BASE_URL}/projects/${projectId}/manual-test-runs/`, payload, { headers: headers() });
  return dataOf(response);
}

export async function submitManualResult(projectId: number, assignmentId: number, payload: { status: ManualResultStatus; failure_reason?: string; comment?: string }) {
  const response = await axios.post(`${API_BASE_URL}/projects/${projectId}/manual-test-assignments/${assignmentId}/result/`, payload, { headers: headers() });
  return dataOf(response);
}
