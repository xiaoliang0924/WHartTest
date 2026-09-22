from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("testcases", "0029_manual_execution_enhancements"),
    ]

    operations = [
        migrations.AddField(
            model_name="manualtestrun",
            name="environment",
            field=models.CharField(blank=True, default="", max_length=100, verbose_name="执行环境"),
        ),
        migrations.AddField(
            model_name="manualtestrun",
            name="version",
            field=models.CharField(blank=True, default="", max_length=100, verbose_name="版本号"),
        ),
        migrations.AddField(
            model_name="manualtestrun",
            name="deadline",
            field=models.DateTimeField(blank=True, null=True, verbose_name="截止日期"),
        ),
        migrations.AddField(
            model_name="manualtestrun",
            name="test_suite",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="manual_test_runs",
                to="testcases.testsuite",
                verbose_name="来源测试套件",
            ),
        ),
        migrations.AddField(
            model_name="manualtestassignment",
            name="defect_title",
            field=models.CharField(blank=True, default="", max_length=255, verbose_name="关联缺陷标题"),
        ),
        migrations.AddField(
            model_name="manualtestassignment",
            name="defect_url",
            field=models.URLField(blank=True, default="", max_length=500, verbose_name="关联缺陷链接"),
        ),
    ]
