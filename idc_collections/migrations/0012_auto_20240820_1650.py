# Generated by Django 3.2.23 on 2024-08-20 23:50

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('idc_collections', '0011_auto_20231116_1627'),
    ]

    operations = [
        migrations.AlterField(
            model_name='attribute_tooltips',
            name='tooltip',
            field=models.TextField(),
        ),
        migrations.AlterField(
            model_name='collection',
            name='collections',
            field=models.TextField(null=True),
        ),
    ]
