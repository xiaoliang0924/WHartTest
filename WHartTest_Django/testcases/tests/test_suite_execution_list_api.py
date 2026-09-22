"""列表接口：分页 + 轻量序列化（套件 / 执行历史）。"""

import json

from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from data_generation.models import DataGenerationPlan, DataGenerationRun
from projects.models import Project
from testcases.models import (
    TestCase as TestCaseModel,
    TestCaseModule,
    TestCaseResult,
    TestCaseStep,
    TestExecution,
    TestSuite,
)


class SuiteAndExecutionListApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username="list_api_user",
            password="password",
            email="list@example.com",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.project = Project.objects.create(
            name="List API Project",
            description="",
            creator=self.user,
        )
        self.module = TestCaseModule.objects.create(
            project=self.project,
            name="Mod",
            creator=self.user,
        )
        self.testcase = TestCaseModel.objects.create(
            project=self.project,
            module=self.module,
            name="Case With Steps",
            precondition="pre",
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=self.testcase,
            step_number=1,
            description="step one",
            expected_result="ok",
        )

        self.suite = TestSuite.objects.create(
            project=self.project,
            name="Suite A",
            creator=self.user,
        )
        self.suite.testcases.add(self.testcase)

        plan = DataGenerationPlan.objects.create(
            project=self.project,
            name="plan",
            steps=[{"type": "api_call", "name": "x"}],
            created_by=self.user,
        )
        run = DataGenerationRun.objects.create(
            plan=plan,
            project=self.project,
            status=DataGenerationRun.STATUS_FAILED,
            trigger_type=DataGenerationRun.TRIGGER_SUITE_PRE,
            input_params={},
            error_message="boom",
            step_logs=[{"index": 1, "name": "heavy", "status": "failed"}],
            output_snapshot={"ticketNo": "T001"},
        )
        self.execution = TestExecution.objects.create(
            suite=self.suite,
            executor=self.user,
            status="completed",
            total_count=1,
            passed_count=0,
            failed_count=1,
            data_generation_run=run,
        )
        TestCaseResult.objects.create(
            execution=self.execution,
            testcase=self.testcase,
            status="fail",
            execution_log="x" * 5000,
            error_message="fail reason",
        )

        self.base = f"/api/projects/{self.project.id}"

    @staticmethod
    def _list_payload(response):
        data = response.data
        if isinstance(data, dict) and "results" in data:
            return data
        inner = data.get("data") if isinstance(data, dict) else None
        if isinstance(inner, dict) and "results" in inner:
            return inner
        raise AssertionError(f"unexpected list shape: {data!r}")

    @staticmethod
    def _detail_payload(response):
        data = response.data
        if isinstance(data, dict) and "id" in data:
            return data
        inner = data.get("data") if isinstance(data, dict) else None
        if isinstance(inner, dict) and "id" in inner:
            return inner
        raise AssertionError(f"unexpected detail shape: {data!r}")

    def test_test_suites_list_is_paginated_and_light(self):
        url = f"{self.base}/test-suites/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        inner = self._list_payload(response)
        self.assertEqual(inner["count"], 1)
        self.assertEqual(len(inner["results"]), 1)

        item = inner["results"][0]
        self.assertEqual(item["name"], "Suite A")
        self.assertEqual(item["testcase_count"], 1)
        self.assertNotIn("testcases_detail", item)

        body = json.dumps(response.data, ensure_ascii=False)
        self.assertLess(len(body.encode()), 8000)

    def test_test_executions_list_is_paginated_and_light(self):
        url = f"{self.base}/test-executions/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        inner = self._list_payload(response)
        self.assertEqual(inner.get("count"), 1)
        item = inner["results"][0]

        self.assertNotIn("results", item)
        self.assertIn("suite_detail", item)
        self.assertEqual(set(item["suite_detail"].keys()), {"id", "name", "description"})
        detail = item.get("data_generation_run_detail") or {}
        self.assertEqual(detail.get("error_message"), "boom")
        self.assertNotIn("step_logs", detail)
        self.assertNotIn("output_snapshot", detail)

        body = json.dumps(response.data, ensure_ascii=False)
        self.assertLess(len(body.encode()), 12000)

    def test_test_executions_list_status_filter(self):
        TestExecution.objects.create(
            suite=self.suite,
            executor=self.user,
            status="running",
            total_count=0,
        )
        url = f"{self.base}/test-executions/?status=completed"
        response = self.client.get(url)
        inner = self._list_payload(response)
        self.assertEqual(inner.get("count"), 1)
        self.assertEqual(inner["results"][0]["status"], "completed")

    def test_test_executions_retrieve_still_has_results(self):
        url = f"{self.base}/test-executions/{self.execution.id}/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = self._detail_payload(response)
        self.assertIn("results", data)
        self.assertGreaterEqual(len(data["results"]), 1)
