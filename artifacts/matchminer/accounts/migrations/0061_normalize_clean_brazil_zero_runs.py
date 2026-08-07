from django.db import migrations


def normalize_clean_zero_runs(apps, schema_editor):
    Run = apps.get_model("accounts", "Run")
    Run.objects.filter(
        scraper__slug="brazil_results",
        status="failed",
        row_count=0,
        errors_csv="",
    ).exclude(requests_csv="").update(status="success")


class Migration(migrations.Migration):
    dependencies = [("accounts", "0060_normalize_atp_partial_runs")]
    operations = [migrations.RunPython(normalize_clean_zero_runs, migrations.RunPython.noop)]
