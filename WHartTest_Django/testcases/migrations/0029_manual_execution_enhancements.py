from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("testcases", "0028_merge_v25"),
    ]

    operations = [
        migrations.AddField(
            model_name="manualtestrun",
            name="blocked_count",
            field=models.PositiveIntegerField(default=0, verbose_name="阻塞数"),
        ),
        migrations.AddField(
            model_name="manualtestrun",
            name="skip_count",
            field=models.PositiveIntegerField(default=0, verbose_name="跳过数"),
        ),
        migrations.AddField(
            model_name="manualtestassignment",
            name="step_results",
            field=models.JSONField(blank=True, default=list, verbose_name="步骤执行结果"),
        ),
        migrations.AddField(
            model_name="manualtestassignment",
            name="evidence_files",
            field=models.JSONField(blank=True, default=list, verbose_name="失败证据"),
        ),
    ]
