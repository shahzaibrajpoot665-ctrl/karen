from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Dashboard', '0007_add_customer_locked'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='description',
            field=models.TextField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name='product',
            name='unit',
            field=models.CharField(max_length=64, null=True, blank=True),
        ),
    ]
