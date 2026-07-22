import json
import os
import uuid
from io import BytesIO

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Case, Count, IntegerField, When
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from projects.models import Project, ProjectMember
from wharttest_django.notification_service import (
    notify_manual_test_assignment,
    notify_manual_test_completion,
)
from wharttest_django.pagination import StandardPagination
from .models import ManualTestAssignment, ManualTestRun, TestCase, TestSuite
from .permissions import IsProjectMemberForManualTestRun
from .serializers import (
    ManualTestAssignmentSerializer,
    ManualTestResultSerializer,
    ManualTestRunCreateSerializer,
    ManualTestRunListSerializer,
    ManualTestRunSerializer,
    TestCaseSerializer,
)


def _testcase_snapshot(testcase, request):
    """Return a plain JSON value so execution history survives testcase deletion."""
    serialized = TestCaseSerializer(testcase, context={"request": request}).data
    return json.loads(json.dumps(serialized))


def _assignment_testcase_name(assignment):
    if assignment.testcase_id:
        return assignment.testcase.name
    snapshot = assignment.testcase_snapshot or {}
    return snapshot.get("name") or f"#{snapshot.get('id', assignment.id)}"


def _assignment_module_name(assignment):
    if assignment.testcase_id and assignment.testcase.module:
        return assignment.testcase.module.name
    snapshot = assignment.testcase_snapshot or {}
    return snapshot.get("module_detail") or snapshot.get("module_name") or "-"


def _assignment_priority_order(queryset):
    """Order assignments P0 → P1 → P2 → P3, then by assignment time."""
    return queryset.annotate(
        level_order=Case(
            When(testcase__level="P0", then=0),
            When(testcase__level="P1", then=1),
            When(testcase__level="P2", then=2),
            When(testcase__level="P3", then=3),
            default=9,
            output_field=IntegerField(),
        )
    ).order_by("level_order", "created_at", "id")


def _parse_deadline(value):
    if value in (None, ""):
        return None
    if hasattr(value, "utcoffset"):
        return value
    if isinstance(value, str):
        parsed = parse_datetime(value)
        if parsed is None:
            from datetime import datetime
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    parsed = datetime.strptime(value, fmt)
                    break
                except ValueError:
                    continue
        if parsed is None:
            return None
        if timezone.is_naive(parsed):
            return timezone.make_aware(parsed, timezone.get_current_timezone())
        return parsed
    return None


def _manual_status_label(status_value):
    return {
        "pending": "待执行",
        "in_progress": "执行中",
        "completed": "已完成",
        "pass": "通过",
        "fail": "不通过",
        "blocked": "阻塞",
        "skip": "跳过",
    }.get(status_value, status_value)


def _build_evidence_url(request, storage_path):
    media_url = (settings.MEDIA_URL or "/media/").rstrip("/") + "/"
    relative_url = f"{media_url}{storage_path.lstrip('/')}"
    if request is not None:
        return request.build_absolute_uri(relative_url)
    return relative_url


