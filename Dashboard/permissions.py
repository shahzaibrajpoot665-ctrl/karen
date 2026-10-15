"""Per-user access control for staff members.

Admins always have full access. Staff access is controlled by the
``User.permissions`` JSON dict keyed by the permission keys defined here.
Keys absent from a staff user's dict fall back to ``DEFAULT_STAFF_PERMISSIONS``.
"""
from functools import wraps

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render


# Ordered groups used to render the permission matrix on the Users page
# and to validate keys stored on User.permissions.
PERMISSION_GROUPS = [
    ('Tabs', [
        ('dashboard', 'Dashboard'),
        ('products', 'Products'),
        ('cart', 'Cart Management'),
        ('quotations', 'Quotations'),
        ('orders', 'Orders / Purchase Orders'),
        ('notifications', 'Notifications'),
        ('reports', 'Reports'),
    ]),
    ('Products', [
        ('products_edit', 'Create / edit products & grouping'),
        ('products_delete', 'Delete products'),
        ('products_import', 'Import (Excel / Google Sheet)'),
        ('products_images', 'Upload & manage images'),
        ('products_price', 'Price setting'),
        ('products_pairing', 'Pairing set'),
        ('products_export', 'Export products'),
    ]),
    ('Cart & Customers', [
        ('cart_edit', 'Edit cart items'),
        ('customers_manage', 'Create / edit / delete customers'),
    ]),
    ('Quotations', [
        ('quotations_create', 'Create quotations'),
        ('quotations_edit', 'Edit quotations & items'),
        ('quotations_delete', 'Delete quotations'),
        ('quotations_followups', 'Manage follow-ups'),
        ('quotations_openpo', 'Open PO to factory'),
    ]),
    ('Orders', [
        ('orders_edit', 'Edit orders'),
        ('orders_delete', 'Delete orders'),
        ('orders_receive', 'Receive goods'),
    ]),
]

ALL_PERMISSION_KEYS = [key for _, perms in PERMISSION_GROUPS for key, _ in perms]

TAB_PERMISSION_KEYS = ['dashboard', 'products', 'cart', 'quotations',
                       'orders', 'notifications', 'reports']

# Deny by default: a staff member with no explicitly granted permissions
# can access nothing (no tabs, features, or actions).
DEFAULT_STAFF_PERMISSIONS = {key: False for key in ALL_PERMISSION_KEYS}

# Preferred landing tab order when redirecting a denied staff member.
_LANDING_ORDER = ['dashboard', 'products', 'cart', 'quotations',
                  'orders', 'notifications', 'reports']

_TAB_URLS = {
    'dashboard': '/',
    'products': '/products/',
    'cart': '/cart-management/',
    'quotations': '/quotations/',
    'orders': '/orders/',
    'notifications': '/notifications/',
    'reports': '/reports/',
}


def is_admin(user):
    """Admin = explicit admin role or Django superuser (covers legacy accounts)."""
    return bool(user and user.is_authenticated and
                (user.role == 'admin' or user.is_superuser))


def user_can(user, key):
    """True if the user may perform the given permission key."""
    if not user or not user.is_authenticated:
        return False
    if is_admin(user):
        return True
    perms = user.permissions or {}
    return bool(perms.get(key, DEFAULT_STAFF_PERMISSIONS.get(key, False)))


def _wants_json(request):
    return (request.path.startswith('/api/') or
            request.headers.get('X-Requested-With') == 'XMLHttpRequest' or
            request.content_type == 'application/json')


def landing_url(user):
    """First tab URL the user can access; None if they can access nothing."""
    for key in _LANDING_ORDER:
        if user_can(user, key):
            return _TAB_URLS[key]
    return None


def deny(request, message='You do not have permission to access this.'):
    if _wants_json(request):
        return JsonResponse({'success': False, 'error': message}, status=403)
    target = landing_url(request.user)
    if target and target != request.path:
        messages.warning(request, message)
        return redirect(target)
    # Nowhere allowed to land — render a minimal denied page (avoids
    # a login->dashboard redirect loop for fully-restricted staff).
    return render(request, 'access_denied.html', status=403)


def perm_required(key):
    """Decorator: require a permission key (applied after @login_required)."""
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not user_can(request.user, key):
                return deny(request)
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator


def admin_required(view_func):
    """Decorator: admin role only."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not is_admin(request.user):
            return deny(request)
        return view_func(request, *args, **kwargs)
    return _wrapped


def perms_context(user):
    """Dict of every permission key -> bool, for templates (as ``perms``)."""
    ctx = {key: user_can(user, key) for key in ALL_PERMISSION_KEYS}
    ctx['is_admin'] = is_admin(user)
    return ctx
