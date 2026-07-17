import json

from django.contrib.auth.models import User
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from projects.models import Project, ProjectMember
from .models import ManualTestAssignment, ManualTestRun, TestCase
from .permissions import IsProjectMemberForManualTestRun
from .serializers import (
    ManualTestAssignmentSerializer,
    ManualTestResultSerializer,
    ManualTestRunCreateSerializer,
    ManualTestRunSerializer,
    TestCaseSerializer,
)


def _testcase_snapshot(testcase, request):
    """Return a plain JSON value so execution history survives testcase deletion."""
    serialized = TestCaseSerializer(testcase, context={"request": request}).data
    return json.loads(json.dumps(serialized))


class ManualTestRunViewSet(viewsets.ModelViewSet):
    """Manual testcase assignment batches, isolated from automated executions."""

    serializer_class = ManualTestRunSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "description"]
    ordering_fields = ["created_at", "updated_at", "status"]
    ordering = ["-created_at"]

    def get_permissions(self):
        return [permissions.IsAuthenticated(), IsProjectMemberForManualTestRun()]

    def _project(self):
        return get_object_or_404(Project, pk=self.kwargs.get("project_pk"))

    def _is_manager(self, user=None):
        user = user or self.request.user
        return user.is_superuser or user.is_staff or ProjectMember.objects.filter(
            project=self._project(), user=user, role__in=["owner", "admin"]
        ).exists()

    def _require_manager(self):
        if not self._is_manager():
            return Response(
                {"error": "Only the project owner or an administrator can assign test cases."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    def get_queryset(self):
        queryset = ManualTestRun.objects.filter(project=self._project()).select_related(
            "creator"
        ).prefetch_related("assignments__testcase__module", "assignments__assignee")
        if not self._is_manager():
            queryset = queryset.filter(assignments__assignee=self.request.user).distinct()
        return queryset

    def get_serializer_class(self):
        return ManualTestRunCreateSerializer if self.action == "create" else ManualTestRunSerializer

    def create(self, request, *args, **kwargs):
        denied = self._require_manager()
        if denied:
            return denied
        serializer = ManualTestRunCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = self._project()
        assignee = get_object_or_404(User, pk=serializer.validated_data["assignee_id"])
        if not ProjectMember.objects.filter(project=project, user=assignee).exists():
            return Response({"error": "The assignee must be a member of this project."}, status=status.HTTP_400_BAD_REQUEST)
        testcase_ids = list(dict.fromkeys(serializer.validated_data["testcase_ids"]))
        testcases = list(
            TestCase.objects.filter(project=project, id__in=testcase_ids)
            .select_related("module", "creator")
            .prefetch_related("steps", "screenshots")
        )
        if len(testcases) != len(testcase_ids):
            return Response({"error": "Some test cases do not belong to this project."}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            run = ManualTestRun.objects.create(
                project=project,
                creator=request.user,
                name=serializer.validated_data["name"],
                description=serializer.validated_data.get("description") or "",
            )
            ManualTestAssignment.objects.bulk_create([
                ManualTestAssignment(
                    run=run,
                    testcase=testcase,
                    testcase_snapshot=_testcase_snapshot(testcase, request),
                    assignee=assignee,
                )
                for testcase in testcases
            ])
            run.refresh_statistics()
        return Response(ManualTestRunSerializer(run, context={"request": request}).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        denied = self._require_manager()
        if denied:
            return denied
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        denied = self._require_manager()
        if denied:
            return denied
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        denied = self._require_manager()
        if denied:
            return denied
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"], url_path="assign")
    def assign(self, request, project_pk=None, pk=None):
        denied = self._require_manager()
        if denied:
            return denied
        run = self.get_object()
        serializer = ManualTestRunCreateSerializer(data={
            "name": run.name,
            "testcase_ids": request.data.get("testcase_ids", []),
            "assignee_id": request.data.get("assignee_id"),
        })
        serializer.is_valid(raise_exception=True)
        assignee = get_object_or_404(User, pk=serializer.validated_data["assignee_id"])
        if not ProjectMember.objects.filter(project=run.project, user=assignee).exists():
            return Response({"error": "The assignee must be a member of this project."}, status=status.HTTP_400_BAD_REQUEST)
        testcase_ids = list(dict.fromkeys(serializer.validated_data["testcase_ids"]))
        testcases = list(
            TestCase.objects.filter(project=run.project, id__in=testcase_ids)
            .select_related("module", "creator")
            .prefetch_related("steps", "screenshots")
        )
        if len(testcases) != len(testcase_ids):
            return Response({"error": "Some test cases do not belong to this project."}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            for testcase in testcases:
                ManualTestAssignment.objects.update_or_create(
                    run=run,
                    testcase=testcase,
                    defaults={
                        "assignee": assignee,
                        "testcase_snapshot": _testcase_snapshot(testcase, request),
                        "status": "pending",
                        "failure_reason": "",
                        "comment": "",
                        "executed_at": None,
                    },
                )
            run.refresh_statistics()
        return Response(ManualTestRunSerializer(run, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="remove-assignment")
    def remove_assignment(self, request, project_pk=None, pk=None):
        denied = self._require_manager()
        if denied:
            return denied
        run = self.get_object()
        testcase_id = request.data.get("testcase_id")
        try:
            tid = int(testcase_id)
        except (TypeError, ValueError):
            return Response({"error": "The assigned test case does not exist."}, status=status.HTTP_404_NOT_FOUND)

        assignment = run.assignments.filter(testcase_id=tid).first()
        if assignment is None:
            # 用例被删除后 FK 会置空，仍可通过快照里的 id 定位执行记录
            assignment = next(
                (
                    item
                    for item in run.assignments.filter(testcase__isnull=True)
                    if (item.testcase_snapshot or {}).get("id") == tid
                ),
                None,
            )
        if assignment is None:
            return Response({"error": "The assigned test case does not exist."}, status=status.HTTP_404_NOT_FOUND)
        assignment.delete()
        run.refresh_statistics()
        return Response(ManualTestRunSerializer(run, context={"request": request}).data)


class ManualTestAssignmentViewSet(viewsets.ReadOnlyModelViewSet):
    """Assigned manual test cases and result submission endpoint."""

    serializer_class = ManualTestAssignmentSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["testcase__name", "run__name", "assignee__username"]
    ordering_fields = ["created_at", "executed_at", "status"]
    ordering = ["status", "created_at", "id"]

    def get_permissions(self):
        return [permissions.IsAuthenticated(), IsProjectMemberForManualTestRun()]

    def _project(self):
        return get_object_or_404(Project, pk=self.kwargs.get("project_pk"))

    def _is_manager(self):
        return self.request.user.is_superuser or self.request.user.is_staff or ProjectMember.objects.filter(
            project=self._project(), user=self.request.user, role__in=["owner", "admin"]
        ).exists()

    def get_queryset(self):
        queryset = ManualTestAssignment.objects.filter(run__project=self._project()).select_related(
            "run", "testcase", "testcase__module", "assignee"
        ).prefetch_related("testcase__steps")
        status_value = self.request.query_params.get("status")
        assignee_id = self.request.query_params.get("assignee_id")
        if status_value in {"pending", "pass", "fail"}:
            queryset = queryset.filter(status=status_value)
        if self._is_manager() and assignee_id:
            queryset = queryset.filter(assignee_id=assignee_id)
        elif not self._is_manager():
            queryset = queryset.filter(assignee=self.request.user)
        return queryset

    @action(detail=True, methods=["post"], url_path="result")
    def result(self, request, project_pk=None, pk=None):
        assignment = self.get_object()
        if not self._is_manager() and assignment.assignee_id != request.user.id:
            return Response({"error": "You can only submit results for cases assigned to you."}, status=status.HTTP_403_FORBIDDEN)
        serializer = ManualTestResultSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assignment.status = serializer.validated_data["status"]
        assignment.failure_reason = serializer.validated_data.get("failure_reason") or ""
        assignment.comment = serializer.validated_data.get("comment") or ""
        assignment.executed_at = timezone.now() if assignment.status != "pending" else None
        assignment.save(update_fields=["status", "failure_reason", "comment", "executed_at", "updated_at"])
        assignment.run.refresh_statistics()
        return Response(ManualTestAssignmentSerializer(assignment, context={"request": request}).data)
