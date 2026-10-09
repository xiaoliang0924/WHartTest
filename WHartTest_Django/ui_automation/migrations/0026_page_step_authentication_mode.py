from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('ui_automation', '0025_environment_login_enabled')]
    operations = [
        migrations.AddField(
            model_name='uipagesteps', name='authentication_mode',
            field=models.CharField(
                choices=[('auto', '自动协调'), ('environment', '环境认证后验证'), ('script', '脚本执行认证')],
                default='auto', max_length=16, verbose_name='认证执行方式',
            ),
        ),
    ]
