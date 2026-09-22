from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("testcases", "0030_manual_execution_phase2"),
    ]

    operations = [
        migrations.CreateModel(
            name="TestCaseRunRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("session_id", models.CharField(db_index=True, max_length=255, unique=True, verbose_name="会话 ID")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("running", "执行中"),
                            ("pass", "通过"),
                            ("fail", "失败"),
                            ("error", "错误"),
                            ("stopped", "已停止"),
                        ],
                        default="running",
                        max_length=20,
                        verbose_name="执行状态",
                    ),
                ),
                ("summary", models.TextField(blank=True, default="", verbose_name="结果摘要")),
                ("step_results", models.JSONField(blank=True, default=list, verbose_name="步骤结果")),
                ("execution_log", models.TextField(blank=True, default="", verbose_name="执行日志")),
                ("generate_playwright_script", models.BooleanField(default=False, verbose_name="生成脚本")),
                ("started_at", models.DateTimeField(auto_now_add=True, verbose_name="开始时间")),
                ("completed_at", models.DateTimeField(blank=True, null=True, verbose_name="完成时间")),
                (
                    "executor",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="testcase_run_records",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="执行人",
                    ),
                ),
                (
                    "testcase",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="run_records",
                        to="testcases.testcase",
                        verbose_name="测试用例",
                    ),
                ),
            ],
            options={
                "verbose_name": "用例执行记录",
                "verbose_name_plural": "用例执行记录",
                "ordering": ["-started_at"],
            },
        ),
    ]
