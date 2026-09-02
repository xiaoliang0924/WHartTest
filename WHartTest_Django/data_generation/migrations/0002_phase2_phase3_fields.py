# Generated manually for Phase 2/3 data generation enhancements

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('data_generation', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='datagenerationplan',
            name='cleanup_steps',
            field=models.JSONField(blank=True, default=list, help_text='执行完成后可选的清理/回滚步骤', verbose_name='清理步骤'),
        ),
        migrations.AddField(
            model_name='datagenerationplan',
            name='is_template',
            field=models.BooleanField(default=False, verbose_name='是否模板'),
        ),
        migrations.AddField(
            model_name='datagenerationplan',
            name='template_icon',
            field=models.CharField(blank=True, default='', max_length=50, verbose_name='模板图标'),
        ),
        migrations.AddField(
            model_name='datagenerationplan',
            name='template_key',
            field=models.CharField(blank=True, default='', max_length=100, verbose_name='模板标识'),
        ),
        migrations.AddField(
            model_name='datagenerationplan',
            name='template_params_schema',
            field=models.JSONField(blank=True, default=dict, verbose_name='模板参数定义'),
        ),
        migrations.AddField(
            model_name='datagenerationrun',
            name='cleanup_error_message',
            field=models.TextField(blank=True, default='', verbose_name='清理错误'),
        ),
        migrations.AddField(
            model_name='datagenerationrun',
            name='cleanup_logs',
            field=models.JSONField(blank=True, default=list, verbose_name='清理日志'),
        ),
        migrations.AddField(
            model_name='datagenerationrun',
            name='cleanup_status',
            field=models.CharField(blank=True, default='', max_length=20, verbose_name='清理状态'),
        ),
        migrations.AddField(
            model_name='datagenerationrun',
            name='is_cleaned',
            field=models.BooleanField(default=False, verbose_name='已清理'),
        ),
        migrations.AddField(
            model_name='datagenerationrun',
            name='parent_run',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='child_runs',
                to='data_generation.datagenerationrun',
                verbose_name='来源执行记录',
            ),
        ),
        migrations.AlterField(
            model_name='datagenerationplan',
            name='steps',
            field=models.JSONField(
                default=list,
                help_text='JSON 步骤列表，支持 api_call / set_env_var / set_public_data / sql / custom_function / delay',
                verbose_name='步骤配置',
            ),
        ),
        migrations.AlterField(
            model_name='datagenerationrun',
            name='trigger_type',
            field=models.CharField(
                choices=[('manual', '手动执行'), ('suite_pre', '套件前置'), ('cleanup', '清理执行')],
                default='manual',
                max_length=20,
                verbose_name='触发方式',
            ),
        ),
    ]
