from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Dashboard', '0008_product_add_description_unit'),
    ]

    operations = [
        migrations.AddField(
            model_name='cart',
            name='address_override',
            field=models.TextField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name='cart',
            name='shipping_amount',
            field=models.DecimalField(max_digits=12, decimal_places=2, default=0),
        ),
        migrations.AddField(
            model_name='cart',
            name='deposit_amount',
            field=models.DecimalField(max_digits=12, decimal_places=2, default=0),
        ),
        migrations.AddField(
            model_name='cart',
            name='notes',
            field=models.TextField(null=True, blank=True),
        ),
    ]
