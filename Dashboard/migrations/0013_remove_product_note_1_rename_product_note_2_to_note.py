# Generated for B/I (auto-computed) and Note rename

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('Dashboard', '0012_pricesetting_remove_product_euro_rate_and_more'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='product',
            name='note_1',
        ),
        migrations.RenameField(
            model_name='product',
            old_name='note_2',
            new_name='note',
        ),
    ]
