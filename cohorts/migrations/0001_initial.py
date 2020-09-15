# Generated by Django 2.2.13 on 2020-09-15 06:50

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='Cohort',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('name', models.CharField(blank=True, max_length=255)),
                ('description', models.TextField(blank=True, null=True)),
                ('active', models.BooleanField(default=True)),
            ],
        ),
        migrations.CreateModel(
            name='Cohort_Comments',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date_created', models.DateTimeField(auto_now_add=True)),
                ('content', models.CharField(max_length=1024)),
            ],
        ),
        migrations.CreateModel(
            name='Cohort_Perms',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('perm', models.CharField(choices=[('READER', 'Reader'), ('OWNER', 'Owner')], default='READER', max_length=10)),
            ],
        ),
        migrations.CreateModel(
            name='Filter',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('value', models.CharField(max_length=256)),
                ('numeric_op', models.CharField(blank=True, choices=[('B', '_btw'), ('GE', '_gte'), ('LE', '_lte'), ('G', '_gt'), ('L', '_lt')], max_length=4, null=True)),
                ('value_delimiter', models.CharField(default=',', max_length=4)),
            ],
        ),
        migrations.CreateModel(
            name='Filter_Group',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('operator', models.CharField(choices=[('A', 'And'), ('O', 'Or')], default='O', max_length=1)),
            ],
        ),
        migrations.CreateModel(
            name='Source',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('type', models.CharField(choices=[('SET_OPS', 'Set Operations'), ('CLONE', 'Clone')], max_length=10)),
                ('notes', models.CharField(blank=True, max_length=1024)),
                ('cohort', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='source_cohort', to='cohorts.Cohort')),
                ('parent', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='source_parent', to='cohorts.Cohort')),
            ],
        ),
    ]
