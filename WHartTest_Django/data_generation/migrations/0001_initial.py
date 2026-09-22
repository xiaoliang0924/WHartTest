# Generated manually for data_generation app

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('api_environments', '0001_initial'),
        ('projects', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('testcases', '0031_testcaserunrecord'),
    ]

    operations = [
        migrations.CreateModel(
            name='DataGenerationPlan',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255, verbose_name='计划名称')),
                ('description', models.TextField(blank=True, default='', verbose_name='描述')),
                ('target_type', models.CharField(
                    choices=[('api', 'API'), ('ui', 'UI'), ('both', 'API + UI')],
                    default='both',
                    max_length=20,
                    verbose_name='目标类型',
                )),
                ('steps', models.JSONField(
                    default=list,
                    help_text='JSON 步骤列表，支持 api_call / set_env_var / set_public_data',
                    verbose_name='步骤配置',
                )),
                ('is_active', models.BooleanField(default=True, verbose_name='启用')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('created_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='created_data_generation_plans',
                    to=settings.AUTH_USER_MODEL,
                    verbose_name='创建人',
                )),
                ('default_environment', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='data_generation_plans',
                    to='api_environments.apienvironment',
                    verbose_name='默认 API 环境',
                )),
                ('project', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='data_generation_plans',
                    to='projects.project',
                    verbose_name='所属项目',
                )),
            ],
            options={
                'verbose_name': '造数计划',
                'verbose_name_plural': '造数计划',
                'ordering': ['-updated_at'],
                'unique_together': {('project', 'name')},
            },
        ),
        migrations.CreateModel(
            name='DataGenerationRun',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(
                    choices=[
                        ('pending', '等待中'),
                        ('running', '执行中'),
                        ('success', '成功'),
                        ('failed', '失败'),
                    ],
                    default='pending',
                    max_length=20,
                    verbose_name='状态',
                )),
                ('trigger_type', models.CharField(
                    choices=[('manual', '手动执行'), ('suite_pre', '套件前置')],
                    default='manual',
                    max_length=20,
                    verbose_name='触发方式',
                )),
                ('input_params', models.JSONField(blank=True, default=dict, verbose_name='输入参数')),
                ('output_snapshot', models.JSONField(blank=True, default=dict, verbose_name='输出快照')),
                ('step_logs', models.JSONField(blank=True, default=list, verbose_name='步骤日志')),
                ('error_message', models.TextField(blank=True, default='', verbose_name='错误信息')),
                ('started_at', models.DateTimeField(blank=True, null=True, verbose_name='开始时间')),
                ('finished_at', models.DateTimeField(blank=True, null=True, verbose_name='结束时间')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('plan', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='runs',
                    to='data_generation.datagenerationplan',
                    verbose_name='造数计划',
                )),
                ('project', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='data_generation_runs',
                    to='projects.project',
                    verbose_name='所属项目',
                )),
                ('test_execution', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='data_generation_runs',
                    to='testcases.testexecution',
                    verbose_name='关联测试执行',
                )),
                ('triggered_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='triggered_data_generation_runs',
                    to=settings.AUTH_USER_MODEL,
                    verbose_name='触发人',
                )),
            ],
            options={
                'verbose_name': '造数执行记录',
                'verbose_name_plural': '造数执行记录',
                'ordering': ['-created_at'],
            },
        ),
    ]
