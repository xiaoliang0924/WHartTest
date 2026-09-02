# Generated manually for suite post-data cleanup

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('testcases', '0032_data_generation_suite_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='testsuite',
            name='post_data_cleanup',
            field=models.BooleanField(
                default=False,
                help_text='套件执行完成后自动运行造数计划的 cleanup_steps',
                verbose_name='造数后自动清理',
            ),
        ),
    ]
