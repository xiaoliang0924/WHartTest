from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("testcases", "0040_testcaserunrecord_ui_generation"),
    ]

    operations = [
        migrations.AddField(
            model_name="testcase",
            name="ui_step_mapping",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="当前功能用例绑定的 UI 步骤组到功能步骤编号的显式映射",
                verbose_name="UI步骤映射",
            ),
        ),
    ]
