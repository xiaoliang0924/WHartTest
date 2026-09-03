"""Tests for per-project template resolution."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from api_environments.models import ApiEnvironment
from api_interfaces.models import ApiInterface
from data_generation.template_resolver import resolve_template_steps
from projects.models import Project


class TemplateResolverTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username='resolver_user2', password='pass')
        self.project = Project.objects.create(name='Resolver Project 2', creator=self.user)
        self.environment = ApiEnvironment.objects.create(
            name='resolver-env-2',
            base_url='http://example.com',
            project=self.project,
            created_by=self.user,
        )
        self.interface = ApiInterface.objects.create(
            name='创建工单',
            project=self.project,
            created_by=self.user,
            type=ApiInterface.TYPE_HTTP,
        )

    def test_resolve_interface_ref_from_bindings(self):
        steps = resolve_template_steps(
            [{
                'type': 'api_call',
                'name': '创建工单',
                'interface_ref': 'create_ticket',
                'environment_ref': 'default',
            }],
            project_id=self.project.id,
            plan_bindings={
                'default_environment_id': self.environment.id,
                'interfaces': {'create_ticket': self.interface.id},
            },
        )
        self.assertEqual(steps[0]['interface_id'], self.interface.id)
        self.assertEqual(steps[0]['environment_id'], self.environment.id)
        self.assertNotIn('interface_ref', steps[0])

    def test_resolve_interface_ref_by_name_hint(self):
        steps = resolve_template_steps(
            [{
                'type': 'api_call',
                'name': '创建工单',
                'interface_ref': 'create_ticket',
                'environment_ref': 'default',
            }],
            project_id=self.project.id,
            plan_bindings={'default_environment_id': self.environment.id},
        )
        self.assertEqual(steps[0]['interface_id'], self.interface.id)
