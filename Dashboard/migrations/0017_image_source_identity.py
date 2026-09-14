from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Dashboard', '0016_cartitem_note_customer_address_customer_company_name_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='image',
            name='source_url',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='image',
            name='source_url_hash',
            field=models.CharField(blank=True, max_length=64, null=True, unique=True),
        ),
    ]
