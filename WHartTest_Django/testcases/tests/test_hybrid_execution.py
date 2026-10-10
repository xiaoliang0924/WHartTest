from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APITransactionTestCase, APIClient
from unittest.mock import patch

from projects.models import Project
from testcases.models import TestCase as TestCaseModel, TestCaseModule, TestCaseStep
from ui_automation.models import UiModule, UiPage, UiElement, UiPageSteps, UiPageStepsDetailed, UiTestCase, UiCaseStepsDetailed, UiExecutionRecord
from testcases.ai_diagnosis_service import diagnose_execution_failure, extract_failure_context


class HybridExecutionAndBindingTests(APITransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username="testuser",
            password="password",
            email="test@example.com",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.project = Project.objects.create(
            name="Test Project",
            description="Test Description",
            creator=self.user,
        )
        self.module = TestCaseModule.objects.create(
            project=self.project,
            name="Test Module",
            creator=self.user,
        )
        self.testcase = TestCaseModel.objects.create(
            project=self.project,
            module=self.module,
            name="功能测试用例1",
            precondition="前置条件1",
            creator=self.user,
            review_status="pending_review",
        )
        TestCaseStep.objects.create(
            test_case=self.testcase,
            step_number=1,
            description="点击登录按钮",
            expected_result="成功跳转首页",
            creator=self.user,
        )

        # 创建 UI 自动化资产
        self.ui_module = UiModule.objects.create(
            project=self.project,
            name="UI模块",
            creator=self.user,
        )
        self.ui_page = UiPage.objects.create(
            project=self.project,
            module=self.ui_module,
            name="登录页",
            creator=self.user,
        )
        self.ui_element = UiElement.objects.create(
            page=self.ui_page,
            name="登录按钮",
            locator_type="xpath",
            locator_value="//button[@id='old-login-btn']",
            creator=self.user,
        )
        self.ui_page_step = UiPageSteps.objects.create(
            project=self.project,
            page=self.ui_page,
            module=self.ui_module,
            name="点击登录步骤",
            creator=self.user,
        )
        UiPageStepsDetailed.objects.create(
            page_step=self.ui_page_step,
            step_type=0,
            element=self.ui_element,
            ope_key="click",
            step_sort=1,
        )
        self.ui_testcase = UiTestCase.objects.create(
            project=self.project,
            module=self.ui_module,
            name="UI登录自动化用例",
            creator=self.user,
        )
        UiCaseStepsDetailed.objects.create(
            test_case=self.ui_testcase,
            page_step=self.ui_page_step,
            case_sort=1,
        )

    def test_bind_ui_testcase_success(self):
        """测试绑定 UI 自动化用例接口"""
        url = f"/api/projects/{self.project.id}/testcases/{self.testcase.id}/bind-ui-testcase/"
        response = self.client.post(
            url,
            {
                "ui_test_case_id": self.ui_testcase.id,
                "execution_mode": "hybrid",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.testcase.refresh_from_db()
        self.assertEqual(self.testcase.ui_test_case_id, self.ui_testcase.id)
        self.assertEqual(self.testcase.execution_mode, "hybrid")
        self.assertIsNotNone(response.data.get("ui_test_case_detail"))
        self.assertEqual(response.data["ui_test_case_detail"]["id"], self.ui_testcase.id)

    def test_bind_ui_testcase_accepts_complete_explicit_step_mapping(self):
        ui_group = self.ui_testcase.case_steps.get()
        url = f"/api/projects/{self.project.id}/testcases/{self.testcase.id}/bind-ui-testcase/"
        response = self.client.post(
            url,
            {
                "ui_test_case_id": self.ui_testcase.id,
                "execution_mode": "hybrid",
                "ui_step_mapping": {
                    "groups": [{
                        "ui_case_step_id": ui_group.id,
                        "functional_step_numbers": [1],
                    }],
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.testcase.refresh_from_db()
        self.assertEqual(
            self.testcase.ui_step_mapping["groups"][0]["functional_step_numbers"],
            [1],
        )

    def test_bind_ui_testcase_rejects_incomplete_explicit_step_mapping(self):
        url = f"/api/projects/{self.project.id}/testcases/{self.testcase.id}/bind-ui-testcase/"
        response = self.client.post(
            url,
            {
                "ui_test_case_id": self.ui_testcase.id,
                "ui_step_mapping": {"groups": []},
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.testcase.refresh_from_db()
        self.assertIsNone(self.testcase.ui_test_case_id)

    def test_unbind_ui_testcase_success(self):
        """测试解绑 UI 自动化用例"""
        self.testcase.ui_test_case = self.ui_testcase
        self.testcase.save()

        url = f"/api/projects/{self.project.id}/testcases/{self.testcase.id}/bind-ui-testcase/"
        response = self.client.post(
            url,
            {"ui_test_case_id": None},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.testcase.refresh_from_db()
        self.assertIsNone(self.testcase.ui_test_case)

    def test_diagnose_failure_rule_fallback(self):
        """测试失败诊断接口在无外部 LLM 时的规则回退诊断能力"""
        self.testcase.ui_test_case = self.ui_testcase
        self.testcase.save()

        # 创建一条失败的 UI 执行记录
        record = UiExecutionRecord.objects.create(
            test_case=self.ui_testcase,
            executor=self.user,
            status=3,  # 失败
            error_message="Timeout 5000ms waiting for locator //button[@id='old-login-btn']",
            step_results=[
                {
                    "step_id": 1,
                    "status": 3,
                    "error_message": "Timeout 5000ms waiting for locator //button[@id='old-login-btn']",
                }
            ],
        )

        url = f"/api/projects/{self.project.id}/testcases/{self.testcase.id}/diagnose-failure/"
        response = self.client.post(
            url,
            {"ui_execution_record_id": record.id},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("root_cause_type", response.data)
        self.assertEqual(response.data["root_cause_type"], "SCRIPT_DEFECT")
        self.assertTrue(response.data["healing_suggestion"]["can_self_heal"])

    def test_hidden_menu_click_on_recorded_target_route_is_diagnosed_as_redundant_navigation(self):
        self.testcase.ui_test_case = self.ui_testcase
        self.testcase.save()
        self.ui_page.url = "https://example.test/work-order/my-tickets"
        self.ui_page.save(update_fields=["url"])
        self.ui_element.name = "我的工单菜单项"
        self.ui_element.locator_type = "xpath"
        self.ui_element.locator_value = "//li[@role='menuitem' and normalize-space()='我的工单']"
        self.ui_element.save()
        detail = self.ui_page_step.step_details.get()
        record = UiExecutionRecord.objects.create(
            test_case=self.ui_testcase,
            executor=self.user,
            status=3,
            error_message="locator.click: Timeout 15000ms exceeded; element is not visible",
            step_results=[{
                "step_id": detail.id,
                "status": 3,
                "description": "click",
                "page_url": "https://example.test/work-order/my-tickets",
                "error_message": (
                    "locator.click: Timeout 15000ms exceeded; "
                    "locator resolved to <li role=menuitem>; element is not visible"
                ),
            }],
        )

        with patch("testcases.ai_diagnosis_service.LLMConfig.objects.filter") as configs:
            configs.return_value.first.return_value = None
            diagnosis = diagnose_execution_failure(
                self.testcase, ui_record_id=record.id, user=self.user,
            )

        self.assertEqual(diagnosis["root_cause_type"], "SCRIPT_DEFECT")
        self.assertEqual(diagnosis["healing_suggestion"]["repair_type"], "navigation_assertion")
        self.assertTrue(diagnosis["healing_suggestion"]["can_self_heal"])
        self.assertEqual(diagnosis["evidence"]["page_url"], self.ui_page.url)

    def test_recorder_group_failure_maps_exact_operation_and_page_route(self):
        """Runtime group/index evidence must map to the actual failed click."""
        self.testcase.ui_test_case = self.ui_testcase
        self.testcase.save()
        self.ui_page.url = "https://example.test/work-order/tickets"
        self.ui_page.save(update_fields=["url"])
        first_click = self.ui_element
        first_click.locator_value = "//li[@role='menuitem' and normalize-space()='工单中心']"
        first_click.save(update_fields=["locator_value"])
        failed_menu_item = UiElement.objects.create(
            page=self.ui_page,
            name="工单列表菜单项",
            locator_type="text",
            locator_value="工单列表",
            creator=self.user,
        )
        UiPageStepsDetailed.objects.create(
            page_step=self.ui_page_step,
            step_type=0,
            element=failed_menu_item,
            ope_key="click",
            step_sort=2,
        )
        record = UiExecutionRecord.objects.create(
            test_case=self.ui_testcase,
            executor=self.user,
            status=3,
            error_message="locator.click: Timeout 15000ms exceeded; element is not visible",
            step_results=[{
                "step_id": "group1_1",
                "status": "success",
                "description": "goto",
            }, {
                "step_id": "group1_2",
                "status": "success",
                "description": "click",
            }, {
                # index 1 is the synthetic goto; index 3 is stored click #2.
                "step_id": "group1_3",
                "status": "failed",
                "description": "click",
                "page_url": self.ui_page.url,
                "error_message": (
                    "locator.click: Timeout 15000ms exceeded; locator resolved to "
                    "<li role=menuitem>; element is not visible"
                ),
            }],
        )

        failure = extract_failure_context(self.testcase, record)["failure_step"]

        self.assertEqual(failure["element_id"], failed_menu_item.id)
        self.assertEqual(failure["page_url"], self.ui_page.url)
        self.assertEqual(failure["actual_url"], self.ui_page.url)

        with patch("testcases.ai_diagnosis_service.LLMConfig.objects.filter") as configs:
            configs.return_value.first.return_value = None
            diagnosis = diagnose_execution_failure(
                self.testcase, ui_record_id=record.id, user=self.user,
            )

        self.assertEqual(diagnosis["healing_suggestion"]["repair_type"], "navigation_assertion")
        self.assertEqual(diagnosis["failure_step_info"]["element_id"], failed_menu_item.id)

    def test_navigation_repair_preserves_failure_when_routes_differ(self):
        self.testcase.ui_test_case = self.ui_testcase
        self.testcase.save()
        from asgiref.sync import async_to_sync
        from orchestrator_integration.agent_loop_view import _apply_safe_generated_ui_repair

        diagnosis = {
            "root_cause_type": "SCRIPT_DEFECT",
            "healing_suggestion": {"repair_type": "navigation_assertion", "can_self_heal": True},
            "evidence": {
                **self._failure_evidence(self.ui_page_step.step_details.get()),
                "locator_value": "//li[@role='menuitem']",
                "actual_url": "https://example.test/work-order/dashboard",
                "page_url": "https://example.test/work-order/my-tickets",
            },
            "failure_step_info": {
                "operation_type": "click",
                "element_id": self.ui_element.id,
                "current_locator_value": "//li[@role='menuitem']",
                "error_message": "element is not visible",
            },
        }
        result = async_to_sync(_apply_safe_generated_ui_repair)(
            project_id=self.project.id,
            ui_test_case_id=self.ui_testcase.id,
            diagnosis=diagnosis,
            attempt=1,
        )

        self.assertEqual(result["status"], "skipped")
        detail = self.ui_page_step.step_details.get()
        self.assertEqual(detail.ope_key, "click")

    def test_navigation_repair_replaces_only_redundant_hidden_menu_click_with_path_assertion(self):
        self.testcase.ui_test_case = self.ui_testcase
        self.testcase.save()
        from asgiref.sync import async_to_sync
        from orchestrator_integration.agent_loop_view import _apply_safe_generated_ui_repair

        self.ui_page.url = "https://example.test/work-order/my-tickets"
        self.ui_page.save(update_fields=["url"])
        self.ui_element.locator_value = "//li[@role='menuitem' and normalize-space()='我的工单']"
        self.ui_element.save(update_fields=["locator_value"])
        detail = self.ui_page_step.step_details.get()
        diagnosis = {
            "root_cause_type": "SCRIPT_DEFECT",
            "healing_suggestion": {
                "repair_type": "navigation_assertion",
                "can_self_heal": True,
                "element_id": self.ui_element.id,
            },
            "evidence": {
                **self._failure_evidence(detail),
                "actual_url": "https://example.test/work-order/my-tickets?session=rotated",
                "page_url": self.ui_page.url,
            },
            "failure_step_info": {
                "operation_type": "click",
                "element_id": self.ui_element.id,
                "current_locator_value": self.ui_element.locator_value,
                "error_message": "locator.click timeout; element is not visible",
            },
        }

        result = async_to_sync(_apply_safe_generated_ui_repair)(
            project_id=self.project.id,
            ui_test_case_id=self.ui_testcase.id,
            diagnosis=diagnosis,
            attempt=1,
        )

        self.assertEqual(result["status"], "applied")
        detail.refresh_from_db()
        self.assertEqual(detail.ope_key, "assert_url")
        self.assertEqual(detail.step_type, 1)
        self.assertIsNone(detail.element_id)
        self.assertEqual(detail.ope_value["expected"], self.ui_page.url)
        self.assertEqual(detail.ope_value["match"], "path")

    def _failure_evidence(self, detail):
        return {
            "mapping_status": "resolved", "detail_id": detail.id,
            "element_id": detail.element_id, "operation_type": detail.ope_key,
            "locator_value": detail.element.locator_value if detail.element else "",
            "error_message": "locator.click: Timeout; element is not visible",
        }

    def _runtime_failure(self, detail, **overrides):
        self.testcase.ui_test_case = self.ui_testcase
        self.testcase.save()
        result = {
            "step_id": "group1_2", "detail_id": detail.id,
            "element_id": detail.element_id, "ope_key": detail.ope_key,
            "status": "failed", "description": "业务元素",
            "message": "locator.%s: Timeout 30000ms exceeded" % detail.ope_key,
        }
        result.update(overrides)
        return UiExecutionRecord.objects.create(
            test_case=self.ui_testcase, executor=self.user, status=3,
            step_results=[{"step_id": "group1_1", "detail_id": None,
                           "synthetic": True, "ope_key": "goto", "status": "success"}, result],
        )

    def test_filtered_login_navigation_does_not_shift_exact_failure_reference(self):
        detail = self.ui_page_step.step_details.get()
        detail.ope_key = "fill"
        detail.save()
        UiPageStepsDetailed.objects.create(
            page_step=self.ui_page_step, step_sort=0, ope_key="goto",
            ope_value={"url": "https://other-system.example/session"},
        )
        failure = extract_failure_context(self.testcase, self._runtime_failure(detail))["failure_step"]
        self.assertEqual(failure["detail_id"], detail.id)
        self.assertEqual(failure["element_id"], self.ui_element.id)
        self.assertEqual(failure["operation_type"], "fill")

    def test_synthetic_or_invalid_reference_never_falls_back_to_another_element(self):
        detail = self.ui_page_step.step_details.get()
        for reference in (None, 999999):
            record = self._runtime_failure(detail, detail_id=reference)
            failure = extract_failure_context(self.testcase, record)["failure_step"]
            self.assertIsNone(failure["element_id"])
            self.assertEqual(failure["mapping_status"], "unresolved")

    def test_reference_to_other_case_is_not_used(self):
        other_group = UiPageSteps.objects.create(
            project=self.project, page=self.ui_page, module=self.ui_module,
            name="Other case", creator=self.user,
        )
        other = UiPageStepsDetailed.objects.create(
            page_step=other_group, ope_key="click", element=self.ui_element, step_sort=0,
        )
        failure = extract_failure_context(self.testcase, self._runtime_failure(other))["failure_step"]
        self.assertIsNone(failure["element_id"])
        self.assertEqual(failure["mapping_status"], "unresolved")

    def test_locator_repair_cannot_target_later_element(self):
        from asgiref.sync import async_to_sync
        from orchestrator_integration.agent_loop_view import _apply_safe_generated_ui_repair
        detail = self.ui_page_step.step_details.get()
        later = UiElement.objects.create(
            page=self.ui_page, name="Search", locator_type="id", locator_value="search", creator=self.user,
        )
        UiPageStepsDetailed.objects.create(
            page_step=self.ui_page_step, element=later, ope_key="fill", step_sort=2,
        )
        diagnosis = {
            "root_cause_type": "SCRIPT_DEFECT", "evidence": self._failure_evidence(detail),
            "healing_suggestion": {"can_self_heal": True, "repair_type": "locator",
                                   "element_id": later.id, "suggested_locator_type": "id",
                                   "suggested_locator_value": "new-search"},
        }
        result = async_to_sync(_apply_safe_generated_ui_repair)(
            project_id=self.project.id, ui_test_case_id=self.ui_testcase.id,
            diagnosis=diagnosis, attempt=1,
        )
        self.assertEqual(result["status"], "skipped")
        later.refresh_from_db()
        self.assertEqual(later.locator_value, "search")

    def test_locator_repair_clones_only_failed_detail_with_shared_element(self):
        from asgiref.sync import async_to_sync
        from orchestrator_integration.agent_loop_view import _apply_safe_generated_ui_repair
        detail = self.ui_page_step.step_details.get()
        later = UiPageStepsDetailed.objects.create(
            page_step=self.ui_page_step, element=self.ui_element, ope_key="assert_visible", step_sort=2,
        )
        diagnosis = {
            "root_cause_type": "SCRIPT_DEFECT", "evidence": self._failure_evidence(detail),
            "healing_suggestion": {"can_self_heal": True, "repair_type": "locator",
                                   "element_id": self.ui_element.id, "suggested_locator_type": "id",
                                   "suggested_locator_value": "fixed-login"},
        }
        result = async_to_sync(_apply_safe_generated_ui_repair)(
            project_id=self.project.id, ui_test_case_id=self.ui_testcase.id,
            diagnosis=diagnosis, attempt=1,
        )
        self.assertEqual(result["status"], "applied")
        detail.refresh_from_db()
        later.refresh_from_db()
        self.assertNotEqual(detail.element_id, self.ui_element.id)
        self.assertEqual(later.element_id, self.ui_element.id)
        self.ui_element.refresh_from_db()
        self.assertEqual(self.ui_element.locator_value, "//button[@id='old-login-btn']")

    def test_generated_work_order_type_trigger_uses_verified_add_icon_locator(self):
        from asgiref.sync import async_to_sync
        from orchestrator_integration.agent_loop_view import _apply_safe_generated_ui_repair

        self.testcase.ui_test_case = self.ui_testcase
        self.testcase.save()
        self.ui_page.url = "https://test.bot.by56.com/work-order/tickets"
        self.ui_page.save(update_fields=["url"])
        self.ui_element.name = "工单类型筛选字段添加按钮"
        self.ui_element.locator_value = "//label[contains(text(), '工单类型')]/following-sibling::*//i[contains(@class, 'el-icon-plus')]"
        self.ui_element.locator_type_2 = "xpath"
        self.ui_element.locator_value_2 = "//input[@role='combobox']"
        self.ui_element.locator_type_3 = "css"
        self.ui_element.locator_value_3 = ".el-icon-plus"
        self.ui_element.save(update_fields=[
            "name", "locator_value", "locator_type_2", "locator_value_2",
            "locator_type_3", "locator_value_3",
        ])
        detail = self.ui_page_step.step_details.get()
        diagnosis = {
            "root_cause_type": "SCRIPT_DEFECT",
            "healing_suggestion": {
                "can_self_heal": True,
                "repair_type": "locator",
                "element_id": self.ui_element.id,
                "suggested_locator_type": "xpath",
                "suggested_locator_value": "//input[@role='combobox']",
            },
            "evidence": {
                **self._failure_evidence(detail),
                "actual_url": self.ui_page.url,
            },
            "failure_step_info": {
                "step_sort": 1,
                "element_name": self.ui_element.name,
                "operation_type": "click",
                "element_id": self.ui_element.id,
                "current_locator_value": self.ui_element.locator_value,
                "error_message": "locator.click: Timeout 15000ms exceeded",
            },
        }

        result = async_to_sync(_apply_safe_generated_ui_repair)(
            project_id=self.project.id,
            ui_test_case_id=self.ui_testcase.id,
            diagnosis=diagnosis,
            attempt=1,
        )

        self.assertEqual(result["status"], "applied")
        detail.refresh_from_db()
        repaired = detail.element
        self.assertIn("add-icon", repaired.locator_value)
        self.assertNotIn("el-icon-plus", repaired.locator_value)
        self.assertNotIn("combobox", repaired.locator_value)
        self.assertIsNone(repaired.locator_value_2)
        self.assertIsNone(repaired.locator_value_3)

    def test_generated_filter_value_assertion_uses_observed_filter_field_container(self):
        from asgiref.sync import async_to_sync
        from orchestrator_integration.agent_loop_view import _apply_safe_generated_ui_repair

        self.testcase.ui_test_case = self.ui_testcase
        self.testcase.save()
        self.ui_page.url = "https://test.bot.by56.com/work-order/tickets"
        self.ui_page.save(update_fields=["url"])
        self.ui_element.name = "工单类型筛选字段显示快递UPS派送（AI修复1）"
        self.ui_element.locator_type = "xpath"
        self.ui_element.locator_value = (
            "//label[contains(text(), '工单类型')]/following-sibling::*"
            "//*[contains(text(), '快递UPS派送')]"
        )
        self.ui_element.save(update_fields=["name", "locator_type", "locator_value"])
        detail = self.ui_page_step.step_details.get()
        detail.ope_key = "assert_visible"
        detail.step_type = 1
        detail.save(update_fields=["ope_key", "step_type"])
        evidence = self._failure_evidence(detail)
        evidence.update({
            "operation_type": "assert_visible",
            "error_message": "locator.waitFor: Timeout 10000ms exceeded",
            "actual_url": self.ui_page.url,
            "page_url": self.ui_page.url,
        })
        diagnosis = {
            "root_cause_type": "SCRIPT_DEFECT",
            "healing_suggestion": {
                "can_self_heal": True,
                "repair_type": "locator",
                "element_id": self.ui_element.id,
                "suggested_locator_type": "xpath",
                "suggested_locator_value": "//label/parent::*//*[contains(@class,'el-tag')]",
            },
            "evidence": evidence,
        }

        result = async_to_sync(_apply_safe_generated_ui_repair)(
            project_id=self.project.id,
            ui_test_case_id=self.ui_testcase.id,
            diagnosis=diagnosis,
            attempt=1,
        )

        self.assertEqual(result["status"], "applied")
        detail.refresh_from_db()
        self.assertIn("filter-field-label", detail.element.locator_value)
        self.assertIn("快递UPS派送", detail.element.locator_value)

    def test_missing_machine_mapping_prevents_automatic_repair(self):
        from asgiref.sync import async_to_sync
        from orchestrator_integration.agent_loop_view import _apply_safe_generated_ui_repair
        result = async_to_sync(_apply_safe_generated_ui_repair)(
            project_id=self.project.id, ui_test_case_id=self.ui_testcase.id,
            diagnosis={"root_cause_type": "SCRIPT_DEFECT", "healing_suggestion": {
                "can_self_heal": True, "element_id": self.ui_element.id,
                "suggested_locator_type": "id", "suggested_locator_value": "new"}}, attempt=1,
        )
        self.assertEqual(result["status"], "skipped")

    def test_apply_healing_to_element(self):
        """测试一键应用自愈建议更新 UI 元素定位"""
        self.testcase.ui_test_case = self.ui_testcase
        self.testcase.save()

        url = f"/api/projects/{self.project.id}/testcases/{self.testcase.id}/apply-healing/"
        response = self.client.post(
            url,
            {
                "element_id": self.ui_element.id,
                "suggested_locator_type": "xpath",
                "suggested_locator_value": "//button[text()='登录']",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.ui_element.refresh_from_db()
        self.assertEqual(self.ui_element.locator_value, "//button[text()='登录']")
        self.assertEqual(self.ui_element.locator_value_2, "//button[@id='old-login-btn']")
