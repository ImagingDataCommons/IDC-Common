# Generated by Django 2.2.10 on 2020-08-28 22:40

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('idc_collections', '0004_auto_20200828_1536'),
    ]

    operations = [
        migrations.AddField(
            model_name='collection',
            name='name',
            field=models.CharField(max_length=255, null=True),
        ),
    ]
