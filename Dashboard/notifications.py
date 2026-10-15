"""Notification service layer for the Quotation -> Order -> Receiving workflow.

Computes pending follow-up and order notifications from real database records
and optionally pushes summaries to LINE Messaging API when credentials are
configured (LINE_CHANNEL_ACCESS_TOKEN / LINE_TARGET_ID in settings or env).
"""
import json
import os
import urllib.request
import urllib.error
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import PurchaseOrder, QuotationFollowUp

# Order statuses that still expect delivery — single source, imported by views.py
_OPEN_PO_STATUSES = (PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.ORDERED,
                     PurchaseOrder.Status.PARTIALLY_RECEIVED)

UPCOMING_FOLLOW_UP_DAYS = 7
ORDER_DUE_SOON_DAYS = 3


def _fu_url(fu):
    return f'/quotations/{fu.quotation_id}/' if fu.quotation_id else '/quotations/'


def _order_url(po):
    return f'/orders/{po.id}/'


def _fmt_dt(dt):
    return dt.strftime('%Y-%m-%d %H:%M') if dt else '-'


def _fmt_d(d):
    return d.strftime('%Y-%m-%d') if d else '-'


def follow_up_items(now=None):
    """Pending follow-ups grouped as overdue / due_today / upcoming."""
    now = now or timezone.now()
    today = now.date()
    tomorrow_end = today + timedelta(days=UPCOMING_FOLLOW_UP_DAYS)

    pending = (QuotationFollowUp.objects
               .filter(is_done=False, follow_up_at__isnull=False)
               .select_related('quotation', 'quotation__customer', 'created_by')
               .order_by('follow_up_at'))

    groups = {'overdue': [], 'due_today': [], 'upcoming': []}
    for fu in pending:
        when = fu.follow_up_at
        customer = fu.quotation.customer.name if fu.quotation and fu.quotation.customer else '-'
        number = fu.quotation.quotation_number if fu.quotation else '-'
        item = {
            'id': fu.id,
            'quotation_id': fu.quotation_id,
            'quotation_number': number or '-',
            'customer_name': customer,
            'channel': fu.get_channel_display(),
            'follow_up_at': _fmt_dt(when),
            'notes': (fu.notes or ''),
            'url': _fu_url(fu),
        }
        if when < now:
            groups['overdue'].append(item)
        elif when.date() == today:
            groups['due_today'].append(item)
        elif when.date() <= tomorrow_end:
            groups['upcoming'].append(item)
    return groups


def order_items(today=None):
    """Open purchase orders grouped as overdue / due_soon / open."""
    today = today or timezone.now().date()
    soon_end = today + timedelta(days=ORDER_DUE_SOON_DAYS)

    qs = (PurchaseOrder.objects
          .filter(status__in=_OPEN_PO_STATUSES)
          .select_related('quotation', 'quotation__customer')
          .prefetch_related('items')
          .order_by('expected_date'))

    groups = {'overdue': [], 'due_soon': [], 'open': []}
    for po in qs:
        remaining = sum(i.remaining_quantity for i in po.items.all())
        item = {
            'id': po.id,
            'po_number': po.po_number or f'PO #{po.id}',
            'quotation_number': po.quotation.quotation_number if po.quotation else '-',
            'customer_name': po.customer.name if po.customer else '-',
            'expected_date': _fmt_d(po.expected_date),
            'remaining_qty': remaining,
            'status': po.status,
            'status_display': po.get_status_display(),
            'url': _order_url(po),
        }
        if po.expected_date and po.expected_date < today:
            groups['overdue'].append(item)
        elif po.expected_date and po.expected_date <= soon_end:
            groups['due_soon'].append(item)
        else:
            groups['open'].append(item)
    return groups


