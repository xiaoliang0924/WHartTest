from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("testcases", "0026_manual_assignment_testcase_snapshot"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="testcase",
            options={
                "ordering": ["project", "sort_order", "id"],
                "verbose_name": "用例",
                "verbose_name_plural": "用例",
            },
        ),
    ]
