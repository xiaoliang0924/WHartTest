from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('testcases', '0034_testcase_pre_data_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='testcaserunrecord',
            name='data_usage',
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='记录本次执行是否实际使用了自动造数返回的数据及其证据',
                verbose_name='造数数据使用情况',
            ),
        ),
    ]
