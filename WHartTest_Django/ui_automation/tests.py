from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase
from django.test import skipUnlessDBFeature
from rest_framework.test import APIClient
from rest_framework import status
from projects.models import Project, ProjectMember
from ui_automation.models import (
    UiCaseStepsDetailed,
    UiElement,
    UiModule,
    UiPage,
    UiPageSteps,
    UiPageStepsDetailed,
    UiTestCase,
    UiEnvironmentConfig,
    UiAuthState,
)
from ui_automation.serializers import UiPageStepsExecuteSerializer
from ui_automation.generated_case_service import persist_generated_ui_testcase
from testcases.models import TestCase as FunctionalTestCase, TestCaseModule, TestCaseStep
from file_management.models import FileAsset, FileManagementSetting, FileReference


class GeneratedUiTestCasePersistenceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username='generated-case-user', password='secret')
        self.project = Project.objects.create(name='Generated UI Project')
        self.functional_module = TestCaseModule.objects.create(
            project=self.project,
            name='Functional tests',
            creator=self.user,
        )
        self.testcase = FunctionalTestCase.objects.create(
            project=self.project,
            module=self.functional_module,
            name='Generated workflow',
            creator=self.user,
            level='P1',
        )
        TestCaseStep.objects.create(
            test_case=self.testcase,
            step_number=1,
            description='使用观察到的步骤完成登录',
            expected_result='登录成功',
            creator=self.user,
        )
        self.payload = {
            'name': 'Observed workflow',
            'url': 'https://example.test/login',
            'steps': [{
                'name': 'Login and open dashboard',
                'description': '使用本次观察到的控件登录并进入首页',
                'functional_step_numbers': [1],
                'url': 'https://example.test/login',
                'actions': [
                    {
                        'type': 'fill',
                        'selector': {
                            'name': 'Password field',
                            'locator_type': 'label',
                            'locator_value': '密码',
                        },
                        'value': 'do-not-persist-this-password',
                    },
                    {
                        'type': 'click',
                        'selector': {
                            'name': 'Login button',
                            'locator_type': 'role',
                            'locator_value': 'button[name="登录"]',
                        },
                    },
                    {'type': 'assert', 'mode': 'url', 'value': '/dashboard'},
                ],
            }],
        }

    def test_generated_case_is_saved_and_bound_with_password_redacted(self):
        result = persist_generated_ui_testcase(
            testcase_id=self.testcase.id,
            project_id=self.project.id,
            user_id=self.user.id,
            payload=self.payload,
        )

        self.assertEqual(result['status'], 'success')
        self.assertTrue(result['ui_test_case_id'])
        self.testcase.refresh_from_db()
        self.assertEqual(self.testcase.ui_test_case_id, result['ui_test_case_id'])
        self.assertEqual(
            self.testcase.ui_step_mapping['groups'][0]['functional_step_numbers'],
            [1],
        )
        ui_case = UiTestCase.objects.get(pk=result['ui_test_case_id'])
        self.assertEqual(ui_case.case_steps.count(), 1)
        password_detail = UiPageStepsDetailed.objects.get(ope_key='fill')
        self.assertEqual(password_detail.ope_value['value'], '${{password}}')
        self.assertNotIn(
            'do-not-persist-this-password',
            str(list(UiPageStepsDetailed.objects.values_list('ope_value', flat=True))),
        )

    def test_generated_authentication_mode_is_persisted(self):
        self.payload['steps'][0]['authentication_mode'] = 'script'
        result = persist_generated_ui_testcase(
            testcase_id=self.testcase.id, project_id=self.project.id,
            user_id=self.user.id, payload=self.payload,
        )
        group = UiTestCase.objects.get(id=result['ui_test_case_id']).case_steps.get().page_step
        self.assertEqual(group.authentication_mode, 'script')

    def test_assertion_only_script_auth_uses_environment_authentication(self):
        self.payload['steps'][0]['authentication_mode'] = 'script'
        self.payload['steps'][0]['actions'] = [self.payload['steps'][0]['actions'][-1]]
        result = persist_generated_ui_testcase(
            testcase_id=self.testcase.id, project_id=self.project.id,
            user_id=self.user.id, payload=self.payload,
        )
        group = UiTestCase.objects.get(id=result['ui_test_case_id']).case_steps.get().page_step
        self.assertEqual(group.authentication_mode, 'environment')
        self.assertEqual(list(group.step_details.values_list('ope_key', flat=True)), ['assert_url'])

    def test_environment_authentication_rejects_repeated_form_actions(self):
        self.payload['steps'][0]['authentication_mode'] = 'environment'
        with self.assertRaisesRegex(ValueError, '只能保存登录成功的断言'):
            persist_generated_ui_testcase(
                testcase_id=self.testcase.id, project_id=self.project.id,
                user_id=self.user.id, payload=self.payload,
            )
        self.assertFalse(UiTestCase.objects.exists())

    def test_environment_authentication_preserves_success_assertion(self):
        self.payload['steps'][0]['authentication_mode'] = 'environment'
        self.payload['steps'][0]['actions'] = [self.payload['steps'][0]['actions'][-1]]
        result = persist_generated_ui_testcase(
            testcase_id=self.testcase.id, project_id=self.project.id,
            user_id=self.user.id, payload=self.payload,
        )
        group = UiTestCase.objects.get(id=result['ui_test_case_id']).case_steps.get().page_step
        self.assertEqual(group.authentication_mode, 'environment')
        self.assertEqual(list(group.step_details.values_list('ope_key', flat=True)), ['assert_url'])

    def test_invalid_payload_rolls_back_all_ui_rows(self):
        invalid_payload = {
            'steps': [{
                'name': 'Invalid step',
                'actions': [{'type': 'click', 'selector': {'locator_value': ''}}],
            }],
        }
        with self.assertRaises(ValueError):
            persist_generated_ui_testcase(
                testcase_id=self.testcase.id,
                project_id=self.project.id,
                user_id=self.user.id,
                payload=invalid_payload,
            )
        self.assertFalse(UiTestCase.objects.exists())
        self.assertFalse(UiPage.objects.exists())
        self.assertFalse(UiPageSteps.objects.exists())

    def test_generation_rejects_empty_date_fill_with_concrete_date_assertion(self):
        date_selector = {
            'name': '创建日期开始日期输入框',
            'locator_type': 'placeholder',
            'locator_value': '开始日期',
            'placeholder': '开始日期',
        }
        self.payload['steps'][0]['actions'] = [
            {'type': 'fill', 'selector': date_selector, 'value': ''},
            {'type': 'assert', 'mode': 'value', 'selector': date_selector, 'value': '2026-08-24'},
        ]

        with self.assertRaisesRegex(ValueError, '空 fill'):
            persist_generated_ui_testcase(
                testcase_id=self.testcase.id,
                project_id=self.project.id,
                user_id=self.user.id,
                payload=self.payload,
            )

        self.assertFalse(UiTestCase.objects.exists())
        self.assertFalse(UiPage.objects.exists())
        self.assertFalse(UiPageSteps.objects.exists())

    def test_generation_rejects_date_fill_assertion_across_step_groups(self):
        date_selector = {
            'name': '创建日期开始日期输入框',
            'locator_type': 'placeholder',
            'locator_value': '开始日期',
            'placeholder': '开始日期',
        }
        self.payload['steps'] = [
            {
                'name': 'Clear date',
                'functional_step_numbers': [1],
                'url': 'https://example.test/list',
                'actions': [{'type': 'fill', 'selector': date_selector, 'value': ''}],
            },
            {
                'name': 'Assert date',
                'functional_step_numbers': [1],
                'url': 'https://example.test/list',
                'actions': [{'type': 'assert', 'mode': 'value', 'selector': date_selector, 'value': '2026-08-24'}],
            },
        ]

        with self.assertRaisesRegex(ValueError, '随后第 2 组'):
            persist_generated_ui_testcase(
                testcase_id=self.testcase.id,
                project_id=self.project.id,
                user_id=self.user.id,
                payload=self.payload,
            )

        self.assertFalse(UiTestCase.objects.exists())

    def test_generation_allows_observed_nonempty_date_fill_and_matching_assertion(self):
        date_selector = {
            'name': '创建日期开始日期输入框',
            'locator_type': 'placeholder',
            'locator_value': '开始日期',
            'placeholder': '开始日期',
        }
        self.payload['steps'][0]['actions'] = [
            {'type': 'fill', 'selector': date_selector, 'value': '2026-08-24'},
            {'type': 'assert', 'mode': 'value', 'selector': date_selector, 'value': '2026-08-24'},
        ]

        result = persist_generated_ui_testcase(
            testcase_id=self.testcase.id,
            project_id=self.project.id,
            user_id=self.user.id,
            payload=self.payload,
        )

        self.assertEqual(result['status'], 'success')

    def test_generation_rejects_unmapped_functional_steps_before_saving(self):
        TestCaseStep.objects.create(
            test_case=self.testcase,
            step_number=2,
            description='检查列表行数',
            expected_result='行数符合预期',
            creator=self.user,
        )

        with self.assertRaisesRegex(ValueError, '未覆盖功能步骤：2'):
            persist_generated_ui_testcase(
                testcase_id=self.testcase.id,
                project_id=self.project.id,
                user_id=self.user.id,
                payload=self.payload,
            )

        self.assertFalse(UiTestCase.objects.exists())
        self.assertFalse(UiPage.objects.exists())

    def test_repeated_call_does_not_create_duplicate_ui_case(self):
        first = persist_generated_ui_testcase(
            testcase_id=self.testcase.id,
            project_id=self.project.id,
            user_id=self.user.id,
            payload=self.payload,
        )
        second = persist_generated_ui_testcase(
            testcase_id=self.testcase.id,
            project_id=self.project.id,
            user_id=self.user.id,
            payload=self.payload,
        )
        self.assertEqual(first['ui_test_case_id'], second['ui_test_case_id'])
        self.assertFalse(second['created'])
        self.assertEqual(UiTestCase.objects.count(), 1)

    def test_generated_case_preserves_per_step_role_auth_states(self):
        env = UiEnvironmentConfig.objects.create(
            project=self.project,
            name='Role test environment',
            base_url='https://example.test/login',
            creator=self.user,
        )
        ordinary = UiAuthState.objects.create(
            env_config=env,
            name='Ordinary user',
            state_json={'cookies': [{'name': 'ordinary', 'value': 'snapshot'}]},
            creator=self.user,
        )
        admin = UiAuthState.objects.create(
            env_config=env,
            name='Administrator',
            state_json={'cookies': [{'name': 'admin', 'value': 'snapshot'}]},
            creator=self.user,
        )
        first_step = dict(self.payload['steps'][0], auth_state_id=ordinary.id)
        second_step = dict(self.payload['steps'][0], name='Switch to administrator', auth_state_id=admin.id)
        payload = dict(self.payload, steps=[first_step, second_step])

        result = persist_generated_ui_testcase(
            testcase_id=self.testcase.id,
            project_id=self.project.id,
            user_id=self.user.id,
            payload=payload,
        )

        case = UiTestCase.objects.get(pk=result['ui_test_case_id'])
        bound_states = list(
            case.case_steps.select_related('page_step').order_by('case_sort')
            .values_list('page_step__auth_state_id', flat=True)
        )
        self.assertEqual(bound_states, [ordinary.id, admin.id])
        self.assertEqual(result['auth_state_ids'], [ordinary.id, admin.id])

    def test_generated_case_rejects_auth_state_from_another_project(self):
        other_project = Project.objects.create(name='Other project')
        other_env = UiEnvironmentConfig.objects.create(
            project=other_project,
            name='Other environment',
            base_url='https://example.test/login',
            creator=self.user,
        )
        foreign_state = UiAuthState.objects.create(
            env_config=other_env,
            name='Foreign state',
            state_json={'cookies': [{'name': 'foreign', 'value': 'snapshot'}]},
            creator=self.user,
        )
        payload = dict(self.payload, steps=[dict(self.payload['steps'][0], auth_state_id=foreign_state.id)])

        with self.assertRaisesRegex(ValueError, '不属于当前项目'):
            persist_generated_ui_testcase(
                testcase_id=self.testcase.id,
                project_id=self.project.id,
                user_id=self.user.id,
                payload=payload,
            )

        self.assertFalse(UiTestCase.objects.exists())

    def test_multi_account_case_is_not_saved_with_only_one_login_state(self):
        TestCaseStep.objects.create(
            test_case=self.testcase,
            step_number=2,
            description='退出后使用管理员账号(802714/000000)重新登录并验证',
            expected_result='管理员登录成功',
            creator=self.user,
        )
        self.testcase.steps.filter(step_number=1).update(
            description='使用普通用户账号(802434/000000)登录并验证'
        )
        payload = dict(self.payload, steps=[
            dict(self.payload['steps'][0], functional_step_numbers=[1]),
            dict(self.payload['steps'][0], name='Administrator checks', functional_step_numbers=[2]),
        ])

        with self.assertRaisesRegex(ValueError, '涉及 2 个账号.*只绑定了 0 个独立登录态'):
            persist_generated_ui_testcase(
                testcase_id=self.testcase.id,
                project_id=self.project.id,
                user_id=self.user.id,
                payload=payload,
            )

        self.assertFalse(UiTestCase.objects.exists())
        self.assertIsNone(self.testcase.__class__.objects.get(pk=self.testcase.pk).ui_test_case_id)

    @skipUnlessDBFeature('has_select_for_update_of')
    def test_postgres_lock_scope_excludes_nullable_ui_case_join(self):
        query = FunctionalTestCase.objects.select_for_update(of=('self',)).select_related('ui_test_case')
        sql = str(query.query)

        self.assertIn('LEFT OUTER JOIN', sql)
        self.assertIn('FOR UPDATE OF', sql)


class UiPageStepsExecuteDataTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username='tester', password='secret')
        self.project = Project.objects.create(name='Demo Project')
        ProjectMember.objects.create(project=self.project, user=self.user, role='admin')
        self.module = UiModule.objects.create(
            project=self.project,
            name='Module A',
            creator=self.user,
        )
        self.page = UiPage.objects.create(
            project=self.project,
            module=self.module,
            name='Login Page',
            url='/login',
            creator=self.user,
        )
        self.element = UiElement.objects.create(
            page=self.page,
            name='Submit Button',
            locator_type='css',
            locator_value='button[type="submit"]',
            locator_index=2,
            locator_type_2='xpath',
            locator_value_2='//button[@type="submit"]',
            locator_index_2=1,
            locator_type_3='text',
            locator_value_3='Submit',
            is_iframe=True,
            iframe_locator='iframe.login-frame',
            creator=self.user,
        )
        self.page_step = UiPageSteps.objects.create(
            project=self.project,
            page=self.page,
            module=self.module,
            name='Submit Login',
            creator=self.user,
        )
        UiPageStepsDetailed.objects.create(
            page_step=self.page_step,
            element=self.element,
            ope_key='click',
            step_sort=0,
        )

    def test_execute_data_includes_iframe_fields(self):
        response = UiPageStepsExecuteSerializer(self.page_step).data
        self.assertEqual(len(response['step_details']), 1)
        detail = response['step_details'][0]
        self.assertEqual(detail['locator_index'], 2)
        self.assertEqual(detail['locator_type_2'], 'xpath')
        self.assertEqual(detail['locator_value_2'], '//button[@type="submit"]')
        self.assertEqual(detail['locator_index_2'], 1)
        self.assertEqual(detail['locator_type_3'], 'text')
        self.assertEqual(detail['locator_value_3'], 'Submit')
        self.assertTrue(detail['is_iframe'])
        self.assertEqual(detail['iframe_locator'], 'iframe.login-frame')

    def test_delete_referenced_element_is_rejected(self):
        client = APIClient()
        client.force_authenticate(user=self.user)

        response = client.delete(f'/api/ui-automation/elements/{self.element.id}/')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('元素已被 1 个页面步骤引用', response.data['error'])
        self.assertTrue(UiElement.objects.filter(id=self.element.id).exists())
        self.assertEqual(
            UiPageStepsDetailed.objects.get(page_step=self.page_step, step_sort=0).element_id,
            self.element.id,
        )

    def test_execute_data_resolves_upload_file_id_to_file_path(self):
        asset = FileAsset.objects.create(
            project=self.project,
            owner=self.user,
            original_name='avatar.png',
            mime_type='image/png',
            size=5,
            sha256='abc',
        )
        asset.file.save('avatar.png', ContentFile(b'hello'), save=True)
        UiPageStepsDetailed.objects.create(
            page_step=self.page_step,
            element=self.element,
            ope_key='upload',
            ope_value={'file_id': asset.id, 'file_name': 'avatar.png', 'value': f'file_id:{asset.id}'},
            step_sort=1,
        )
        response = UiPageStepsExecuteSerializer(self.page_step).data
        upload_detail = next(item for item in response['step_details'] if item.get('ope_key') == 'upload')
        self.assertEqual(upload_detail['ope_value']['file_id'], asset.id)
        self.assertEqual(upload_detail['ope_value']['file_name'], 'avatar.png')
        self.assertIn('file_management/projects/', upload_detail['ope_value']['file_path'])
        self.assertEqual(upload_detail['ope_value']['value'], upload_detail['ope_value']['file_path'])
        self.assertEqual(
            upload_detail['ope_value']['download_url'],
            f'/api/projects/{self.project.id}/files/{asset.id}/download/',
        )
        self.assertEqual(upload_detail['ope_value']['project_id'], self.project.id)

    def test_delete_upload_step_keeps_file_when_auto_delete_disabled(self):
        asset = FileAsset.objects.create(
            project=self.project,
            owner=self.user,
            original_name='delete-me.txt',
            mime_type='text/plain',
            size=5,
            sha256='def',
        )
        asset.file.save('delete-me.txt', ContentFile(b'hello'), save=True)
        storage = asset.file.storage
        storage_name = asset.file.name
        upload_step = UiPageStepsDetailed.objects.create(
            page_step=self.page_step,
            element=self.element,
            ope_key='upload',
            ope_value={'file_id': asset.id, 'file_name': 'delete-me.txt', 'value': f'file_id:{asset.id}'},
            step_sort=2,
        )

        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.delete(f'/api/ui-automation/page-steps-detailed/{upload_step.id}/')
        self.assertIn(response.status_code, (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT))
        self.assertTrue(FileAsset.objects.filter(id=asset.id).exists())
        self.assertTrue(storage.exists(storage_name))

    def test_delete_upload_step_keeps_file_when_no_reference_exists_even_if_enabled(self):
        from file_management.models import FileManagementSetting
        FileManagementSetting.objects.update_or_create(
            project=self.project,
            defaults={'auto_delete_on_unbind': True},
        )
        asset = FileAsset.objects.create(
            project=self.project,
            owner=self.user,
            original_name='delete-me-enabled.txt',
            mime_type='text/plain',
            size=5,
            sha256='ghi',
        )
        asset.file.save('delete-me-enabled.txt', ContentFile(b'hello'), save=True)
        storage = asset.file.storage
        storage_name = asset.file.name
        upload_step = UiPageStepsDetailed.objects.create(
            page_step=self.page_step,
            element=self.element,
            ope_key='upload',
            ope_value={'file_id': asset.id, 'file_name': 'delete-me-enabled.txt', 'value': f'file_id:{asset.id}'},
            step_sort=3,
        )

        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.delete(f'/api/ui-automation/page-steps-detailed/{upload_step.id}/')
        self.assertIn(response.status_code, (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT))
        self.assertTrue(FileAsset.objects.filter(id=asset.id).exists())
        self.assertTrue(storage.exists(storage_name))

    def test_create_upload_step_creates_file_reference_count(self):
        asset = FileAsset.objects.create(
            project=self.project,
            owner=self.user,
            original_name='referenced.txt',
            mime_type='text/plain',
            size=5,
            sha256='ref',
        )
        asset.file.save('referenced.txt', ContentFile(b'hello'), save=True)
        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.post('/api/ui-automation/page-steps-detailed/', {
            'page_step': self.page_step.id,
            'step_type': 0,
            'element': self.element.id,
            'step_sort': 4,
            'ope_key': 'upload',
            'ope_value': {'file_id': asset.id, 'file_name': 'referenced.txt', 'value': f'file_id:{asset.id}'},
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(FileReference.objects.filter(
            file=asset,
            project=self.project,
            ref_type=FileReference.REF_UI_PAGE_STEPS,
            ref_id=f"detail:{response.data['id']}",
        ).exists())
        asset.refresh_from_db()
        self.assertEqual(asset.references.count(), 1)

    def test_api_created_upload_step_deletes_file_when_auto_delete_enabled(self):
        from file_management.models import FileManagementSetting
        FileManagementSetting.objects.update_or_create(
            project=self.project,
            defaults={'auto_delete_on_unbind': True},
        )
        asset = FileAsset.objects.create(
            project=self.project,
            owner=self.user,
            original_name='delete-api-created.txt',
            mime_type='text/plain',
            size=5,
            sha256='apidel',
        )
        asset.file.save('delete-api-created.txt', ContentFile(b'hello'), save=True)
        storage = asset.file.storage
        storage_name = asset.file.name
        client = APIClient()
        client.force_authenticate(user=self.user)
        create_response = client.post('/api/ui-automation/page-steps-detailed/', {
            'page_step': self.page_step.id,
            'step_type': 0,
            'element': self.element.id,
            'step_sort': 5,
            'ope_key': 'upload',
            'ope_value': {'file_id': asset.id, 'file_name': 'delete-api-created.txt', 'value': f'file_id:{asset.id}'},
        }, format='json')
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(asset.references.count(), 1)

        delete_response = client.delete(f"/api/ui-automation/page-steps-detailed/{create_response.data['id']}/")
        self.assertIn(delete_response.status_code, (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT))
        self.assertFalse(FileAsset.objects.filter(id=asset.id).exists())
        self.assertFalse(storage.exists(storage_name))


class UiModuleSortingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username='testuser', password='password', email='test@example.com')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        
        self.project = Project.objects.create(name='Test Project', description='Test Description', creator=self.user)
        ProjectMember.objects.create(project=self.project, user=self.user, role='admin')
        
        # Create hierarchy:
        # root1
        #   - child1_1 (order=1)
        #     - child1_1_1
        #   - child1_2 (order=2)
        # root2
        self.root1 = UiModule.objects.create(project=self.project, name='Root 1', creator=self.user, order=1)
        self.child1_1 = UiModule.objects.create(project=self.project, name='Child 1-1', parent=self.root1, creator=self.user, order=1)
        self.child1_1_1 = UiModule.objects.create(project=self.project, name='Child 1-1-1', parent=self.child1_1, creator=self.user, order=1)
        self.child1_2 = UiModule.objects.create(project=self.project, name='Child 1-2', parent=self.root1, creator=self.user, order=2)
        self.root2 = UiModule.objects.create(project=self.project, name='Root 2', creator=self.user, order=2)

    def test_get_max_depth(self):
        """测试模块子树的最大深度"""
        self.assertEqual(self.root1.get_max_depth(), 3)
        self.assertEqual(self.child1_1.get_max_depth(), 2)
        self.assertEqual(self.child1_1_1.get_max_depth(), 1)

    def test_move_api_sibling_reorder_before(self):
        """测试通过 API 移动模块到同级模块之前"""
        url = f'/api/ui-automation/modules/{self.child1_2.id}/move/'
        data = {
            'target_id': self.child1_1.id,
            'drop_position': -1 # before child1_1
        }
        
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        self.child1_1.refresh_from_db()
        self.child1_2.refresh_from_db()
        
        self.assertEqual(self.child1_2.order, 1)
        self.assertEqual(self.child1_1.order, 2)
        self.assertEqual(self.child1_2.parent, self.root1)
        self.assertEqual(self.child1_1.parent, self.root1)

    def test_move_api_into_parent(self):
        """测试通过 API 移动模块到其他父模块下"""
        url = f'/api/ui-automation/modules/{self.child1_2.id}/move/'
        data = {
            'target_id': self.root2.id,
            'drop_position': 0 # inside root2
        }
        
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        self.child1_2.refresh_from_db()
        self.assertEqual(self.child1_2.parent, self.root2)
        self.assertEqual(self.child1_2.level, 2)

    def test_move_api_circular_reference_protection(self):
        """测试循环引用保护：禁止移动到自己的子模块下"""
        url = f'/api/ui-automation/modules/{self.root1.id}/move/'
        data = {
            'target_id': self.child1_1_1.id,
            'drop_position': 0
        }
        
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("无法移动模块到自身或其子模块下", response.data['error'])

    def test_move_api_depth_limit_protection(self):
        """测试5级深度保护"""
        child4 = UiModule.objects.create(project=self.project, name='Child 4', parent=self.child1_1_1, creator=self.user) # level 4
        child5 = UiModule.objects.create(project=self.project, name='Child 5', parent=child4, creator=self.user) # level 5
        
        url = f'/api/ui-automation/modules/{self.root1.id}/move/'
        data = {
            'target_id': self.root2.id,
            'drop_position': 0
        }
        
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("超过5级限制", response.data['error'])


class UiStepBatchUpdatePreservesOverrideTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username='batchuser', password='password', email='batch@example.com')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.project = Project.objects.create(name='Batch Project', description='Test Description', creator=self.user)
        ProjectMember.objects.create(project=self.project, user=self.user, role='admin')
        self.module = UiModule.objects.create(project=self.project, name='Root', creator=self.user, order=1)
        self.page = UiPage.objects.create(project=self.project, module=self.module, name='Login', url='https://example.com', creator=self.user)
        self.element = UiElement.objects.create(
            page=self.page,
            name='Username',
            locator_type='css',
            locator_value='#username',
            creator=self.user,
        )
        self.page_step = UiPageSteps.objects.create(
            project=self.project,
            page=self.page,
            module=self.module,
            name='Fill login form',
            creator=self.user,
        )
        self.other_page_step = UiPageSteps.objects.create(
            project=self.project,
            page=self.page,
            module=self.module,
            name='Submit login form',
            creator=self.user,
        )
        self.detail_one = UiPageStepsDetailed.objects.create(
            page_step=self.page_step,
            step_type=0,
            element=self.element,
            step_sort=0,
            ope_key='fill',
            ope_value={'text': 'default-one'},
        )
        self.detail_two = UiPageStepsDetailed.objects.create(
            page_step=self.page_step,
            step_type=0,
            element=self.element,
            step_sort=1,
            ope_key='type',
            ope_value={'text': 'default-two'},
        )
        self.test_case = UiTestCase.objects.create(
            project=self.project,
            module=self.module,
            name='Login case',
            creator=self.user,
        )
        self.case_step = UiCaseStepsDetailed.objects.create(
            test_case=self.test_case,
            page_step=self.page_step,
            case_sort=0,
            case_data={
                str(self.detail_one.id): {'text': 'override-one'},
                str(self.detail_two.id): {'text': 'override-two'},
            },
        )
        self.other_case_step = UiCaseStepsDetailed.objects.create(
            test_case=self.test_case,
            page_step=self.other_page_step,
            case_sort=1,
        )

    def test_page_step_detail_reorder_keeps_detail_ids(self):
        url = '/api/ui-automation/page-steps-detailed/batch_update/'
        response = self.client.post(url, {
            'page_step': self.page_step.id,
            'steps': [
                {
                    'id': self.detail_two.id,
                    'step_type': self.detail_two.step_type,
                    'element': self.element.id,
                    'ope_key': self.detail_two.ope_key,
                    'ope_value': self.detail_two.ope_value,
                },
                {
                    'id': self.detail_one.id,
                    'step_type': self.detail_one.step_type,
                    'element': self.element.id,
                    'ope_key': self.detail_one.ope_key,
                    'ope_value': self.detail_one.ope_value,
                },
            ],
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            list(UiPageStepsDetailed.objects.filter(page_step=self.page_step).order_by('step_sort').values_list('id', flat=True)),
            [self.detail_two.id, self.detail_one.id],
        )
        self.detail_one.refresh_from_db()
        self.detail_two.refresh_from_db()
        self.assertEqual(self.detail_one.step_sort, 1)
        self.assertEqual(self.detail_two.step_sort, 0)

    def test_page_step_detail_reorder_keeps_upload_file_when_auto_delete_enabled(self):
        FileManagementSetting.objects.update_or_create(
            project=self.project,
            defaults={'auto_delete_on_unbind': True},
        )
        asset = FileAsset.objects.create(
            project=self.project,
            owner=self.user,
            original_name='reorder-upload.txt',
            mime_type='text/plain',
            size=5,
            sha256='reorder-upload',
        )
        asset.file.save('reorder-upload.txt', ContentFile(b'hello'), save=True)
        storage = asset.file.storage
        storage_name = asset.file.name
        upload_detail = UiPageStepsDetailed.objects.create(
            page_step=self.page_step,
            step_type=0,
            element=self.element,
            step_sort=2,
            ope_key='upload',
            ope_value={
                'file_id': asset.id,
                'file_name': 'reorder-upload.txt',
                'value': f'file_id:{asset.id}',
            },
        )
        FileReference.objects.create(
            file=asset,
            project=self.project,
            ref_type=FileReference.REF_UI_PAGE_STEPS,
            ref_id=f'detail:{upload_detail.id}',
            created_by=self.user,
        )

        response = self.client.post('/api/ui-automation/page-steps-detailed/batch_update/', {
            'page_step': self.page_step.id,
            'steps': [
                {
                    'id': self.detail_two.id,
                    'step_type': self.detail_two.step_type,
                    'element': self.element.id,
                    'ope_key': self.detail_two.ope_key,
                    'ope_value': self.detail_two.ope_value,
                },
                {
                    'id': upload_detail.id,
                    'step_type': upload_detail.step_type,
                    'element': self.element.id,
                    'ope_key': upload_detail.ope_key,
                    'ope_value': upload_detail.ope_value,
                },
                {
                    'id': self.detail_one.id,
                    'step_type': self.detail_one.step_type,
                    'element': self.element.id,
                    'ope_key': self.detail_one.ope_key,
                    'ope_value': self.detail_one.ope_value,
                },
            ],
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(FileAsset.objects.filter(id=asset.id).exists())
        self.assertTrue(storage.exists(storage_name))
        self.assertTrue(FileReference.objects.filter(
            file=asset,
            project=self.project,
            ref_type=FileReference.REF_UI_PAGE_STEPS,
            ref_id=f'detail:{upload_detail.id}',
        ).exists())
        upload_detail.refresh_from_db()
        self.assertEqual(upload_detail.step_sort, 1)

    def test_case_step_reorder_keeps_case_data_when_not_submitted(self):
        url = '/api/ui-automation/case-steps/batch_update/'
        expected_case_data = self.case_step.case_data

        response = self.client.post(url, {
            'test_case': self.test_case.id,
            'steps': [
                {
                    'id': self.other_case_step.id,
                    'page_step': self.other_case_step.page_step_id,
                },
                {
                    'id': self.case_step.id,
                    'page_step': self.case_step.page_step_id,
                },
            ],
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(UiCaseStepsDetailed.objects.filter(id=self.case_step.id).exists())
        self.case_step.refresh_from_db()
        self.other_case_step.refresh_from_db()
        self.assertEqual(self.case_step.case_sort, 1)
        self.assertEqual(self.other_case_step.case_sort, 0)
        self.assertEqual(self.case_step.case_data, expected_case_data)


class UiDeletionRestrictionTests(TestCase):
    """删除引用限制：删除顺序 测试用例 -> 步骤 -> 页面（元素）"""

    def setUp(self):
        self.user = User.objects.create_superuser(username='restrict_tester', password='secret')
        self.project = Project.objects.create(name='Deletion Project')
        ProjectMember.objects.create(project=self.project, user=self.user, role='admin')
        self.module = UiModule.objects.create(project=self.project, name='Module D', creator=self.user)

        self.page_a = UiPage.objects.create(project=self.project, module=self.module, name='Page A', creator=self.user)
        self.element_a = UiElement.objects.create(
            page=self.page_a, name='Element A', locator_type='css',
            locator_value='#a', creator=self.user,
        )
        self.page_step_a = UiPageSteps.objects.create(
            project=self.project, page=self.page_a, module=self.module, name='Step A', creator=self.user,
        )
        UiPageStepsDetailed.objects.create(
            page_step=self.page_step_a, element=self.element_a, ope_key='click', step_sort=0,
        )

        # 跨页引用：页面 B 的元素被页面 A 下的另一个步骤引用
        self.page_b = UiPage.objects.create(project=self.project, module=self.module, name='Page B', creator=self.user)
        self.element_b = UiElement.objects.create(
            page=self.page_b, name='Element B', locator_type='css',
            locator_value='#b', creator=self.user,
        )
        self.page_step_a2 = UiPageSteps.objects.create(
            project=self.project, page=self.page_a, module=self.module, name='Step A2', creator=self.user,
        )
        UiPageStepsDetailed.objects.create(
            page_step=self.page_step_a2, element=self.element_b, ope_key='click', step_sort=0,
        )

        # 测试用例引用步骤 A
        self.test_case = UiTestCase.objects.create(project=self.project, module=self.module, name='Case 1', creator=self.user)
        self.case_step = UiCaseStepsDetailed.objects.create(
            test_case=self.test_case, page_step=self.page_step_a, case_sort=0,
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_delete_case_referenced_step_is_rejected(self):
        response = self.client.delete(f'/api/ui-automation/page-steps/{self.page_step_a.id}/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('步骤已被 1 个测试用例引用', response.data['error'])
        self.assertTrue(UiPageSteps.objects.filter(id=self.page_step_a.id).exists())

    def test_delete_unreferenced_step_succeeds(self):
        page_c = UiPage.objects.create(project=self.project, module=self.module, name='Page C', creator=self.user)
        page_step_c = UiPageSteps.objects.create(
            project=self.project, page=page_c, module=self.module, name='Step C', creator=self.user,
        )
        response = self.client.delete(f'/api/ui-automation/page-steps/{page_step_c.id}/')
        self.assertIn(response.status_code, (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT))
        self.assertFalse(UiPageSteps.objects.filter(id=page_step_c.id).exists())

    def test_delete_page_with_steps_is_rejected(self):
        response = self.client.delete(f'/api/ui-automation/pages/{self.page_a.id}/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('页面下存在 2 个页面步骤', response.data['error'])
        self.assertTrue(UiPage.objects.filter(id=self.page_a.id).exists())

    def test_delete_page_with_cross_page_referenced_element_is_rejected(self):
        # 页面 B 无步骤集合，但元素被页面 A 的步骤引用，仍不允许删除
        response = self.client.delete(f'/api/ui-automation/pages/{self.page_b.id}/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('页面下的元素已被 1 个页面步骤引用', response.data['error'])
        self.assertTrue(UiPage.objects.filter(id=self.page_b.id).exists())

    def test_full_deletion_order_succeeds(self):
        """按顺序删除：用例 -> 步骤 -> 页面，最终可全部删除"""
        # 1. 删除用例步骤引用，再删除用例
        response = self.client.delete(f'/api/ui-automation/case-steps/{self.case_step.id}/')
        self.assertIn(response.status_code, (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT))
        response = self.client.delete(f'/api/ui-automation/testcases/{self.test_case.id}/')
        self.assertIn(response.status_code, (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT))

        # 2. 删除步骤（级联删除步骤详情）
        response = self.client.delete(f'/api/ui-automation/page-steps/{self.page_step_a.id}/')
        self.assertIn(response.status_code, (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT))
        response = self.client.delete(f'/api/ui-automation/page-steps/{self.page_step_a2.id}/')
        self.assertIn(response.status_code, (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT))

        # 3. 删除页面（级联删除元素）
        response = self.client.delete(f'/api/ui-automation/pages/{self.page_a.id}/')
        self.assertIn(response.status_code, (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT))
        response = self.client.delete(f'/api/ui-automation/pages/{self.page_b.id}/')
        self.assertIn(response.status_code, (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT))
        self.assertFalse(UiPage.objects.filter(id__in=[self.page_a.id, self.page_b.id]).exists())


class UiCaptchaRecognizeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username='captcha_tester', password='secret')
        self.project = Project.objects.create(name='Captcha Project')
        ProjectMember.objects.create(project=self.project, user=self.user, role='admin')
        self.module = UiModule.objects.create(project=self.project, name='Auth Module', creator=self.user)
        self.page = UiPage.objects.create(project=self.project, module=self.module, name='Login', creator=self.user)
        self.img_element = UiElement.objects.create(
            page=self.page,
            name='Captcha Image',
            locator_type='xpath',
            locator_value='//img[@id="captcha_img"]',
            creator=self.user,
        )
        self.input_element = UiElement.objects.create(
            page=self.page,
            name='Captcha Input',
            locator_type='xpath',
            locator_value='//input[@id="captcha_code"]',
            locator_index=0,
            creator=self.user,
        )
        self.page_step = UiPageSteps.objects.create(
            project=self.project,
            page=self.page,
            module=self.module,
            name='Login Step',
            creator=self.user,
        )

    def test_execute_data_resolves_captcha_target_locator(self):
        detail = UiPageStepsDetailed.objects.create(
            page_step=self.page_step,
            element=self.img_element,
            ope_key='captcha_recognize',
            ope_value={
                'target_element_id': self.input_element.id,
                'retry_count': 3,
                'click_to_refresh': True,
            },
            step_sort=0,
        )
        response = UiPageStepsExecuteSerializer(self.page_step).data
        self.assertEqual(len(response['step_details']), 1)
        step_detail = response['step_details'][0]
        self.assertEqual(step_detail['ope_key'], 'captcha_recognize')
        self.assertEqual(step_detail['locator_value'], '//img[@id="captcha_img"]')
        
        target_locator = step_detail['ope_value']['target_locator']
        self.assertEqual(target_locator['element_id'], self.input_element.id)
        self.assertEqual(target_locator['locator_value'], '//input[@id="captcha_code"]')
        self.assertEqual(target_locator['locator_type'], 'xpath')
        self.assertEqual(target_locator['locator_index'], 0)

    def test_captcha_recognize_validation_requires_target_element(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        
        # 缺少 target_element_id
        res = client.post(
            '/api/ui-automation/page-steps-detailed/',
            {
                'page_step': self.page_step.id,
                'element': self.img_element.id,
                'ope_key': 'captcha_recognize',
                'ope_value': {'retry_count': 3},
                'step_sort': 0,
            },
            format='json'
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('必须选择目标输入框元素', str(res.data))

        # 提供 target_element_id 成功
        res_ok = client.post(
            '/api/ui-automation/page-steps-detailed/',
            {
                'page_step': self.page_step.id,
                'element': self.img_element.id,
                'ope_key': 'captcha_recognize',
                'ope_value': {'target_element_id': self.input_element.id, 'retry_count': 3},
                'step_sort': 0,
            },
            format='json'
        )
        self.assertEqual(res_ok.status_code, status.HTTP_201_CREATED)
