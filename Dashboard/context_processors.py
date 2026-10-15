from .permissions import perms_context


def user_permissions(request):
    """Expose per-user permission flags to all templates as ``perms``."""
    if request.user.is_authenticated:
        return {'perms': perms_context(request.user)}
    return {'perms': {'is_admin': False}}
