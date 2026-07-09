from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0007_keep_only_bjkc"),
    ]

    operations = [
        migrations.AddField(
            model_name="scraper",
            name="threads",
            field=models.PositiveSmallIntegerField(default=5),
        ),
    ]
