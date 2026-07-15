from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from projects.models import Project, ProjectMember
from ui_automation.models import (
    UiElement,
    UiModule,
    UiPage,
    UiPageSteps,
    UiPageStepsDetailed,
)
from ui_automation.serializers import UiPageStepsExecuteSerializer


class UiPageStepsExecuteDataTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='secret')
        self.project = Project.objects.create(name='Demo Project')
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


class UiModuleSortingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='testuser',
            password='password',
            email='test@example.com',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.project = Project.objects.create(
            name='Test Project',
            description='Test Description',
            creator=self.user,
        )
        ProjectMember.objects.create(project=self.project, user=self.user, role='admin')

        self.root1 = UiModule.objects.create(
            project=self.project,
            name='Root 1',
            creator=self.user,
            order=1,
        )
        self.child1_1 = UiModule.objects.create(
            project=self.project,
            name='Child 1-1',
            parent=self.root1,
            creator=self.user,
            order=1,
        )
        self.child1_1_1 = UiModule.objects.create(
            project=self.project,
            name='Child 1-1-1',
            parent=self.child1_1,
            creator=self.user,
            order=1,
        )
        self.child1_2 = UiModule.objects.create(
            project=self.project,
            name='Child 1-2',
            parent=self.root1,
            creator=self.user,
            order=2,
        )
        self.root2 = UiModule.objects.create(
            project=self.project,
            name='Root 2',
            creator=self.user,
            order=2,
        )

    def test_get_max_depth(self):
        self.assertEqual(self.root1.get_max_depth(), 3)
        self.assertEqual(self.child1_1.get_max_depth(), 2)
        self.assertEqual(self.child1_1_1.get_max_depth(), 1)

    def test_move_api_sibling_reorder_before(self):
        url = f'/api/ui-automation/modules/{self.child1_2.id}/move/'
        data = {
            'target_id': self.child1_1.id,
            'drop_position': -1,
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
        url = f'/api/ui-automation/modules/{self.child1_2.id}/move/'
        data = {
            'target_id': self.root2.id,
            'drop_position': 0,
        }

        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.child1_2.refresh_from_db()
        self.assertEqual(self.child1_2.parent, self.root2)
        self.assertEqual(self.child1_2.level, 2)

    def test_move_api_circular_reference_protection(self):
        url = f'/api/ui-automation/modules/{self.root1.id}/move/'
        data = {
            'target_id': self.child1_1_1.id,
            'drop_position': 0,
        }

        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('无法移动模块到自身或其子模块下', response.data['error'])

    def test_move_api_depth_limit_protection(self):
        child4 = UiModule.objects.create(
            project=self.project,
            name='Child 4',
            parent=self.child1_1_1,
            creator=self.user,
        )
        UiModule.objects.create(
            project=self.project,
            name='Child 5',
            parent=child4,
            creator=self.user,
        )

        url = f'/api/ui-automation/modules/{self.root1.id}/move/'
        data = {
            'target_id': self.root2.id,
            'drop_position': 0,
        }

        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('超过5级限制', response.data['error'])
