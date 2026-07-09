from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Dashboard', '0006_customer_cart_cartitem'),
    ]

    operations = [
        migrations.AddField(
            model_name='customer',
            name='locked',
            field=models.BooleanField(default=False),
        ),
    ]

