from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0048_alter_ticket_status"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="run",
            index=models.Index(fields=["-started_at"], name="acct_run_started_desc_idx"),
        ),
        migrations.AddIndex(
            model_name="run",
            index=models.Index(fields=["status", "created_at"], name="acct_run_status_created_idx"),
        ),
        migrations.AddIndex(
            model_name="notification",
            index=models.Index(fields=["recipient", "-created_at"], name="acct_notif_rec_created_idx"),
        ),
    ]
