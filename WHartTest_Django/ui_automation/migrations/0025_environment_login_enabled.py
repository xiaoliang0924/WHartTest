from django.db import migrations, models
from django.db.models import Q


def preserve_existing_login_config(apps, schema_editor):
    Environment = apps.get_model('ui_automation', 'UiEnvironmentConfig')
    Environment.objects.filter(
        Q(login_username__gt='')
        | Q(login_password_encrypted__gt='')
        | Q(login_url__gt='')
    ).update(login_enabled=True)


class Migration(migrations.Migration):

    dependencies = [
        ('ui_automation', '0024_environment_manual_login'),
    ]

    operations = [
        migrations.AddField(
            model_name='uienvironmentconfig',
            name='login_enabled',
            field=models.BooleanField(default=False, verbose_name='启用登录辅助'),
        ),
        migrations.RunPython(preserve_existing_login_config, migrations.RunPython.noop),
    ]
