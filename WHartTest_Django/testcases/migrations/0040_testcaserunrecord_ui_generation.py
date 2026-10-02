from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("testcases", "0039_testcaserunrecord_actual_execution_path")]
    operations = [migrations.AddField(
        model_name="testcaserunrecord", name="ui_generation",
        field=models.JSONField(blank=True, default=dict, verbose_name="UI 用例生成进度"),
    )]
