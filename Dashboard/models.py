from django.db import models
from django.contrib.auth.models import AbstractUser
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone
from decimal import Decimal


def compute_bi(base, increment):
    """Compute B/I = (Base * 7) / (Increment * 7), rounded to 2 decimals.

    Returns None when base or increment is missing/empty, or when increment is 0.
    """
    if base is None or increment is None:
        return None
    if base == '' or increment == '':
        return None
    try:
        base_d = Decimal(str(base))
        inc_d = Decimal(str(increment))
    except Exception:
        return None
    if inc_d == 0:
        return None
    val = (base_d * Decimal('7')) / (inc_d * Decimal('7'))
    return str(val.quantize(Decimal('0.01')))

# Create your models here.
class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = 'admin', 'Admin'
        STAFF = 'staff', 'Staff'

    email = models.EmailField(blank=True, null=True)
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.STAFF)
    permissions = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return self.username

class PairingSet(models.Model):
    pair_value = models.CharField(max_length=255, unique=True)

class ImageName(models.Model):
    name = models.CharField(max_length=255, unique=True)

class Image(models.Model):
    image = models.ImageField(upload_to='product_images/')
    source_url = models.TextField(null=True, blank=True)
    source_url_hash = models.CharField(max_length=64, null=True, blank=True, unique=True)

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
    # child_code doubles as the SKU: QR/barcode images are generated from it
    # and scanning/lookups (single_product?code=) resolve a product through it.
    child_code = models.CharField(max_length=255, db_index=True)
    location = models.CharField(max_length=255)
    stock = models.CharField(max_length=255, null=True, blank=True)
    kpo = models.CharField(max_length=255, null=True, blank=True)
    images_names = models.ManyToManyField(ImageName, blank=True)
    images = models.ManyToManyField(Image, blank=True)
    pairing_set = models.ManyToManyField(PairingSet, blank=True)
    qrcode_image = models.ImageField(upload_to='qrcode_images/', blank=True, null=True)
    barcode_image = models.ImageField(upload_to='barcode_images/', blank=True, null=True)
    weight = models.DecimalField(max_digits=6, decimal_places=2, default=0.00)
    base = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    increment = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    note = models.TextField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    unit = models.CharField(max_length=64, null=True, blank=True)
    tag = models.ForeignKey(Tag, null=True, blank=True, on_delete=models.SET_NULL)

    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    @property
    def bi(self):
        """Auto-computed B/I value: (Base * 7) / (Increment * 7), 2 decimals.
        Returns None when base/increment missing or increment is 0."""
        return compute_bi(self.base, self.increment)

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
    company_name = models.CharField(max_length=255, null=True, blank=True)
    contact_person = models.CharField(max_length=255, null=True, blank=True)
    address = models.TextField(null=True, blank=True)
    phone = models.CharField(max_length=255, null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
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
    special_notes = models.TextField(null=True, blank=True)
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
    note = models.TextField(null=True, blank=True)
    
    def __str__(self):
        return f"{self.quantity}x {self.product.parent_code}-{self.product.child_code}"
    
    class Meta:
        unique_together = ('cart', 'product')


class PriceSetting(models.Model):
    silver_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    usd_rate = models.DecimalField(max_digits=12, decimal_places=2, default=1)
    euro_rate = models.DecimalField(max_digits=12, decimal_places=2, default=1)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_singleton(cls):
        obj, _ = cls.objects.get_or_create(pk=1, defaults={
            'silver_price': 0,
            'usd_rate': 1,
            'euro_rate': 1,
        })
        return obj


class GoogleSheetConfig(models.Model):
    spreadsheet_id = models.CharField(max_length=255, blank=True, default='')
    spreadsheet_url = models.TextField(blank=True, default='')
    sheet_gid = models.CharField(max_length=64, blank=True, default='')
    sheet_name = models.CharField(max_length=255, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_singleton(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


# ==============================================================
# Quotation -> Purchase Order -> Receiving foundation
# ==============================================================

class Quotation(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        SENT = 'sent', 'Sent'
        ACCEPTED = 'accepted', 'Accepted'
        REJECTED = 'rejected', 'Rejected'

    quotation_number = models.CharField(max_length=255, unique=True, null=True, blank=True)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='quotations')
    cart = models.ForeignKey(Cart, on_delete=models.SET_NULL, null=True, blank=True, related_name='quotations')
    quotation_date = models.DateField(null=True, blank=True)
    validity_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    currency = models.CharField(max_length=8, default='THB')
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    terms_conditions = models.TextField(null=True, blank=True)
    remarks = models.TextField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_quotations')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return self.quotation_number or f"Quotation #{self.id}"


class QuotationItem(models.Model):
    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True, related_name='quotation_items')
    source_cart_item = models.ForeignKey(CartItem, on_delete=models.SET_NULL, null=True, blank=True, related_name='quotation_items')
    product_code = models.CharField(max_length=255, blank=True, default='')
    description = models.TextField(null=True, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    line_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    note = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"{self.quantity}x {self.product_code or self.product_id}"


class QuotationDocument(models.Model):
    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name='documents')
    file = models.FileField(upload_to='quotation_documents/')
    name = models.CharField(max_length=255, blank=True, default='')
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='uploaded_quotation_documents')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name or self.file.name


class QuotationFollowUp(models.Model):
    class Channel(models.TextChoices):
        CALL = 'call', 'Call'
        LINE = 'line', 'LINE'
        EMAIL = 'email', 'Email'
        MEETING = 'meeting', 'Meeting'
        OTHER = 'other', 'Other'

    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name='follow_ups')
    follow_up_at = models.DateTimeField(null=True, blank=True)
    channel = models.CharField(max_length=32, choices=Channel.choices, default=Channel.CALL)
    notes = models.TextField(null=True, blank=True)
    is_done = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    proof = models.FileField(upload_to='followup_proofs/', null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_follow_ups')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['follow_up_at']
        indexes = [
            models.Index(fields=['follow_up_at', 'is_done']),
        ]

    def __str__(self):
        return f"Follow-up for {self.quotation} at {self.follow_up_at}"


class PurchaseOrder(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        ORDERED = 'ordered', 'Ordered'
        PARTIALLY_RECEIVED = 'partially_received', 'Partially Received'
        RECEIVED = 'received', 'Received'
        CANCELLED = 'cancelled', 'Cancelled'

    po_number = models.CharField(max_length=255, unique=True, null=True, blank=True)
    quotation = models.ForeignKey(Quotation, on_delete=models.SET_NULL, null=True, blank=True, related_name='purchase_orders')
    order_date = models.DateField(null=True, blank=True)
    expected_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    remarks = models.TextField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_purchase_orders')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status']),
        ]

    @property
    def customer(self):
        return self.quotation.customer if self.quotation_id else None

    def __str__(self):
        return self.po_number or f"PO #{self.id}"


