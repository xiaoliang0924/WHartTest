from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('api_environments', '0001_initial'),
        ('data_generation', '0001_initial'),
        ('testcases', '0033_test_suite_post_data_cleanup'),
    ]

    operations = [
        migrations.AddField(
            model_name='testcase',
            name='pre_data_plan',
            field=models.ForeignKey(
                blank=True,
                help_text='单条执行前自动运行的造数计划（优先级高于模块默认）',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='bound_testcases',
                to='data_generation.datagenerationplan',
                verbose_name='造数计划',
            ),
        ),
        migrations.AddField(
            model_name='testcase',
            name='pre_data_params',
            field=models.JSONField(blank=True, default=dict, help_text='传递给造数计划的运行时参数', verbose_name='造数参数'),
        ),
        migrations.AddField(
            model_name='testcase',
            name='pre_data_environment',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='bound_testcases',
                to='api_environments.apienvironment',
                verbose_name='造数 API 环境',
            ),
        ),
        migrations.AddField(
            model_name='testcase',
            name='pre_data_fail_fast',
            field=models.BooleanField(default=True, help_text='造数失败时是否阻断用例执行', verbose_name='造数失败阻断'),
        ),
        migrations.AddField(
            model_name='testcase',
            name='skip_pre_data',
            field=models.BooleanField(default=False, help_text='开启后单条执行不再自动准备测试数据', verbose_name='跳过自动造数'),
        ),
        migrations.AddField(
            model_name='testcasemodule',
            name='pre_data_plan',
            field=models.ForeignKey(
                blank=True,
                help_text='该模块及子模块下单条执行时，若无用例级计划则使用此计划',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='bound_testcase_modules',
                to='data_generation.datagenerationplan',
                verbose_name='默认造数计划',
            ),
        ),
        migrations.AddField(
            model_name='testcasemodule',
            name='pre_data_params',
            field=models.JSONField(blank=True, default=dict, verbose_name='造数参数'),
        ),
        migrations.AddField(
            model_name='testcasemodule',
            name='pre_data_environment',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='bound_testcase_modules',
                to='api_environments.apienvironment',
                verbose_name='造数 API 环境',
            ),
        ),
        migrations.AddField(
            model_name='testcasemodule',
            name='pre_data_fail_fast',
            field=models.BooleanField(default=True, verbose_name='造数失败阻断'),
        ),
        migrations.AddField(
            model_name='testcaserunrecord',
            name='data_generation_run',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='testcase_run_records',
                to='data_generation.datagenerationrun',
                verbose_name='造数执行记录',
            ),
        ),
    ]
