from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase as DjangoTestCase
from rest_framework.test import APIClient

from .models import ManualTestAssignment, ManualTestRun
from .models import TestCase as TestCaseModel
from .models import TestCaseModule, TestCaseStep
from projects.models import Project, ProjectMember


class TestCaseReviewStatusTests(SimpleTestCase):
    def test_pending_product_confirmation_is_a_valid_review_status(self):
        choices = dict(TestCaseModel.REVIEW_STATUS_CHOICES)

        self.assertEqual(choices['pending_product_confirmation'], '待产品确认')


class TestCaseBatchMoveTests(DjangoTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="batch-move-admin",
            password="test-password",
            email="batch-move-admin@example.com",
        )
        self.project = Project.objects.create(name="Batch move project", creator=self.user)
        self.source_module = TestCaseModule.objects.create(
            project=self.project,
            name="Source module",
            creator=self.user,
        )
        self.target_module = TestCaseModule.objects.create(
            project=self.project,
            name="Target module",
            creator=self.user,
        )
        self.testcases = [
            TestCaseModel.objects.create(
                project=self.project,
                module=self.source_module,
                name=f"Case {index}",
                creator=self.user,
            )
            for index in range(2)
        ]
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_moves_selected_testcases_to_target_module(self):
        response = self.client.post(
            f"/api/projects/{self.project.id}/testcases/batch-move/",
            {
                "ids": [testcase.id for testcase in self.testcases],
                "target_module_id": self.target_module.id,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            TestCaseModel.objects.filter(
                id__in=[testcase.id for testcase in self.testcases],
                module=self.target_module,
            ).count(),
            2,
        )

    def test_moves_module_subtree_under_target_module(self):
        child_module = TestCaseModule.objects.create(
            project=self.project,
            name="Child module",
            parent=self.source_module,
            creator=self.user,
        )
        TestCaseModel.objects.create(
            project=self.project,
            module=child_module,
            name="Case in child module",
            creator=self.user,
        )

        response = self.client.post(
            f"/api/projects/{self.project.id}/testcases/batch-move-by-module/",
            {
                "source_module_id": self.source_module.id,
                "target_module_id": self.target_module.id,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.source_module.refresh_from_db()
        child_module.refresh_from_db()
        self.assertEqual(self.source_module.parent_id, self.target_module.id)
        self.assertEqual(child_module.parent_id, self.source_module.id)
        self.assertEqual(
            TestCaseModel.objects.filter(module=child_module).count(),
            1,
        )
        self.assertEqual(
            TestCaseModel.objects.filter(module=self.target_module).count(),
            0,
        )

    def test_rejects_move_module_to_descendant(self):
        child_module = TestCaseModule.objects.create(
            project=self.project,
            name="Child module",
            parent=self.source_module,
            creator=self.user,
        )
        response = self.client.post(
            f"/api/projects/{self.project.id}/testcases/batch-move-by-module/",
            {
                "source_module_id": self.source_module.id,
                "target_module_id": child_module.id,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)


class TestCaseBatchReviewStatusTests(DjangoTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="batch-review-admin",
            password="test-password",
            email="batch-review-admin@example.com",
        )
        self.project = Project.objects.create(name="Batch review project", creator=self.user)
        self.module = TestCaseModule.objects.create(
            project=self.project,
            name="Review module",
            creator=self.user,
        )
        self.testcases = [
            TestCaseModel.objects.create(
                project=self.project,
                module=self.module,
                name=f"Case {index}",
                review_status="pending_review",
                creator=self.user,
            )
            for index in range(3)
        ]
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_updates_review_status_for_selected_testcases(self):
        response = self.client.post(
            f"/api/projects/{self.project.id}/testcases/batch-update-review-status/",
            {
                "ids": [testcase.id for testcase in self.testcases],
                "review_status": "approved",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            TestCaseModel.objects.filter(
                id__in=[testcase.id for testcase in self.testcases],
                review_status="approved",
            ).count(),
            3,
        )

    def test_rejects_invalid_review_status(self):
        response = self.client.post(
            f"/api/projects/{self.project.id}/testcases/batch-update-review-status/",
            {
                "ids": [self.testcases[0].id],
                "review_status": "invalid_status",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)


class AssignedTestCaseDeletionTests(DjangoTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="manual-run-admin",
            password="test-password",
            email="manual-run-admin@example.com",
        )
        self.project = Project.objects.create(name="Manual run project", creator=self.user)
        ProjectMember.objects.create(project=self.project, user=self.user, role="owner")
        self.module = TestCaseModule.objects.create(
            project=self.project,
            name="Manual module",
            creator=self.user,
        )
        self.testcase = TestCaseModel.objects.create(
            project=self.project,
            module=self.module,
            name="Case retained in history",
            precondition="Signed in",
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=self.testcase,
            step_number=1,
            description="Submit the form",
            expected_result="Submission succeeds",
            creator=self.user,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def create_manual_run(self):
        create_response = self.client.post(
            f"/api/projects/{self.project.id}/manual-test-runs/",
            {
                "name": "Regression run",
                "testcase_ids": [self.testcase.id],
                "assignee_id": self.user.id,
            },
            format="json",
        )
        self.assertEqual(create_response.status_code, 201)
        return ManualTestRun.objects.get()

    def test_delete_preserves_manual_execution_snapshot(self):
        run = self.create_manual_run()

        assignment = ManualTestAssignment.objects.get(run=run)
        assignment.status = "pass"
        assignment.save(update_fields=["status"])

        delete_response = self.client.delete(
            f"/api/projects/{self.project.id}/testcases/{self.testcase.id}/"
        )
        self.assertEqual(delete_response.status_code, 200)

        assignment.refresh_from_db()
        self.assertIsNone(assignment.testcase_id)
        self.assertEqual(assignment.status, "pass")
        self.assertEqual(assignment.testcase_snapshot["name"], "Case retained in history")
        self.assertEqual(
            assignment.testcase_snapshot["steps"][0]["expected_result"],
            "Submission succeeds",
        )

        detail_response = self.client.get(
            f"/api/projects/{self.project.id}/manual-test-runs/{run.id}/"
        )
        self.assertEqual(detail_response.status_code, 200)
        assignment_data = detail_response.data["assignments"][0]
        self.assertIn("testcase", assignment_data, assignment_data)
        self.assertIn("testcase_detail", assignment_data, assignment_data)
        self.assertEqual(assignment_data["testcase"], self.testcase.id)
        self.assertEqual(
            assignment_data["testcase_detail"]["name"],
            "Case retained in history",
        )

    def test_batch_delete_preserves_manual_execution_snapshot(self):
        run = self.create_manual_run()
        assignment = ManualTestAssignment.objects.get(run=run)
        assignment.status = "fail"
        assignment.failure_reason = "Observed an error"
        assignment.save(update_fields=["status", "failure_reason"])

        response = self.client.post(
            f"/api/projects/{self.project.id}/testcases/batch-delete/",
            {"ids": [self.testcase.id]},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        assignment.refresh_from_db()
        self.assertIsNone(assignment.testcase_id)
        self.assertEqual(assignment.status, "fail")
        self.assertEqual(assignment.failure_reason, "Observed an error")
        self.assertEqual(
            assignment.testcase_snapshot["name"],
            "Case retained in history",
        )
