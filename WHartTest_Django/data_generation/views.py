import logging

from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from projects.models import Project
from testcases.models import TestSuite
from wharttest_django.api_permissions import IsProjectMemberForResource
from wharttest_django.permissions import HasModelPermission
from wharttest_django.viewsets import BaseModelViewSet

from .analysis import analyze_suite_variable_gaps
from .assignee_resolver import AssigneeResolutionError
from .llm_plan_generator import generate_plan_from_description_with_llm
from .models import DataGenerationPlan, DataGenerationRun
from .serializers import (
    DataGenerationAnalyzeSuiteSerializer,
    DataGenerationGeneratePlanSerializer,
    DataGenerationPlanSerializer,
    DataGenerationRunRequestSerializer,
    DataGenerationRunSerializer,
    DataGenerationTemplateRunSerializer,
)
from .services import execute_cleanup_steps, execute_plan
from .templates import get_builtin_templates, get_template_by_key

logger = logging.getLogger(__name__)


class DataGenerationPlanViewSet(BaseModelViewSet):
    serializer_class = DataGenerationPlanSerializer

    def get_permissions(self):
        return [
            permissions.IsAuthenticated(),
            HasModelPermission(),
            IsProjectMemberForResource(),
        ]

    def get_queryset(self):
        project_pk = self.kwargs.get('project_pk')
        queryset = DataGenerationPlan.objects.filter(project_id=project_pk)
        is_active = self.request.query_params.get('is_active')
        if is_active in ('true', '1'):
            queryset = queryset.filter(is_active=True)
        elif is_active in ('false', '0'):
            queryset = queryset.filter(is_active=False)
        is_template = self.request.query_params.get('is_template')
        if is_template in ('true', '1'):
            queryset = queryset.filter(is_template=True)
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(name__icontains=search.strip())
        return queryset.select_related('default_environment', 'created_by')

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['project_id'] = self.kwargs.get('project_pk')
        return context

    def perform_create(self, serializer):
        project = get_object_or_404(Project, pk=self.kwargs.get('project_pk'))
        serializer.save(project=project, created_by=self.request.user)

    @action(detail=True, methods=['post'])
    def run(self, request, **kwargs):
        plan = self.get_object()
        payload = DataGenerationRunRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        run = execute_plan(
            plan,
            trigger_type=DataGenerationRun.TRIGGER_MANUAL,
            input_params=payload.validated_data.get('input_params') or {},
            triggered_by=request.user,
            default_environment_id=plan.default_environment_id,
        )
        serializer = DataGenerationRunSerializer(run)
        if run.status == DataGenerationRun.STATUS_SUCCESS:
            return Response({'status': 'success', 'data': serializer.data})
        return Response(
            {
                'status': 'error',
                'message': run.error_message or '造数失败',
                'data': serializer.data,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    @action(detail=False, methods=['get'])
    def templates(self, request, **kwargs):
        project_pk = self.kwargs.get('project_pk')
        builtin = get_builtin_templates()
        saved = DataGenerationPlan.objects.filter(
            project_id=project_pk,
            is_template=True,
            is_active=True,
        )
        saved_data = DataGenerationPlanSerializer(saved, many=True).data
        return Response({
            'builtin': builtin,
            'saved': saved_data,
        })

    @action(detail=False, methods=['post'])
    def run_template(self, request, **kwargs):
        project_pk = self.kwargs.get('project_pk')
        payload = DataGenerationTemplateRunSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        template_key = payload.validated_data['template_key']
        input_params = payload.validated_data.get('input_params') or {}
        default_environment_id = payload.validated_data.get('default_environment')

        plan = DataGenerationPlan.objects.filter(
            project_id=project_pk,
            template_key=template_key,
            is_template=True,
            is_active=True,
        ).first()

        if plan is None:
            template = get_template_by_key(template_key)
            if template is None:
                return Response(
                    {'status': 'error', 'message': f'模板不存在: {template_key}'},
                    status=status.HTTP_404_NOT_FOUND,
                )
            project = get_object_or_404(Project, pk=project_pk)
            plan = DataGenerationPlan.objects.create(
                project=project,
                name=template['name'],
                description=template.get('description', ''),
                target_type=template.get('target_type', 'both'),
                steps=template.get('steps') or [],
                cleanup_steps=template.get('cleanup_steps') or [],
                default_environment_id=default_environment_id,
                is_template=True,
                template_key=template_key,
                template_icon=template.get('icon', ''),
                template_params_schema=template.get('params_schema') or {},
                created_by=request.user,
            )

        run = execute_plan(
            plan,
            trigger_type=DataGenerationRun.TRIGGER_MANUAL,
            input_params=input_params,
            triggered_by=request.user,
            default_environment_id=default_environment_id or plan.default_environment_id,
        )
        serializer = DataGenerationRunSerializer(run)
        if run.status == DataGenerationRun.STATUS_SUCCESS:
            return Response({'status': 'success', 'data': serializer.data})
        return Response(
            {
                'status': 'error',
                'message': run.error_message or '造数失败',
                'data': serializer.data,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    @action(detail=False, methods=['post'])
    def generate(self, request, **kwargs):
        project_pk = self.kwargs.get('project_pk')
        payload = DataGenerationGeneratePlanSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            generated = generate_plan_from_description_with_llm(
                payload.validated_data['description'],
                project_id=project_pk,
                default_environment_id=payload.validated_data.get('default_environment'),
                suite_id=payload.validated_data.get('suite_id'),
                use_llm=payload.validated_data.get('use_llm', True),
            )
        except AssigneeResolutionError as exc:
            raise ValidationError({'description': [str(exc)]}) from exc
        return Response({'status': 'success', 'data': generated})

    @action(detail=False, methods=['post'])
    def analyze_suite(self, request, **kwargs):
        project_pk = self.kwargs.get('project_pk')
        payload = DataGenerationAnalyzeSuiteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        suite = get_object_or_404(
            TestSuite,
            pk=payload.validated_data['suite_id'],
            project_id=project_pk,
        )
        result = analyze_suite_variable_gaps(
            suite,
            environment_id=payload.validated_data.get('environment_id'),
        )
        return Response({'status': 'success', 'data': result})


class DataGenerationRunViewSet(BaseModelViewSet):
    serializer_class = DataGenerationRunSerializer
    http_method_names = ['get', 'head', 'options', 'post']

    def get_permissions(self):
        return [
            permissions.IsAuthenticated(),
            HasModelPermission(),
            IsProjectMemberForResource(),
        ]

    def get_queryset(self):
        project_pk = self.kwargs.get('project_pk')
        queryset = DataGenerationRun.objects.filter(project_id=project_pk)
        plan_id = self.request.query_params.get('plan_id')
        if plan_id:
            queryset = queryset.filter(plan_id=plan_id)
        test_execution_id = self.request.query_params.get('test_execution_id')
        if test_execution_id:
            queryset = queryset.filter(test_execution_id=test_execution_id)
        status_param = self.request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status=status_param.strip())
        return queryset.select_related('plan', 'triggered_by')

    @action(detail=True, methods=['post'])
    def rerun(self, request, **kwargs):
        run = self.get_object()
        plan = run.plan
        new_run = execute_plan(
            plan,
            trigger_type=DataGenerationRun.TRIGGER_MANUAL,
            input_params=run.input_params if isinstance(run.input_params, dict) else {},
            triggered_by=request.user,
            default_environment_id=plan.default_environment_id,
            parent_run=run,
        )
        serializer = DataGenerationRunSerializer(new_run)
        if new_run.status == DataGenerationRun.STATUS_SUCCESS:
            return Response({'status': 'success', 'data': serializer.data})
        return Response(
            {
                'status': 'error',
                'message': new_run.error_message or '重跑失败',
                'data': serializer.data,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    @action(detail=True, methods=['post'])
    def cleanup(self, request, **kwargs):
        run = self.get_object()
        if run.is_cleaned:
            return Response(
                {'status': 'success', 'message': '该记录已清理', 'data': DataGenerationRunSerializer(run).data},
            )
        updated = execute_cleanup_steps(run, triggered_by=request.user)
        serializer = DataGenerationRunSerializer(updated)
        if updated.cleanup_status == DataGenerationRun.CLEANUP_SUCCESS:
            return Response({'status': 'success', 'data': serializer.data})
        if updated.cleanup_status == DataGenerationRun.CLEANUP_SKIPPED:
            return Response({'status': 'success', 'message': '该计划未配置清理步骤', 'data': serializer.data})
        return Response(
            {
                'status': 'error',
                'message': updated.cleanup_error_message or '清理失败',
                'data': serializer.data,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
