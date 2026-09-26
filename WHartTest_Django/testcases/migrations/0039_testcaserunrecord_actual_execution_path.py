from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("testcases", "0038_testcaserunrecord_execution_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="testcaserunrecord",
            name="actual_execution_path",
            field=models.CharField(
                blank=True,
                choices=[
                    ("hybrid_script_pending", "智能双模：正在优先执行绑定脚本"),
                    ("bound_ui_script", "绑定的 UI 脚本执行"),
                    ("agent_browser", "AI Agent 浏览器执行"),
                    ("agent_fallback", "绑定脚本失败后由 AI Agent 接管"),
                    ("agent_no_bound_script", "未绑定脚本，由 AI Agent 执行"),
                ],
                default="",
                max_length=32,
                verbose_name="实际执行路径",
            ),
        ),
    ]
