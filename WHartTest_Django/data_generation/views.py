import logging

from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response

from projects.models import Project
from wharttest_django.api_permissions import IsProjectMemberForResource
from wharttest_django.permissions import HasModelPermission
from wharttest_django.viewsets import BaseModelViewSet

from .models import DataGenerationPlan, DataGenerationRun
from .serializers import (
    DataGenerationPlanSerializer,
    DataGenerationRunRequestSerializer,
    DataGenerationRunSerializer,
)
from .services import execute_plan

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


class DataGenerationRunViewSet(BaseModelViewSet):
    serializer_class = DataGenerationRunSerializer
    http_method_names = ['get', 'head', 'options']

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
        status = self.request.query_params.get('status')
        if status:
            queryset = queryset.filter(status=status.strip())
        return queryset.select_related('plan', 'triggered_by')