class PurchaseOrderItem(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True, related_name='purchase_order_items')
    quotation_item = models.ForeignKey(QuotationItem, on_delete=models.SET_NULL, null=True, blank=True, related_name='order_items')
    product_code = models.CharField(max_length=255, blank=True, default='')
    description = models.TextField(null=True, blank=True)
    ordered_quantity = models.PositiveIntegerField(default=1)
    received_quantity = models.PositiveIntegerField(default=0)
    note = models.TextField(null=True, blank=True)

    @property
    def remaining_quantity(self):
        return max(self.ordered_quantity - self.received_quantity, 0)

    def __str__(self):
        return f"{self.ordered_quantity}x {self.product_code or self.product_id}"


class ReceivingRecord(models.Model):
    class Condition(models.TextChoices):
        GOOD = 'good', 'Good'
        DAMAGED = 'damaged', 'Damaged'

    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='receiving_records')
    order_item = models.ForeignKey(PurchaseOrderItem, on_delete=models.SET_NULL, null=True, blank=True, related_name='receiving_records')
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True, related_name='receiving_records')
    product_code = models.CharField(max_length=255, blank=True, default='')
    quantity = models.PositiveIntegerField(default=0)
    condition = models.CharField(max_length=32, choices=Condition.choices, default=Condition.GOOD)
    notes = models.TextField(null=True, blank=True)
    photo = models.ImageField(upload_to='receiving_photos/', null=True, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    received_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='receiving_records')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-received_at']

    def __str__(self):
        return f"Received {self.quantity}x {self.product_code or self.product_id} for {self.purchase_order}"
