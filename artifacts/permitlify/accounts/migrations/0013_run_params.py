from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0012_ticket_notification_ticketattachment_ticketcomment_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="run",
            name="params",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
