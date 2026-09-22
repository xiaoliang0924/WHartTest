from django.db import migrations


def normalize_literal_newlines(apps, schema_editor):
    TestCase = apps.get_model("testcases", "TestCase")
    for testcase in TestCase.objects.filter(
        precondition__contains="\\n"
    ).iterator():
        testcase.precondition = testcase.precondition.replace("\\r\\n", "\n").replace(
            "\\n", "\n"
        ).replace("\\r", "\n")
        testcase.save(update_fields=["precondition"])

    for testcase in TestCase.objects.filter(notes__contains="\\n").iterator():
        testcase.notes = testcase.notes.replace("\\r\\n", "\n").replace(
            "\\n", "\n"
        ).replace("\\r", "\n")
        testcase.save(update_fields=["notes"])


class Migration(migrations.Migration):

    dependencies = [
        ("testcases", "0023_add_pending_product_confirmation_review_status"),
    ]

    operations = [
        migrations.RunPython(normalize_literal_newlines, migrations.RunPython.noop),
    ]
