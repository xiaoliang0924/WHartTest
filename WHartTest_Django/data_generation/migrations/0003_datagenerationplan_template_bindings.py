from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('data_generation', '0002_phase2_phase3_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='datagenerationplan',
            name='template_bindings',
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='按项目解析 interface_ref / environment_ref，如 interfaces.create_ticket=445',
                verbose_name='模板资源绑定',
            ),
        ),
    ]
