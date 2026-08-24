from django.contrib.auth.models import User
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from projects.models import Project, ProjectMember
from testcases.models import TestCaseModule

class TestCaseModuleSortingTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username='testuser', password='password', email='test@example.com')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.project = Project.objects.create(name='Test Project', description='Test Description', creator=self.user)

        # Add user to project members as admin
        ProjectMember.objects.create(project=self.project, user=self.user, role='admin')

        # Create hierarchy:
        # root1
        #   - child1_1 (order=1)
        #     - child1_1_1
        #   - child1_2 (order=2)
        # root2
        self.root1 = TestCaseModule.objects.create(project=self.project, name='Root 1', creator=self.user, order=1)
        self.child1_1 = TestCaseModule.objects.create(project=self.project, name='Child 1-1', parent=self.root1, creator=self.user, order=1)
        self.child1_1_1 = TestCaseModule.objects.create(project=self.project, name='Child 1-1-1', parent=self.child1_1, creator=self.user, order=1)
        self.child1_2 = TestCaseModule.objects.create(project=self.project, name='Child 1-2', parent=self.root1, creator=self.user, order=2)
        self.root2 = TestCaseModule.objects.create(project=self.project, name='Root 2', creator=self.user, order=2)

    def test_get_max_depth(self):
        """测试模块子树的最大深度"""
        # root1 has child1_1 -> child1_1_1, so max depth should be 3
        self.assertEqual(self.root1.get_max_depth(), 3)
        # child1_1 has child1_1_1, so max depth should be 2
        self.assertEqual(self.child1_1.get_max_depth(), 2)
        # child1_1_1 has no children, depth is 1
        self.assertEqual(self.child1_1_1.get_max_depth(), 1)

    def test_level_propagation_on_parent_change(self):
        """测试改变父级时层级的自动递归更新"""
        # Level of root2 is 1. Level of root1 is 1.
        self.assertEqual(self.root2.level, 1)
        self.assertEqual(self.child1_1.level, 2)
        self.assertEqual(self.child1_1_1.level, 3)

        # Move root1 under root2 as child
        self.root1.parent = self.root2
        self.root1.save()

        # Levels must propagate:
        # root2 is still 1
        self.assertEqual(self.root2.level, 1)
        # root1 is now 2
        self.root1.refresh_from_db()
        self.assertEqual(self.root1.level, 2)
        # child1_1 is now 3
        self.child1_1.refresh_from_db()
        self.assertEqual(self.child1_1.level, 3)
        # child1_1_1 is now 4
        self.child1_1_1.refresh_from_db()
        self.assertEqual(self.child1_1_1.level, 4)

    def test_move_api_sibling_reorder_before(self):
        """测试通过 API 移动模块到同级模块之前"""
        url = f'/api/projects/{self.project.id}/testcase-modules/{self.child1_2.id}/move/'
        data = {
            'target_id': self.child1_1.id,
            'drop_position': -1 # before child1_1
        }

        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Sibling orders should change:
        # child1_2 should now have order=1, child1_1 should have order=2
        self.child1_1.refresh_from_db()
        self.child1_2.refresh_from_db()

        self.assertEqual(self.child1_2.order, 1)
        self.assertEqual(self.child1_1.order, 2)
        self.assertEqual(self.child1_2.sort_order, 1)
        self.assertEqual(self.child1_1.sort_order, 2)
        self.assertEqual(self.child1_2.parent, self.root1)
        self.assertEqual(self.child1_1.parent, self.root1)

    def test_move_api_sibling_reorder_after(self):
        """测试通过 API 移动模块到同级模块之后"""
        url = f'/api/projects/{self.project.id}/testcase-modules/{self.child1_1.id}/move/'
        data = {
            'target_id': self.child1_2.id,
            'drop_position': 1 # after child1_2
        }

        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # child1_1 should now have order=2, child1_2 should have order=1
        self.child1_1.refresh_from_db()
        self.child1_2.refresh_from_db()

        self.assertEqual(self.child1_2.order, 1)
        self.assertEqual(self.child1_1.order, 2)
        self.assertEqual(self.child1_2.sort_order, 1)
        self.assertEqual(self.child1_1.sort_order, 2)

    def test_move_api_direction_updates_both_order_fields(self):
        """The legacy up/down controls remain compatible with v2.5 ordering."""
        url = f'/api/projects/{self.project.id}/testcase-modules/{self.child1_2.id}/move/'

        response = self.client.post(url, {'direction': 'up'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.child1_1.refresh_from_db()
        self.child1_2.refresh_from_db()
        self.assertEqual(self.child1_2.order, 1)
        self.assertEqual(self.child1_2.sort_order, 1)
        self.assertEqual(self.child1_1.order, 2)
        self.assertEqual(self.child1_1.sort_order, 2)

    def test_move_api_into_parent(self):
        """测试通过 API 移动模块到其他父模块下"""
        url = f'/api/projects/{self.project.id}/testcase-modules/{self.child1_2.id}/move/'
        data = {
            'target_id': self.root2.id,
            'drop_position': 0 # inside root2
        }

        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.child1_2.refresh_from_db()
        self.assertEqual(self.child1_2.parent, self.root2)
        self.assertEqual(self.child1_2.level, 2)
        self.assertEqual(self.child1_2.order, self.child1_2.sort_order)

    def test_move_api_circular_reference_protection(self):
        """测试循环引用保护：禁止移动到自己的子模块下"""
        url = f'/api/projects/{self.project.id}/testcase-modules/{self.root1.id}/move/'
        data = {
            'target_id': self.child1_1_1.id,
            'drop_position': 0
        }

        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("无法移动模块到自身或其子模块下", response.data['error'])

    def test_move_api_depth_limit_protection(self):
        """测试6级深度保护"""
        # root1 (1) -> child1_1 (2) -> child1_1_1 (3)
        child4 = TestCaseModule.objects.create(project=self.project, name='Child 4', parent=self.child1_1_1, creator=self.user) # level 4
        child5 = TestCaseModule.objects.create(project=self.project, name='Child 5', parent=child4, creator=self.user) # level 5
        TestCaseModule.objects.create(project=self.project, name='Child 6', parent=child5, creator=self.user) # level 6

        # Move root1 (subtree depth 6) under root2 (level 1) would make deepest child level 7.
        url = f'/api/projects/{self.project.id}/testcase-modules/{self.root1.id}/move/'
        data = {
            'target_id': self.root2.id,
            'drop_position': 0
        }

        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("将超过6级限制", response.data['error'])
