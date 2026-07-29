from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0056_schedule_new_zealand_tournament")]

    operations = [
        migrations.CreateModel(
            name="NewZealandMember",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("national_id", models.CharField(max_length=255, unique=True)),
                ("dob", models.DateField(blank=True, null=True)),
                (
                    "gender",
                    models.CharField(
                        blank=True,
                        choices=[("M", "Male"), ("F", "Female"), ("O", "Other")],
                        default="",
                        max_length=1,
                    ),
                ),
            ],
            options={"ordering": ["national_id"]},
        ),
    ]
