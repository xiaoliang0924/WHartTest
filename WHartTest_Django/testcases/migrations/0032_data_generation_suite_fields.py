# Generated manually for test suite pre-data generation fields

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api_environments', '0001_initial'),
        ('data_generation', '0001_initial'),
        ('testcases', '0031_testcaserunrecord'),
    ]

    operations = [
        migrations.AddField(
            model_name='testsuite',
            name='pre_data_plan',
            field=models.ForeignKey(
                blank=True,
                help_text='套件执行前自动运行的造数计划',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='bound_test_suites',
                to='data_generation.datagenerationplan',
                verbose_name='造数计划',
            ),
        ),
        migrations.AddField(
            model_name='testsuite',
            name='pre_data_params',
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='传递给造数计划的运行时参数',
                verbose_name='造数参数',
            ),
        ),
        migrations.AddField(
            model_name='testsuite',
            name='pre_data_environment',
            field=models.ForeignKey(
                blank=True,
                help_text='造数步骤默认使用的 API 环境',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='bound_test_suites',
                to='api_environments.apienvironment',
                verbose_name='造数 API 环境',
            ),
        ),
        migrations.AddField(
            model_name='testsuite',
            name='pre_data_fail_fast',
            field=models.BooleanField(
                default=True,
                help_text='造数失败时是否阻断套件执行',
                verbose_name='造数失败阻断',
            ),
        ),
        migrations.AddField(
            model_name='testexecution',
            name='data_generation_run',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='linked_test_executions',
                to='data_generation.datagenerationrun',
                verbose_name='造数执行记录',
            ),
        ),
    ]
