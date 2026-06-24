from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0010_scraper_trigger_token"),
    ]

    operations = [
        migrations.AddField(
            model_name="run",
            name="progress_done",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="run",
            name="progress_total",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