def build_notifications(now=None):
    """Unified notification list for the bell / notifications page."""
    now = now or timezone.now()
    fu = follow_up_items(now)
    po = order_items(now.date())
    items = []

    for f in fu['overdue']:
        items.append({
            'kind': 'follow_up_overdue', 'severity': 'danger',
            'title': 'Follow-up overdue',
            'text': f"{f['quotation_number']} - {f['customer_name']} - {f['channel']} at {f['follow_up_at']}",
            'url': f['url'], 'when': f['follow_up_at'],
        })
    for o in po['overdue']:
        items.append({
            'kind': 'order_overdue', 'severity': 'danger',
            'title': 'Order overdue',
            'text': f"{o['po_number']} - {o['customer_name']} - due {o['expected_date']} - {o['remaining_qty']} undelivered",
            'url': o['url'], 'when': o['expected_date'],
        })
    for f in fu['due_today']:
        items.append({
            'kind': 'follow_up_due', 'severity': 'warning',
            'title': 'Follow-up due today',
            'text': f"{f['quotation_number']} - {f['customer_name']} - {f['channel']} at {f['follow_up_at']}",
            'url': f['url'], 'when': f['follow_up_at'],
        })
    for o in po['due_soon']:
        items.append({
            'kind': 'order_due_soon', 'severity': 'warning',
            'title': 'Order due soon',
            'text': f"{o['po_number']} - {o['customer_name']} - due {o['expected_date']} - {o['remaining_qty']} undelivered",
            'url': o['url'], 'when': o['expected_date'],
        })
    for f in fu['upcoming']:
        items.append({
            'kind': 'follow_up_upcoming', 'severity': 'info',
            'title': 'Upcoming follow-up',
            'text': f"{f['quotation_number']} - {f['customer_name']} - {f['channel']} at {f['follow_up_at']}",
            'url': f['url'], 'when': f['follow_up_at'],
        })
    for o in po['open']:
        items.append({
            'kind': 'order_open', 'severity': 'secondary',
            'title': 'Open order',
            'text': f"{o['po_number']} - {o['customer_name']} - due {o['expected_date']} - {o['remaining_qty']} undelivered",
            'url': o['url'], 'when': o['expected_date'],
        })
    return {
        'notifications': items,
        'count': len(items),
        'line_configured': line_configured(),
        'groups': {'follow_ups': fu, 'orders': po},
    }


# ----------------------------------------------------------------------
# LINE Messaging API integration
# ----------------------------------------------------------------------

def line_credentials():
    """LINE Messaging API credentials from settings or environment."""
    token = getattr(settings, 'LINE_CHANNEL_ACCESS_TOKEN', None) or os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
    target = getattr(settings, 'LINE_TARGET_ID', None) or os.environ.get('LINE_TARGET_ID')
    return (token or '').strip(), (target or '').strip()


def line_configured():
    token, target = line_credentials()
    return bool(token and target)


def send_line_message(text):
    """Push a text message to the configured LINE user/group.

    Returns {'sent': bool, 'reason': str}. Cleanly reports missing
    configuration instead of failing or using fake credentials.
    """
    token, target = line_credentials()
    if not (token and target):
        return {
            'sent': False,
            'reason': 'LINE is not configured - set LINE_CHANNEL_ACCESS_TOKEN and LINE_TARGET_ID',
            'configured': False,
        }
    body = json.dumps({
        'to': target,
        'messages': [{'type': 'text', 'text': text}],
    }).encode('utf-8')
    req = urllib.request.Request(
        'https://api.line.me/v2/bot/message/push',
        data=body,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {token}',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return {'sent': True, 'reason': '', 'configured': True}
    except urllib.error.HTTPError as e:
        return {'sent': False, 'reason': f'LINE API error {e.code}', 'configured': True}
    except Exception as e:
        return {'sent': False, 'reason': f'LINE send failed: {e}', 'configured': True}


def build_reminder_text(now=None):
    """Plain-text summary of pending work for LINE / scheduler use."""
    now = now or timezone.now()
    fu = follow_up_items(now)
    po = order_items(now.date())

    lines = [f'Karen Silver - Staff Reminders ({now.strftime("%Y-%m-%d %H:%M")})', '']
    if fu['overdue']:
        lines.append(f"OVERDUE FOLLOW-UPS ({len(fu['overdue'])}):")
        for f in fu['overdue']:
            lines.append(f"- {f['quotation_number']} {f['customer_name']} ({f['channel']}) was due {f['follow_up_at']}")
    if fu['due_today']:
        lines.append(f"FOLLOW-UPS DUE TODAY ({len(fu['due_today'])}):")
        for f in fu['due_today']:
            lines.append(f"- {f['quotation_number']} {f['customer_name']} ({f['channel']}) at {f['follow_up_at']}")
    if fu['upcoming']:
        lines.append(f"UPCOMING FOLLOW-UPS ({len(fu['upcoming'])}):")
        for f in fu['upcoming']:
            lines.append(f"- {f['quotation_number']} {f['customer_name']} ({f['channel']}) at {f['follow_up_at']}")
    if po['overdue']:
        lines.append(f"OVERDUE ORDERS ({len(po['overdue'])}):")
        for o in po['overdue']:
            lines.append(f"- {o['po_number']} {o['customer_name']} due {o['expected_date']} ({o['remaining_qty']} undelivered)")
    if po['due_soon']:
        lines.append(f"ORDERS DUE SOON ({len(po['due_soon'])}):")
        for o in po['due_soon']:
            lines.append(f"- {o['po_number']} {o['customer_name']} due {o['expected_date']} ({o['remaining_qty']} undelivered)")
    if po['open']:
        lines.append(f"OPEN / INCOMPLETE ORDERS ({len(po['open'])}):")
        for o in po['open']:
            lines.append(f"- {o['po_number']} {o['customer_name']} due {o['expected_date']} ({o['remaining_qty']} undelivered)")
    if len(lines) == 2:
        lines.append('Nothing pending.')
    return '\n'.join(lines)
