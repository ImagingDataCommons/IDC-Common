# Generated by Django 2.2.24 on 2021-11-17 02:59

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('idc_collections', '0006_dataversion_current'),
    ]

    operations = [
        migrations.AlterField(
            model_name='collection',
            name='access',
            field=models.CharField(default='Public', max_length=16),
        ),
    ]