class ManualTestRunViewSet(viewsets.ModelViewSet):
    """Manual testcase assignment batches, isolated from automated executions."""

    serializer_class = ManualTestRunSerializer
    pagination_class = StandardPagination
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

        status_value = self.request.query_params.get("status")
        if status_value in {"pending", "in_progress", "completed"}:
            queryset = queryset.filter(status=status_value)

        assignee_id = self.request.query_params.get("assignee_id")
        if assignee_id and self._is_manager():
            try:
                queryset = queryset.filter(assignments__assignee_id=int(assignee_id)).distinct()
            except (TypeError, ValueError):
                pass

        created_from = self.request.query_params.get("created_from")
        created_to = self.request.query_params.get("created_to")
        if created_from:
            queryset = queryset.filter(created_at__gte=created_from)
        if created_to:
            queryset = queryset.filter(created_at__lte=created_to)

        environment = self.request.query_params.get("environment")
        if environment:
            queryset = queryset.filter(environment=environment)
        version = self.request.query_params.get("version")
        if version:
            queryset = queryset.filter(version__icontains=version)

        return queryset

    def get_serializer_class(self):
        if self.action == "create":
            return ManualTestRunCreateSerializer
        if self.action == "list":
            return ManualTestRunListSerializer
        return ManualTestRunSerializer

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
        testsuite_id = serializer.validated_data.get("testsuite_id")
        test_suite = None
        if testsuite_id:
            test_suite = get_object_or_404(TestSuite, id=testsuite_id, project=project)
            testcase_ids = list(test_suite.testcases.values_list("id", flat=True))
        else:
            testcase_ids = list(dict.fromkeys(serializer.validated_data.get("testcase_ids") or []))
        if not testcase_ids:
            return Response({"error": "所选测试套件没有可执行的测试用例。"}, status=status.HTTP_400_BAD_REQUEST)
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
                test_suite=test_suite,
                name=serializer.validated_data["name"],
                description=serializer.validated_data.get("description") or "",
                environment=serializer.validated_data.get("environment") or "",
                version=serializer.validated_data.get("version") or "",
                deadline=serializer.validated_data.get("deadline"),
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
        notify_manual_test_assignment(run, assignee, request.user)
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
        run = self.get_object()
        allowed_fields = {"name", "description", "environment", "version", "deadline"}
        for field in allowed_fields:
            if field in request.data:
                value = request.data.get(field)
                if field == "deadline":
                    value = _parse_deadline(value)
                    if request.data.get(field) and value is None:
                        return Response({"error": "截止日期格式无效"}, status=status.HTTP_400_BAD_REQUEST)
                elif field in {"environment", "version", "description", "name"}:
                    value = value or ""
                setattr(run, field, value)
        run.save(update_fields=[*allowed_fields.intersection(request.data.keys()), "updated_at"])
        run.refresh_from_db()
        return Response(ManualTestRunSerializer(run, context={"request": request}).data)

    def destroy(self, request, *args, **kwargs):
        denied = self._require_manager()
        if denied:
            return denied
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["get"], url_path="report")
    def report(self, request, project_pk=None, pk=None):
        run = self.get_object()
        executed = run.passed_count + run.failed_count + run.blocked_count + run.skip_count
        pass_rate = round(run.passed_count / run.total_count * 100, 1) if run.total_count else 0
        first_assignment = run.assignments.select_related("assignee").first()
        report_data = {
            "run_id": run.id,
            "name": run.name,
            "description": run.description,
            "environment": run.environment,
            "version": run.version,
            "deadline": run.deadline,
            "test_suite_name": run.test_suite.name if run.test_suite_id else "",
            "status": run.status,
            "creator": {
                "id": run.creator_id,
                "username": run.creator.username if run.creator else None,
            } if run.creator else None,
            "assignee": {
                "id": first_assignment.assignee_id,
                "username": first_assignment.assignee.username,
            } if first_assignment else None,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "statistics": {
                "total": run.total_count,
                "passed": run.passed_count,
                "failed": run.failed_count,
                "blocked": run.blocked_count,
                "skip": run.skip_count,
                "pending": run.pending_count,
                "executed": executed,
                "pass_rate": pass_rate,
            },
            "results": [],
        }
        assignments = run.assignments.select_related("testcase", "testcase__module", "assignee").order_by("id")
        for assignment in assignments:
            report_data["results"].append({
                "assignment_id": assignment.id,
                "testcase_id": assignment.testcase_id or (assignment.testcase_snapshot or {}).get("id"),
                "testcase_name": _assignment_testcase_name(assignment),
                "module_name": _assignment_module_name(assignment),
                "status": assignment.status,
                "failure_reason": assignment.failure_reason or "",
                "comment": assignment.comment or "",
                "step_results": assignment.step_results or [],
                "evidence_files": assignment.evidence_files or [],
                "defect_title": assignment.defect_title or "",
                "defect_url": assignment.defect_url or "",
                "executed_at": assignment.executed_at,
                "assignee": assignment.assignee.username if assignment.assignee else "",
            })
        return Response(report_data)

    @action(detail=True, methods=["get"], url_path="export-excel")
    def export_excel(self, request, project_pk=None, pk=None):
        run = self.get_object()
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "执行结果"
        headers = ["用例ID", "用例名称", "所属模块", "执行结果", "失败原因", "执行备注", "关联缺陷", "缺陷链接", "步骤结果", "证据数", "执行时间", "测试人员"]
        sheet.append(headers)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center")

        assignments = run.assignments.select_related("testcase", "testcase__module", "assignee").order_by("id")
        for assignment in assignments:
            executed_at = assignment.executed_at.strftime("%Y-%m-%d %H:%M:%S") if assignment.executed_at else ""
            step_summary = "; ".join(
                f"步骤{item.get('step_number')}: {_manual_status_label(item.get('status', 'pending'))}"
                for item in (assignment.step_results or [])
            )
            sheet.append([
                assignment.testcase_id or (assignment.testcase_snapshot or {}).get("id"),
                _assignment_testcase_name(assignment),
                _assignment_module_name(assignment),
                _manual_status_label(assignment.status),
                assignment.failure_reason or "",
                assignment.comment or "",
                assignment.defect_title or "",
                assignment.defect_url or "",
                step_summary,
                len(assignment.evidence_files or []),
                executed_at,
                assignment.assignee.username if assignment.assignee else "",
            ])

        buffer = BytesIO()
        workbook.save(buffer)
        buffer.seek(0)
        filename = f"manual_run_{run.id}_{run.name}.xlsx"
        response = HttpResponse(
            buffer.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    @action(detail=True, methods=["post"], url_path="reassign")
    def reassign(self, request, project_pk=None, pk=None):
        denied = self._require_manager()
        if denied:
            return denied
        run = self.get_object()
        assignee_id = request.data.get("assignee_id")
        try:
            assignee_id = int(assignee_id)
        except (TypeError, ValueError):
            return Response({"error": "Invalid assignee."}, status=status.HTTP_400_BAD_REQUEST)
        assignee = get_object_or_404(User, pk=assignee_id)
        if not ProjectMember.objects.filter(project=run.project, user=assignee).exists():
            return Response({"error": "The assignee must be a member of this project."}, status=status.HTTP_400_BAD_REQUEST)
        run.assignments.update(assignee=assignee)
        notify_manual_test_assignment(run, assignee, request.user)
        run = self.get_queryset().get(pk=run.pk)
        return Response(ManualTestRunSerializer(run, context={"request": request}).data)

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
    pagination_class = StandardPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["testcase__name", "run__name", "assignee__username"]
    ordering_fields = ["created_at", "executed_at", "status"]
    ordering = []

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
        run_id = self.request.query_params.get("run_id")
        level_value = self.request.query_params.get("level")
        module_id = self.request.query_params.get("module_id")
        if status_value in {"pending", "pass", "fail", "blocked", "skip"}:
            queryset = queryset.filter(status=status_value)
        if level_value in {"P0", "P1", "P2", "P3"}:
            queryset = queryset.filter(testcase__level=level_value)
        if module_id:
            try:
                queryset = queryset.filter(testcase__module_id=int(module_id))
            except (TypeError, ValueError):
                pass
        if run_id:
            try:
                queryset = queryset.filter(run_id=int(run_id))
            except (TypeError, ValueError):
                pass
        if self._is_manager() and assignee_id:
            queryset = queryset.filter(assignee_id=assignee_id)
        elif not self._is_manager():
            queryset = queryset.filter(assignee=self.request.user)
        return queryset

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        if not self.request.query_params.get("ordering"):
            queryset = _assignment_priority_order(queryset)
        return queryset

    def _todo_pending_queryset(self):
        queryset = self.get_queryset().filter(status="pending")
        if self._is_manager() and not self.request.query_params.get("assignee_id"):
            queryset = queryset.filter(assignee=self.request.user)
        return _assignment_priority_order(queryset)

    @action(detail=False, methods=["get"], url_path="team-todo-summary")
    def team_todo_summary(self, request, project_pk=None):
        if not self._is_manager():
            return Response({"error": "仅项目管理员可查看团队待办"}, status=status.HTTP_403_FORBIDDEN)
        pending_qs = ManualTestAssignment.objects.filter(
            run__project=self._project(), status="pending"
        ).select_related("assignee")
        assignee_id = request.query_params.get("assignee_id")
        if assignee_id:
            try:
                pending_qs = pending_qs.filter(assignee_id=int(assignee_id))
            except (TypeError, ValueError):
                pass
        members = list(
            pending_qs.values("assignee_id", "assignee__username")
            .annotate(pending_count=Count("id"))
            .order_by("-pending_count", "assignee__username")
        )
        overdue_before = timezone.now() - timezone.timedelta(days=3)
        return Response({
            "pending_count": pending_qs.count(),
            "run_count": pending_qs.values("run_id").distinct().count(),
            "overdue_count": pending_qs.filter(created_at__lt=overdue_before).count(),
            "members": [
                {
                    "assignee_id": item["assignee_id"],
                    "username": item["assignee__username"],
                    "pending_count": item["pending_count"],
                }
                for item in members
            ],
        })

    @action(detail=False, methods=["get"], url_path="todo-summary")
    def todo_summary(self, request, project_pk=None):
        queryset = self.get_queryset()
        scope = request.query_params.get("scope", "mine")
        if scope == "team":
            if not self._is_manager():
                return Response({"error": "仅项目管理员可查看团队待办"}, status=status.HTTP_403_FORBIDDEN)
        elif self._is_manager() and not request.query_params.get("assignee_id"):
            queryset = queryset.filter(assignee=request.user)
        pending_qs = queryset.filter(status="pending")
        run_count = pending_qs.values("run_id").distinct().count()
        run_ids = list(pending_qs.values_list("run_id", flat=True).distinct())
        runs = ManualTestRun.objects.filter(id__in=run_ids).values(
            "id", "name", "status", "total_count", "pending_count", "passed_count", "failed_count", "created_at"
        )
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        overdue_before = timezone.now() - timezone.timedelta(days=3)
        today_completed_count = queryset.filter(
            status__in=["pass", "fail"], executed_at__gte=today_start
        ).count()
        overdue_count = pending_qs.filter(created_at__lt=overdue_before).count()
        level_counts = {
            level: pending_qs.filter(testcase__level=level).count()
            for level in ("P0", "P1", "P2", "P3")
        }
        earliest_pending_at = pending_qs.order_by("created_at").values_list("created_at", flat=True).first()
        return Response({
            "pending_count": pending_qs.count(),
            "run_count": run_count,
            "runs": list(runs.order_by("-created_at")),
            "today_completed_count": today_completed_count,
            "overdue_count": overdue_count,
            "level_counts": level_counts,
            "earliest_pending_at": earliest_pending_at,
        })

    @action(detail=False, methods=["get"], url_path="next-pending")
    def next_pending(self, request, project_pk=None):
        assignment = self._todo_pending_queryset().first()
        if assignment is None:
            return Response(None)
        return Response(ManualTestAssignmentSerializer(assignment, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="upload-evidence")
    def upload_evidence(self, request, project_pk=None, pk=None):
        assignment = self.get_object()
        if not self._is_manager() and assignment.assignee_id != request.user.id:
            return Response({"error": "You can only upload evidence for cases assigned to you."}, status=status.HTTP_403_FORBIDDEN)
        files = request.FILES.getlist("files")
        if not files:
            single = request.FILES.get("file")
            files = [single] if single else []
        if not files:
            return Response({"error": "请选择要上传的文件"}, status=status.HTTP_400_BAD_REQUEST)

        uploaded = list(assignment.evidence_files or [])
        for file_obj in files:
            ext = os.path.splitext(file_obj.name)[1]
            storage_name = f"manual_test_evidence/{project_pk}/{assignment.id}/{uuid.uuid4().hex}{ext}"
            saved_path = default_storage.save(storage_name, file_obj)
            uploaded.append({
                "name": file_obj.name,
                "url": _build_evidence_url(request, saved_path),
                "uploaded_at": timezone.now().isoformat(),
            })
        assignment.evidence_files = uploaded
        assignment.save(update_fields=["evidence_files", "updated_at"])
        return Response(ManualTestAssignmentSerializer(assignment, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="result")
    def result(self, request, project_pk=None, pk=None):
        assignment = self.get_object()
        if not self._is_manager() and assignment.assignee_id != request.user.id:
            return Response({"error": "You can only submit results for cases assigned to you."}, status=status.HTTP_403_FORBIDDEN)
        previous_status = assignment.status
        serializer = ManualTestResultSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assignment.status = serializer.validated_data["status"]
        assignment.failure_reason = serializer.validated_data.get("failure_reason") or ""
        assignment.comment = serializer.validated_data.get("comment") or ""
        if "step_results" in serializer.validated_data:
            assignment.step_results = serializer.validated_data["step_results"] or []
        if "evidence_files" in serializer.validated_data:
            assignment.evidence_files = serializer.validated_data["evidence_files"] or []
        if "defect_title" in serializer.validated_data:
            assignment.defect_title = serializer.validated_data.get("defect_title") or ""
        if "defect_url" in serializer.validated_data:
            assignment.defect_url = serializer.validated_data.get("defect_url") or ""
        assignment.executed_at = timezone.now() if assignment.status != "pending" else None
        assignment.save(update_fields=[
            "status", "failure_reason", "comment", "step_results", "evidence_files",
            "defect_title", "defect_url", "executed_at", "updated_at",
        ])
        assignment.run.refresh_statistics()
        assignment.run.refresh_from_db()
        if assignment.run.status == "completed" and previous_status != assignment.status:
            notify_manual_test_completion(assignment.run)
        return Response(ManualTestAssignmentSerializer(assignment, context={"request": request}).data)
