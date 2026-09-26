from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("testcases", "0037_alter_manualtestassignment_status_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="testcaserunrecord",
            name="execution_mode",
            field=models.CharField(
                choices=[("hybrid", "智能双模"), ("script_only", "仅 UI 自动化"), ("ai_only", "纯 AI")],
                default="hybrid",
                max_length=20,
                verbose_name="本次执行模式",
            ),
        ),
        migrations.AddField(
            model_name="testcaserunrecord",
            name="ui_test_case_id_snapshot",
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name="本次 UI 自动化用例 ID"),
        ),
    ]
