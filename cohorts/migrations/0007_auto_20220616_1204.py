# Generated by Django 2.2.27 on 2022-06-16 19:04

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('cohorts', '0006_auto_20220616_1201'),
    ]

    operations = [
        migrations.RenameField(
            model_name='filter',
            old_name='numeric_op',
            new_name='operator',
        ),
    ]
