# Generated by Django 2.2.18 on 2021-06-01 16:14

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cohorts', '0003_auto_20210125_0829'),
    ]

    operations = [
        migrations.AlterField(
            model_name='filter',
            name='value',
            field=models.TextField(),
        ),
    ]
