from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0050_seed_sportradar")]

    operations = [
        migrations.AddField(
            model_name="scraperschedule",
            name="itf_lookback_days",
            field=models.PositiveSmallIntegerField(
                choices=[
                    (5, "5 days"),
                    (10, "10 days"),
                    (15, "15 days"),
                    (20, "20 days"),
                    (25, "25 days"),
                    (30, "30 days"),
                    (35, "35 days"),
                    (40, "40 days"),
                    (45, "45 days"),
                ],
                default=15,
            ),
        ),
    ]
