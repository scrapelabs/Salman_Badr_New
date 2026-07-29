from django.db import migrations


def normalize_atp_partial_runs(apps, schema_editor):
    Run = apps.get_model("accounts", "Run")
    db_alias = schema_editor.connection.alias
    Run.objects.using(db_alias).filter(
        scraper__slug="atptour",
        status="partial",
        row_count__gt=0,
    ).update(status="success")


class Migration(migrations.Migration):
    dependencies = [("accounts", "0059_schedule_atp_rankings")]
    operations = [migrations.RunPython(normalize_atp_partial_runs, migrations.RunPython.noop)]
