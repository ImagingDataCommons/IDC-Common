# Generated by Django 2.2 on 2020-02-05 01:00

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('idc_collections', '0007_collection_version'),
    ]

    operations = [
        migrations.AddField(
            model_name='attribute',
            name='default_ui_display',
            field=models.BooleanField(default=True),
        ),
    ]
