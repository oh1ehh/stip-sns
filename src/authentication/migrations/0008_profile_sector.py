# -*- coding: utf-8 -*-
# Generated by Django 1.10.4 on 2017-08-01 09:36
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('authentication', '0007_profile_region'),
    ]

    operations = [
        migrations.AddField(
            model_name='profile',
            name='sector',
            field=models.CharField(choices=[(b'information', b'Information and communication services'), (b'financial', b'Financial services'), (b'aviation', b'Aviation services'), (b'railway', b'Railway services'), (b'electric', b'Electric power supply services'), (b'gas', b'Gas supply services'), (b'government', b'Government and administrative services'), (b'medical', b'Medical services'), (b'water', b'Water services'), (b'logistics', b'Logistics services'), (b'chemical', b'Chemical industries'), (b'credit', b'Credit card services'), (b'petroleum', b'Petroleum industries'), (b'other', b'Other')], max_length=128, null=True),
        ),
    ]