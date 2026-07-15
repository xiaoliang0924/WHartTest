from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("testcases", "0024_normalize_testcase_text_newlines"),
    ]

    operations = [
        migrations.CreateModel(
            name="ManualTestRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255, verbose_name="执行批次名称")),
                ("description", models.TextField(blank=True, null=True, verbose_name="说明")),
                ("status", models.CharField(choices=[("pending", "待执行"), ("in_progress", "执行中"), ("completed", "已完成")], default="pending", max_length=20, verbose_name="执行状态")),
                ("total_count", models.PositiveIntegerField(default=0, verbose_name="用例总数")),
                ("passed_count", models.PositiveIntegerField(default=0, verbose_name="通过数")),
                ("failed_count", models.PositiveIntegerField(default=0, verbose_name="不通过数")),
                ("pending_count", models.PositiveIntegerField(default=0, verbose_name="待执行数")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("creator", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_manual_test_runs", to=settings.AUTH_USER_MODEL, verbose_name="创建人")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="manual_test_runs", to="projects.project", verbose_name="所属项目")),
            ],
            options={"verbose_name": "人工用例执行批次", "verbose_name_plural": "人工用例执行批次", "ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="ManualTestAssignment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("pending", "待执行"), ("pass", "通过"), ("fail", "不通过")], default="pending", max_length=20, verbose_name="执行结果")),
                ("failure_reason", models.TextField(blank=True, null=True, verbose_name="失败原因")),
                ("comment", models.TextField(blank=True, null=True, verbose_name="执行备注")),
                ("executed_at", models.DateTimeField(blank=True, null=True, verbose_name="执行时间")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="分派时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("assignee", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="manual_test_assignments", to=settings.AUTH_USER_MODEL, verbose_name="测试人员")),
                ("run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="assignments", to="testcases.manualtestrun", verbose_name="执行批次")),
                ("testcase", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="manual_test_assignments", to="testcases.testcase", verbose_name="测试用例")),
            ],
            options={"verbose_name": "人工用例执行记录", "verbose_name_plural": "人工用例执行记录", "ordering": ["status", "created_at", "id"]},
        ),
        migrations.AddConstraint(
            model_name="manualtestassignment",
            constraint=models.UniqueConstraint(fields=("run", "testcase"), name="unique_manual_run_testcase"),
        ),
    ]
