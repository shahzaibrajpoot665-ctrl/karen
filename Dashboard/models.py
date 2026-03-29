from django.db import models
from django.contrib.auth.models import AbstractUser
from django.db.models.signals import post_delete
from django.dispatch import receiver

# Create your models here.
class User(AbstractUser):
    email = models.EmailField(blank=True, null=True)

    def __str__(self):
        return self.username

class PairingSet(models.Model):
    pair_value = models.CharField(max_length=255, unique=True)

class ImageName(models.Model):
    name = models.CharField(max_length=255, unique=True)

class Image(models.Model):
    image = models.ImageField(upload_to='product_images/')

class Tag(models.Model):
    name = models.CharField(max_length=255)

class ProductImageLink(models.Model):
    """
    Persistent link between images and products that survives product deletion.
    Stores product codes to enable automatic re-linking when products are re-created.
    """
    image = models.ForeignKey(Image, on_delete=models.CASCADE, related_name='persistent_links')
    parent_code = models.CharField(max_length=255)
    child_code = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ('image', 'parent_code', 'child_code')
        indexes = [
            models.Index(fields=['parent_code', 'child_code']),
        ]
    
    def __str__(self):
        return f"Image {self.image.id} -> {self.parent_code}-{self.child_code}"

class Product(models.Model):
    parent_code = models.CharField(max_length=255)
    child_code = models.CharField(max_length=255)
    location = models.CharField(max_length=255)
    stock = models.CharField(max_length=255, null=True, blank=True)
    kpo = models.CharField(max_length=255, null=True, blank=True)
    images_names = models.ManyToManyField(ImageName, blank=True)
    images = models.ManyToManyField(Image, blank=True)
    pairing_set = models.ManyToManyField(PairingSet, blank=True)
    qrcode_image = models.ImageField(upload_to='qrcode_images/', blank=True, null=True)
    barcode_image = models.ImageField(upload_to='barcode_images/', blank=True, null=True)
    weight = models.DecimalField(max_digits=6, decimal_places=2, default=0.00)
    thai_baht = models.CharField(max_length=255, null=True, blank=True)
    usd_rate = models.CharField(max_length=255, null=True, blank=True)
    euro_rate = models.CharField(max_length=255, null=True, blank=True)  
    note_1 = models.TextField(null=True, blank=True)
    note_2 = models.TextField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    unit = models.CharField(max_length=64, null=True, blank=True)
    tag = models.ForeignKey(Tag, null=True, blank=True, on_delete=models.SET_NULL)

    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)
    
    def save(self, *args, **kwargs):
        """Override save to automatically link images based on persistent links"""
        is_new = self.pk is None
        super().save(*args, **kwargs)
        
        if is_new:
            # Auto-link images based on persistent links
            persistent_links = ProductImageLink.objects.filter(
                parent_code=self.parent_code,
                child_code=self.child_code
            )
            for link in persistent_links:
                if not self.images.filter(id=link.image.id).exists():
                    self.images.add(link.image)

@receiver(post_delete, sender=Product)
def _delete_product_generated_images(sender, instance, **kwargs):
    for f in (getattr(instance, 'qrcode_image', None), getattr(instance, 'barcode_image', None)):
        try:
            if not f or not getattr(f, 'name', None):
                continue
            storage = f.storage
            name = f.name
            if storage.exists(name):
                storage.delete(name)
        except Exception:
            pass

class Customer(models.Model):
    name = models.CharField(max_length=255)
    locked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.name} (ID: {self.id})"
    
    class Meta:
        ordering = ['name']

class Cart(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='carts')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    address_override = models.TextField(null=True, blank=True)
    shipping_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    deposit_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    notes = models.TextField(null=True, blank=True)
    sales_person = models.CharField(max_length=255, null=True, blank=True)
    doc_ref = models.CharField(max_length=255, null=True, blank=True)
    customer_code = models.CharField(max_length=255, null=True, blank=True)
    gross_weight = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    def __str__(self):
        return f"Cart for {self.customer.name} (ID: {self.id})"
    
    def get_total_items(self):
        return self.items.count()
    
    class Meta:
        ordering = ['-created_at']

class CartItem(models.Model):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=1)
    added_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.quantity}x {self.product.parent_code}-{self.product.child_code}"
    
    class Meta:
        unique_together = ('cart', 'product')
