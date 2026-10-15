from django.shortcuts import render,HttpResponse, redirect, get_object_or_404
from django.db import models, transaction, IntegrityError
import hashlib
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit, urljoin, unquote
from django.core.files.base import ContentFile
from PIL import Image as PillowImage
from .models import *
import uuid
import os
import tempfile
import threading
import re
from openpyxl import Workbook
from django.http import JsonResponse
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth import authenticate, login as auth_login, logout
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.contrib.auth.hashers import make_password
from django.core.files.images import ImageFile
from math import ceil
from django.core import serializers
from openpyxl import load_workbook
from decimal import Decimal
from time import time
from qrcode import make
from barcode import Code39
from .filters import *
import json
from barcode.writer import ImageWriter
from django.conf import settings
from django.db.models import Q, Sum
import io
import csv as _csv
import urllib.request
import urllib.error
from django.views.decorators.cache import cache_page
from django.core.cache import cache
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from .notifications import (build_notifications, build_reminder_text,
                            follow_up_items, line_configured, order_items,
                            send_line_message, _OPEN_PO_STATUSES)
from .permissions import (admin_required, deny, is_admin, perm_required,
                          user_can, ALL_PERMISSION_KEYS, PERMISSION_GROUPS,
                          DEFAULT_STAFF_PERMISSIONS, TAB_PERMISSION_KEYS)
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

def _product_import_cache_key(import_id):
    return f'product_import:{import_id}'

def _get_product_import_state(import_id):
    return cache.get(_product_import_cache_key(import_id))

def _set_product_import_state(import_id, state, timeout_seconds=3600):
    cache.set(_product_import_cache_key(import_id), state, timeout=timeout_seconds)

def _update_product_import_state(import_id, **updates):
    state = _get_product_import_state(import_id) or {}
    state.update(updates)
    _set_product_import_state(import_id, state)
    return state

def _parse_price_to_decimal(val):
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() == 'null':
        return None
    s = s.replace(',', '')
    if '/' in s:
        return None
    if re.fullmatch(r'[+-]?(\d+(\.\d+)?|\.\d+)', s) is None:
        return None
    try:
        return Decimal(s)
    except Exception:
        return None

def _normalize_import_image_url(value):
    parts = urlsplit(value.strip())
    if parts.scheme.lower() not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('Image URL must be an HTTP(S) URL without credentials')
    host = parts.hostname.lower()
    host = f'[{host}]' if ':' in host else host
    port = parts.port
    if port and (parts.scheme.lower(), port) not in (('http', 80), ('https', 443)):
        host += f':{port}'
    return urlunsplit((parts.scheme.lower(), host, parts.path or '/', parts.query, ''))


def _validate_image_download_url(url):
    parts = urlsplit(_normalize_import_image_url(url))
    addresses = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(addr[4][0]).is_global for addr in addresses):
        raise ValueError('External image URL must resolve to a public address')


class _ImageRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_image_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_import_image(url):
    _validate_image_download_url(url)
    request = urllib.request.Request(url, headers={'User-Agent': 'Karen-Product-Import'})
    opener = urllib.request.build_opener(_ImageRedirectHandler())
    limit = 10 * 1024 * 1024
    with opener.open(request, timeout=30) as response:
        image_data = response.read(limit + 1)
    if not image_data or len(image_data) > limit:
        raise ValueError('Image is empty or exceeds the 10 MB limit')
    with PillowImage.open(io.BytesIO(image_data)) as image:
        if image.width * image.height > 25000000:
            raise ValueError('Image dimensions exceed the import limit')
        image_format = image.format
        image.verify()
    extensions = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp', 'GIF': '.gif'}
    if image_format not in extensions:
        raise ValueError('Unsupported image format')
    return image_data, extensions[image_format]


def _local_import_image(value, media_origin):
    parts = urlsplit(value)
    if parts.query:
        return None
    media_url = urlsplit(urljoin(media_origin or '', settings.MEDIA_URL))
    if parts.scheme or parts.netloc:
        if not media_url.netloc or parts.netloc.lower() != media_url.netloc.lower():
            return None
    media_path = media_url.path
    if not media_path.endswith('/'):
        media_path += '/'
    path = unquote(parts.path)
    if path.startswith(media_path):
        name = path[len(media_path):]
    elif not parts.scheme and not parts.netloc and '/' not in path:
        name = f'product_images/{path}'
    else:
        return None
    image = Image.objects.filter(image=name).order_by('id').first()
    if image and image.image.storage.exists(image.image.name):
        return image
    return None


def _download_and_link_external_images(product, image_url_data, media_origin=None):
    """Resolve the complete imported image set before replacing product links.

    For non-HTTP URLs (plain filenames), falls back to existing filename-based
    matching against already-uploaded Image objects.

    Duplicate prevention uses the full source URL identity independently of
    storage filenames. A failed replacement leaves existing links intact.
    """
    if image_url_data is None:
        return

    # Deduplicate input URLs while preserving order
    seen_urls = set()
    resolved = []
    for index, value in enumerate(image_url_data, start=1):
        url_str = str(value or '').strip()
        if not url_str:
            continue
        try:
            # Strip query parameters and fragments only from storage filenames
            parts = urlsplit(url_str)
            filename = os.path.basename(unquote(parts.path))
            local = _local_import_image(url_str, media_origin)
            if local:
                resolved.append(local)
                continue

            # If it's an HTTP(S) URL, resolve by source identity
            if parts.scheme.lower() in ('http', 'https'):
                # Check whether this exact source URL was already resolved
                # Storage renaming must not change the identity
                source_url = _normalize_import_image_url(url_str)
                source_hash = hashlib.sha256(source_url.encode('utf-8')).hexdigest()
                if source_hash in seen_urls:
                    # Already resolved in this replacement set
                    continue
                seen_urls.add(source_hash)

                # Check if an Image object with this source exists in the DB
                existing = Image.objects.filter(source_url_hash=source_hash).first()
                if existing and existing.image and existing.image.storage.exists(existing.image.name):
                    # Reuse the existing Image without downloading
                    resolved.append(existing)
                    continue

                # Download the external image
                image_data, ext = _read_import_image(source_url)
                # Determine extension from verified image content
                stem = re.sub(r'[^A-Za-z0-9_-]', '_', os.path.splitext(filename)[0])[:40] or 'image'
                # Ensure filename has an extension
                filename = f'{stem}_{source_hash[:16]}{ext}'
                if existing:
                    existing.image.save(filename, ContentFile(image_data), save=True)
                    resolved.append(existing)
                else:
                    image = Image(source_url=source_url, source_url_hash=source_hash)
                    try:
                        with transaction.atomic():
                            image.image.save(filename, ContentFile(image_data), save=False)
                            image.save(force_insert=True)
                    except IntegrityError:
                        if image.image.name:
                            image.image.storage.delete(image.image.name)
                        image = Image.objects.get(source_url_hash=source_hash)
                    resolved.append(image)
            else:
                # Non-HTTP URL: treat as filename, match existing Image objects
                raise ValueError('No matching uploaded image')
        except Exception as exc:
            raise ValueError(f'Image {index} could not be imported; previous product images were kept ({type(exc).__name__}).') from None

    unique_images = {image.pk: image for image in resolved}
    with transaction.atomic():
        locked_product = Product.objects.select_for_update().get(pk=product.pk)
        locked_product.images.set(unique_images.values())
        links = ProductImageLink.objects.filter(parent_code=locked_product.parent_code, child_code=locked_product.child_code)
        links.exclude(image_id__in=unique_images).delete()
        for image in unique_images.values():
            ProductImageLink.objects.get_or_create(image=image, parent_code=locked_product.parent_code, child_code=locked_product.child_code)

EXPECTED_IMPORT_COLUMNS = [
    'Parent Code', 'Child Code', 'Location', 'QTY', 'kpo', 'pairing_set',
    'weight', 'base', 'increment', 'Category', 'Unit', 'Product Description',
    'Note', 'Image URL'
]

def _validate_import_columns(header_values):
    expected_lower = [c.lower() for c in EXPECTED_IMPORT_COLUMNS]
    actual_lower = [h.lower() for h in header_values if h]
    expected_set = set(expected_lower)
    actual_set = set(actual_lower)
    missing = [EXPECTED_IMPORT_COLUMNS[i] for i, e in enumerate(expected_lower) if e not in actual_set]
    extra = [h for h in header_values if h and h.lower() not in expected_set]
    if missing or extra:
        parts = []
        if missing:
            parts.append("Missing columns: " + ", ".join(missing))
        if extra:
            parts.append("Unexpected columns: " + ", ".join(extra))
        return False, missing, extra, "; ".join(parts) + ". The import file must contain exactly these columns: " + ", ".join(EXPECTED_IMPORT_COLUMNS)
    return True, [], [], ""

def _get_computed_prices(product, price_setting=None):
    ps = price_setting if price_setting is not None else PriceSetting.get_singleton()
    base = product.base or Decimal('0')
    increment = product.increment or Decimal('0')
    silver = ps.silver_price or Decimal('0')
    usd_r = ps.usd_rate or Decimal('1')
    eur_r = ps.euro_rate or Decimal('1')
    thb_unit = base + (increment * silver)
    usd_unit = thb_unit / usd_r if usd_r else Decimal('0')
    eur_unit = thb_unit / eur_r if eur_r else Decimal('0')
    return {
        'thai_baht': str(thb_unit),
        'usd_rate': str(usd_unit),
        'euro_rate': str(eur_unit),
        'thai_baht_value': float(thb_unit),
        'usd_rate_value': float(usd_unit),
        'euro_rate_value': float(eur_unit),
    }

def _compute_bi_value(product):
    """B/I = (Base * 7) / (Increment * 7), 2 decimals.
    Returns None when base/increment missing or increment is 0."""
    return compute_bi(getattr(product, 'base', None), getattr(product, 'increment', None))


# ---- Google Sheets integration helpers ----
GOOGLE_SHEET_EXPORT_URL = "https://docs.google.com/spreadsheets/d/{sid}/export?format=xlsx"

def _extract_spreadsheet_id(url_or_id):
    """Accept a full URL or a bare spreadsheet ID; return the ID."""
    s = (url_or_id or '').strip()
    if not s:
        return ''
    m = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', s)
    if m:
        return m.group(1)
    if re.fullmatch(r'[a-zA-Z0-9-_]+', s):
        return s
    return ''

def _download_google_sheet_xlsx(spreadsheet_id, timeout=60):
    """Download a public Google Sheet as xlsx. Returns path to a temp file.
    Raises ValueError with a clear message on any failure."""
    url = GOOGLE_SHEET_EXPORT_URL.format(sid=spreadsheet_id)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get('Content-Type', '')
            content = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError('Google Sheet not found or not publicly accessible. Make sure the sheet is set to "Anyone with the link can view".')
        raise ValueError(f'Google Sheets returned HTTP {e.code}')
    except urllib.error.URLError as e:
        raise ValueError(f'Could not reach Google Sheets: {e.reason}')
    except Exception as e:
        raise ValueError(f'Could not reach Google Sheets: {e}')
    # Non-public sheets may return 200 with an HTML login/error page instead of xlsx
    ct_lower = (content_type or '').lower()
    if 'html' in ct_lower or content[:5] == b'<!DOC' or content[:5] == b'<html' or content[:1] == b'<':
        raise ValueError('Google Sheet is not publicly accessible. Make sure the sheet is set to "Anyone with the link can view".')
    if not content:
        raise ValueError('The Google Sheet is empty.')
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx')
    tmp.write(content)
    tmp.close()
    return tmp.name

def _fetch_google_sheet_tabs(spreadsheet_id, timeout=60):
    """Return list of {'gid': name, 'name': name} for a public Google Sheet.
    Uses the xlsx export endpoint and reads sheet names with openpyxl.
    Raises ValueError with a clear message on any failure."""
    tmp_path = _download_google_sheet_xlsx(spreadsheet_id, timeout=timeout)
    try:
        wb = load_workbook(tmp_path, read_only=True)
        names = list(wb.sheetnames)
        wb.close()
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise ValueError('Could not read the Google Sheet. The file may be corrupted or in an unsupported format.')
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    except Exception:
        pass
    if not names:
        raise ValueError('No sheets/tabs found in the Google Sheet.')
    return [{'gid': name, 'name': name} for name in names]

def _download_google_sheet_to_xlsx(spreadsheet_id, sheet_name, timeout=60):
    """Download a Google Sheet, extract the selected tab, return path to temp xlsx.
    Raises ValueError with a clear message on any failure."""
    tmp_path = _download_google_sheet_xlsx(spreadsheet_id, timeout=timeout)
    try:
        wb = load_workbook(tmp_path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise ValueError('Could not read the downloaded Google Sheet.')
    # Extract only the selected sheet so wb.active points to it
    if sheet_name and sheet_name in wb.sheetnames:
        for name in list(wb.sheetnames):
            if name != sheet_name:
                wb.remove(wb[name])
        wb.active = 0
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    except Exception:
        pass
    out = tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx')
    out_path = out.name
    out.close()
    wb.save(out_path)
    wb.close()
    return out_path

def _process_product_import_job(import_id, file_path, max_import_rows, media_origin=None):
    try:
        wb = load_workbook(file_path, data_only=True)
        sheet = wb.active

        header_values = [str(c.value).strip().lower() if c.value is not None else '' for c in sheet[1]]

        is_valid, missing, extra, err_msg = _validate_import_columns(header_values)
        if not is_valid:
            _update_product_import_state(import_id, status='error', message=err_msg)
            return

        def find_idx(candidates):
            for name in candidates:
                if name in header_values:
                    return header_values.index(name)
            return None

        idx_parent = find_idx(['parent code','parent_code'])
        idx_child = find_idx(['child code','child_code'])
        idx_location = find_idx(['location'])
        idx_stock = find_idx(['qty','stock','quantity'])
        idx_kpo = find_idx(['kpo'])
        idx_pairing = find_idx(['pairing set','pairing_set','pairing sets'])
        idx_weight = find_idx(['weight'])
        idx_base = find_idx(['base','thai baht','thai_baht','thb'])
        idx_increment = find_idx(['increment','inc'])
        idx_note = find_idx(['note'])
        idx_category = find_idx(['category','tag'])
        idx_unit = find_idx(['unit'])
        idx_description = find_idx(['product description','description'])
        idx_images_names = find_idx(['images','image names','images names','image_names'])
        idx_image_url = find_idx(['image url','image_url'])

        required_indices = [idx_parent, idx_child, idx_location, idx_stock, idx_weight, idx_base, idx_note]
        if any(i is None for i in required_indices):
            _update_product_import_state(import_id, status='error', message='Missing required columns in Excel header')
            return

        total_rows = max(0, (sheet.max_row or 1) - 1)
        total_rows = min(total_rows, max_import_rows)
        _update_product_import_state(
            import_id,
            total_rows=total_rows,
            processed_rows=0,
            success_count=0,
            failed_count=0,
            skipped_count=0,
            failed_rows=[],
            failed_rows_truncated=False,
            failed_rows_truncated_count=0,
            status='running',
            message=''
        )

        processed_rows = 0
        success_count = 0
        failed_count = 0
        skipped_count = 0
        failed_rows = []
        failed_rows_limit = 5000
        failed_rows_truncated_count = 0

        for row_index, fields in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=1):
            if row_index > max_import_rows:
                break

            processed_rows = row_index
            _update_product_import_state(import_id, processed_rows=processed_rows)

            try:
                if not fields or (idx_parent is not None and not fields[idx_parent]):
                    skipped_count += 1
                    continue

                parent_code = fields[idx_parent]
                child_code = fields[idx_child]
                location = fields[idx_location]
                stock = fields[idx_stock]
                kpo = fields[idx_kpo] if idx_kpo is not None else None

                if parent_code is None or str(parent_code).strip() == '':
                    raise ValueError('Missing Parent Code')
                if child_code is None or str(child_code).strip() == '':
                    raise ValueError('Missing Child Code')
                if location is None or str(location).strip() == '':
                    raise ValueError('Missing Location')

                pairing_sets_data = None
                if idx_pairing is not None and fields[idx_pairing]:
                    pairing_sets_data = str(fields[idx_pairing]).split(',')

                try:
                    weight = Decimal(str(fields[idx_weight])) if fields[idx_weight] is not None else Decimal('0.00')
                except (ValueError, TypeError):
                    weight = Decimal('0.00')

                try:
                    base_val = Decimal(str(fields[idx_base])) if fields[idx_base] is not None else Decimal('0')
                except (ValueError, TypeError):
                    base_val = Decimal('0')
                try:
                    increment_val = Decimal(str(fields[idx_increment])) if idx_increment is not None and fields[idx_increment] is not None else Decimal('0')
                except (ValueError, TypeError):
                    increment_val = Decimal('0')
                note = fields[idx_note]
                description = fields[idx_description] if idx_description is not None else None
                unit = fields[idx_unit] if idx_unit is not None else None
                category_name = fields[idx_category] if idx_category is not None else None

                images_names_data = None
                if idx_images_names is not None and len(fields) > idx_images_names and fields[idx_images_names]:
                    images_names_data = str(fields[idx_images_names]).split(',')

                image_url_data = None
                if idx_image_url is not None and len(fields) > idx_image_url:
                    image_url_data = str(fields[idx_image_url] or '').split(',')

                child_code_str = str(child_code).strip()
                product = Product.objects.filter(child_code=child_code_str).first()
                if product:
                    product.location = location
                    product.stock = stock
                    product.kpo = kpo
                    product.weight = weight
                    product.base = base_val
                    product.increment = increment_val
                    product.note = note
                    if description is not None:
                        product.description = description
                    if unit is not None:
                        product.unit = unit
                    if category_name:
                        tag_obj, _ = Tag.objects.get_or_create(name=str(category_name).strip())
                        product.tag = tag_obj
                    product.save()
                else:
                    product = Product.objects.create(
                        parent_code=str(parent_code).strip(),
                        child_code=child_code_str,
                        location=str(location).strip(),
                        stock=stock,
                        kpo=kpo,
                        weight=weight,
                        base=base_val,
                        increment=increment_val,
                        note=note,
                        description=description or None,
                        unit=unit or None
                    )
                    if category_name:
                        try:
                            tag_obj, _ = Tag.objects.get_or_create(name=str(category_name).strip())
                            product.tag = tag_obj
                            product.save()
                        except Exception:
                            pass

                pairing_sets = []
                if pairing_sets_data:
                    for pair_value in pairing_sets_data:
                        pair_value_str = str(pair_value).strip()
                        if not pair_value_str:
                            continue
                        pairing_set_obj, created = PairingSet.objects.get_or_create(pair_value=pair_value_str)
                        pairing_sets.append(pairing_set_obj)
                    product.pairing_set.set(pairing_sets)

                if images_names_data:
                    images_names = []
                    for image_name in images_names_data:
                        image_name_str = str(image_name).strip()
                        if not image_name_str:
                            continue
                        image_name_obj, created = ImageName.objects.get_or_create(name=image_name_str)
                        images_names.append(image_name_obj)
                    product.images_names.set(images_names)
                    image_files = Image.objects.filter(image__in=[f'product_images/{img_name.name}' for img_name in images_names])
                    if image_url_data is None:
                        product.images.set(image_files)
                    product.save()

                if image_url_data is not None:
                    _download_and_link_external_images(product, image_url_data, media_origin)
                    product.save()

                if not product.qrcode_image:
                    qr_img = make(child_code_str)
                    qr_img_name = f'{str(time())}.png'
                    buffer = io.BytesIO()
                    qr_img.save(buffer, format='PNG')
                    buffer.seek(0)
                    product.qrcode_image.save(qr_img_name, ImageFile(buffer), save=True)

                if not product.barcode_image:
                    barcode_obj = Code39(child_code_str, writer=ImageWriter())
                    buffer = io.BytesIO()
                    barcode_obj.write(buffer)
                    buffer.seek(0)
                    product.barcode_image.save(f'{child_code_str}.png', ImageFile(buffer), save=True)

                product.save()
                success_count += 1
            except Exception as e:
                failed_count += 1
                excel_row_number = row_index + 1
                err_text = str(e) if str(e) else e.__class__.__name__
                if len(err_text) > 300:
                    err_text = err_text[:300]
                child_code_value = None
                try:
                    if fields and idx_child is not None and len(fields) > idx_child:
                        child_code_value = fields[idx_child]
                except Exception:
                    child_code_value = None

                if len(failed_rows) < failed_rows_limit:
                    failed_rows.append({
                        'row': excel_row_number,
                        'child_code': (str(child_code_value).strip() if child_code_value is not None else ''),
                        'error': err_text
                    })
                else:
                    failed_rows_truncated_count += 1

                _update_product_import_state(
                    import_id,
                    success_count=success_count,
                    failed_count=failed_count,
                    skipped_count=skipped_count,
                    failed_rows=failed_rows,
                    failed_rows_truncated=(failed_rows_truncated_count > 0),
                    failed_rows_truncated_count=failed_rows_truncated_count
                )

            if row_index % 25 == 0:
                _update_product_import_state(
                    import_id,
                    success_count=success_count,
                    failed_count=failed_count,
                    skipped_count=skipped_count,
                    failed_rows=failed_rows,
                    failed_rows_truncated=(failed_rows_truncated_count > 0),
                    failed_rows_truncated_count=failed_rows_truncated_count
                )

        _update_product_import_state(
            import_id,
            status='done',
            processed_rows=processed_rows,
            success_count=success_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            failed_rows=failed_rows,
            failed_rows_truncated=(failed_rows_truncated_count > 0),
            failed_rows_truncated_count=failed_rows_truncated_count,
            message='Import completed'
        )
    except Exception as e:
        _update_product_import_state(import_id, status='error', message=str(e))
    finally:
        try:
            try:
                wb.close()
            except Exception:
                pass
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass

@login_required
@perm_required('products_import')
def product_import_start(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Invalid request method'}, status=405)

    xlsx_file = request.FILES.get('xlsx_file')
    if not xlsx_file:
        return JsonResponse({'success': False, 'message': 'No file uploaded'}, status=400)

    if not (xlsx_file.name.endswith('.xlsx') or xlsx_file.name.endswith('.xls')):
        return JsonResponse({'success': False, 'message': 'File is not xlsx type'}, status=400)

    max_import_rows = 10000
    import_id = uuid.uuid4().hex

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(xlsx_file.name)[1] or '.xlsx')
    tmp_path = tmp.name
    try:
        for chunk in xlsx_file.chunks():
            tmp.write(chunk)
    finally:
        tmp.close()

    try:
        wb = load_workbook(tmp_path, data_only=True, read_only=True)
        sheet = wb.active
        total_rows = max(0, (sheet.max_row or 1) - 1)
        header_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), [])
        header_values = [str(c).strip().lower() if c is not None else '' for c in header_row]
        wb.close()
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return JsonResponse({'success': False, 'message': 'Error reading Excel file'}, status=400)

    is_valid, missing, extra, err_msg = _validate_import_columns(header_values)
    if not is_valid:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return JsonResponse({'success': False, 'message': err_msg}, status=400)

    if total_rows > max_import_rows:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return JsonResponse({'success': False, 'message': f'Import limit is {max_import_rows} rows per upload. Please split the file and try again.'}, status=400)

    _set_product_import_state(import_id, {
        'import_id': import_id,
        'total_rows': total_rows,
        'processed_rows': 0,
        'success_count': 0,
        'failed_count': 0,
        'skipped_count': 0,
        'failed_rows': [],
        'failed_rows_truncated': False,
        'failed_rows_truncated_count': 0,
        'status': 'queued',
        'message': ''
    })

    t = threading.Thread(target=_process_product_import_job, args=(import_id, tmp_path, max_import_rows, request.build_absolute_uri('/')), daemon=True)
    t.start()

    _update_product_import_state(import_id, status='running')
    return JsonResponse({'success': True, 'import_id': import_id, 'total_rows': total_rows, 'max_rows': max_import_rows})

@login_required
@perm_required('products_import')
def product_import_status(request):
    import_id = request.GET.get('import_id')
    if not import_id:
        return JsonResponse({'success': False, 'message': 'Missing import_id'}, status=400)

    state = _get_product_import_state(import_id)
    if not state:
        return JsonResponse({'success': False, 'message': 'Import not found'}, status=404)

    return JsonResponse({'success': True, **state})


# ---- Google Sheets import views ----

@login_required
@perm_required('products_import')
def google_sheet_status(request):
    """Return the currently connected Google Sheet config + live tab list."""
    cfg = GoogleSheetConfig.get_singleton()
    sid = cfg.spreadsheet_id
    base = {
        'success': True,
        'configured': bool(sid),
        'spreadsheet_id': sid,
        'spreadsheet_url': cfg.spreadsheet_url,
        'sheet_gid': cfg.sheet_gid,
        'sheet_name': cfg.sheet_name,
    }
    if not sid:
        base['connection_status'] = 'not_configured'
        base['tabs'] = []
        return JsonResponse(base)
    try:
        tabs = _fetch_google_sheet_tabs(sid)
        base['tabs'] = tabs
        base['connection_status'] = 'connected'
        return JsonResponse(base)
    except Exception as e:
        base['tabs'] = []
        base['connection_status'] = 'error'
        base['error'] = str(e)
        return JsonResponse(base)


@login_required
@perm_required('products_import')
def google_sheet_connect(request):
    """Save a Google Sheet URL/ID and validate it is reachable. Returns the tab list."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Invalid request method'}, status=405)
    try:
        data = json.loads(request.body or '{}')
    except Exception:
        return JsonResponse({'success': False, 'message': 'Invalid request body'}, status=400)
    url_or_id = (data.get('spreadsheet_url') or '').strip()
    sid = _extract_spreadsheet_id(url_or_id)
    if not sid:
        return JsonResponse({'success': False, 'message': 'Invalid Google Sheet URL or ID'}, status=400)
    try:
        tabs = _fetch_google_sheet_tabs(sid)
    except ValueError as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Could not connect to Google Sheet: {e}'}, status=400)
    if not tabs:
        return JsonResponse({'success': False, 'message': 'No sheets/tabs found in the Google Sheet'}, status=400)
    cfg = GoogleSheetConfig.get_singleton()
    cfg.spreadsheet_id = sid
    cfg.spreadsheet_url = url_or_id
    chosen_gid = str((data.get('sheet_gid') or '').strip())
    chosen = next((t for t in tabs if str(t['gid']) == chosen_gid), tabs[0])
    cfg.sheet_gid = str(chosen['gid'])
    cfg.sheet_name = chosen['name']
    cfg.save()
    return JsonResponse({
        'success': True,
        'tabs': tabs,
        'sheet_gid': cfg.sheet_gid,
        'sheet_name': cfg.sheet_name,
        'spreadsheet_id': sid,
    })


@login_required
@perm_required('products_import')
def google_sheet_select_tab(request):
    """Change the selected sheet tab (gid) for the connected Google Sheet."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Invalid request method'}, status=405)
    try:
        data = json.loads(request.body or '{}')
    except Exception:
        return JsonResponse({'success': False, 'message': 'Invalid request body'}, status=400)
    gid = str((data.get('sheet_gid') or '').strip())
    cfg = GoogleSheetConfig.get_singleton()
    if not cfg.spreadsheet_id:
        return JsonResponse({'success': False, 'message': 'No Google Sheet connected'}, status=400)
    try:
        tabs = _fetch_google_sheet_tabs(cfg.spreadsheet_id)
    except ValueError as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Could not reach Google Sheet: {e}'}, status=400)
    chosen = next((t for t in tabs if str(t['gid']) == gid), None)
    if not chosen:
        return JsonResponse({'success': False, 'message': 'Selected tab not found in the Google Sheet'}, status=400)
    cfg.sheet_gid = gid
    cfg.sheet_name = chosen['name']
    cfg.save()
    return JsonResponse({'success': True, 'sheet_gid': gid, 'sheet_name': chosen['name']})


@login_required
@perm_required('products_import')
def google_sheet_import_start(request):
    """Download the connected Google Sheet tab and run the same import job as file upload."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Invalid request method'}, status=405)
    cfg = GoogleSheetConfig.get_singleton()
    if not cfg.spreadsheet_id or not cfg.sheet_gid:
        return JsonResponse({'success': False, 'message': 'No Google Sheet connected. Please connect a sheet first.'}, status=400)

    max_import_rows = 10000
    import_id = uuid.uuid4().hex

    # Download the Google Sheet tab and convert to xlsx
    try:
        tmp_path = _download_google_sheet_to_xlsx(cfg.spreadsheet_id, cfg.sheet_gid)
    except ValueError as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Failed to download Google Sheet: {e}'}, status=400)

    # Pre-validate headers (same as file import)
    try:
        wb = load_workbook(tmp_path, data_only=True, read_only=True)
        sheet = wb.active
        total_rows = max(0, (sheet.max_row or 1) - 1)
        header_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), [])
        header_values = [str(c).strip().lower() if c is not None else '' for c in header_row]
        wb.close()
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return JsonResponse({'success': False, 'message': 'Error reading Google Sheet data'}, status=400)

    is_valid, missing, extra, err_msg = _validate_import_columns(header_values)
    if not is_valid:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return JsonResponse({'success': False, 'message': err_msg}, status=400)

    if total_rows > max_import_rows:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return JsonResponse({'success': False, 'message': f'Import limit is {max_import_rows} rows per import. Please reduce the sheet size and try again.'}, status=400)

    _set_product_import_state(import_id, {
        'import_id': import_id,
        'total_rows': total_rows,
        'processed_rows': 0,
        'success_count': 0,
        'failed_count': 0,
        'skipped_count': 0,
        'failed_rows': [],
        'failed_rows_truncated': False,
        'failed_rows_truncated_count': 0,
        'status': 'queued',
        'message': ''
    })

    t = threading.Thread(target=_process_product_import_job, args=(import_id, tmp_path, max_import_rows, request.build_absolute_uri('/')), daemon=True)
    t.start()

    _update_product_import_state(import_id, status='running')
    return JsonResponse({'success': True, 'import_id': import_id, 'total_rows': total_rows, 'max_rows': max_import_rows})


@csrf_exempt
@login_required()
@perm_required('products_images')
def upload_bulk_images(request):
    if request.method == 'POST':
        uploaded_images = request.FILES.getlist("file")
        if not uploaded_images:
            return JsonResponse({'success': False, 'message': 'No images selected!'})
        
        uploaded_count = 0
        linked_count = 0
        results = []
        
        for uploaded_image in uploaded_images:
            try:
                # Check if image already exists
                image, created = Image.objects.get_or_create(
                    defaults={'image': uploaded_image},
                    image__icontains=uploaded_image.name.split('.')[0]
                )
                
                if created:
                    uploaded_count += 1
                    # Try to link with products based on image name
                    image_name_without_ext = uploaded_image.name.split('.')[0]
                    products = Product.objects.filter(
                        Q(parent_code__icontains=image_name_without_ext) |
                        Q(child_code__icontains=image_name_without_ext)
                    )
                    
                    linked_products = []
                    for product in products:
                        if not product.images.filter(id=image.id).exists():
                            product.images.add(image)
                            product.save()
                            
                            # Create persistent link
                            ProductImageLink.objects.get_or_create(
                                image=image,
                                parent_code=product.parent_code,
                                child_code=product.child_code
                            )
                            
                            linked_products.append(f"{product.parent_code}-{product.child_code}")
                            linked_count += 1
                    
                    results.append({
                        'name': uploaded_image.name,
                        'status': 'uploaded',
                        'linked_products': linked_products
                    })
                else:
                    results.append({
                        'name': uploaded_image.name,
                        'status': 'already_exists',
                        'linked_products': []
                    })
            except Exception as e:
                results.append({
                    'name': uploaded_image.name,
                    'status': 'error',
                    'error': str(e),
                    'linked_products': []
                })
        
        return JsonResponse({
            'success': True, 
            'message': f'Successfully uploaded {uploaded_count} images and linked to {linked_count} products!',
            'results': results,
            'uploaded_count': uploaded_count,
            'linked_count': linked_count
        })


@csrf_exempt
@login_required()
@perm_required('products_images')
def image_management(request):
    """View for managing images and linking them to products"""
    if request.method == 'GET':
        # Get all images with their linked products
        images = Image.objects.prefetch_related('product_set').all().order_by('-id')
        products = Product.objects.all().order_by('child_code')
        
        # Prepare image data with linked products
        image_data = []
        for image in images:
            linked_products = image.product_set.all()
            image_data.append({
                'id': image.id,
                'name': image.image.name.split('/')[-1] if image.image else 'No name',
                'url': image.image.url if image.image else '',
                'linked_products': [{
                    'id': p.id,
                    'parent_code': p.parent_code,
                    'child_code': p.child_code
                } for p in linked_products]
            })
        
        return render(request, 'image_management.html', {
            'images': image_data,
            'products': products
        })
    
    elif request.method == 'POST':
        data = json.loads(request.body)
        action = data.get('action')
        
        if action == 'link_image':
            image_id = data.get('image_id')
            product_ids = data.get('product_ids', [])
            
            try:
                image = Image.objects.get(id=image_id)
                products = Product.objects.filter(id__in=product_ids)
                
                # Add new product links without clearing existing ones
                for product in products:
                    if not product.images.filter(id=image.id).exists():
                        product.images.add(image)
                    
                    # Create persistent link
                    ProductImageLink.objects.get_or_create(
                        image=image,
                        parent_code=product.parent_code,
                        child_code=product.child_code
                    )
                
                return JsonResponse({
                    'success': True,
                    'message': f'Image linked to {len(products)} products successfully!'
                })
            except Image.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'message': 'Image not found!'
                })
            except Exception as e:
                return JsonResponse({
                    'success': False,
                    'message': f'Error linking image: {str(e)}'
                })
        
        elif action == 'unlink_image':
            image_id = data.get('image_id')
            product_id = data.get('product_id')
            
            try:
                image = Image.objects.get(id=image_id)
                product = Product.objects.get(id=product_id)
                product.images.remove(image)
                
                # Remove persistent link only if manually unlinked
                ProductImageLink.objects.filter(
                    image=image,
                    parent_code=product.parent_code,
                    child_code=product.child_code
                ).delete()
                
                return JsonResponse({
                    'success': True,
                    'message': 'Image unlinked successfully!'
                })
            except (Image.DoesNotExist, Product.DoesNotExist):
                return JsonResponse({
                    'success': False,
                    'message': 'Image or product not found!'
                })
            except Exception as e:
                return JsonResponse({
                    'success': False,
                    'message': f'Error unlinking image: {str(e)}'
                })
        
        elif action == 'delete_image':
            image_id = data.get('image_id')
            
            try:
                image = Image.objects.get(id=image_id)
                image.delete()
                
                return JsonResponse({
                    'success': True,
                    'message': 'Image deleted successfully!'
                })
            except Image.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'message': 'Image not found!'
                })
            except Exception as e:
                return JsonResponse({
                    'success': False,
                    'message': f'Error deleting image: {str(e)}'
                })
    
    return JsonResponse({'success': False, 'message': 'Invalid request method'})


_FORM_POST_PERMS = {
    'bulk-images': 'products_images',
    'bulk-create': 'products_import',
    'product-grouping': 'products_edit',
    'create': 'products_edit',
    'update': 'products_edit',
    'delete': 'products_delete',
    'delete_all_products': 'products_delete',
}


@login_required()
@perm_required('products')
def form_view(request):
    if request.method == 'POST':
        data = request.POST
        type = data.get('type')
        required_perm = _FORM_POST_PERMS.get(type, 'products_edit')
        if not user_can(request.user, required_perm):
            return deny(request)
        if type == 'bulk-images':
            uploaded_images = request.FILES.getlist("images") 
            if not uploaded_images:
                messages.error(request,"Select at least one image!")
                return redirect('form')    
            for uploaded_image in uploaded_images:
                # Check if image already exists by filename
                existing_image = Image.objects.filter(image__icontains=uploaded_image.name).first()
                if existing_image:
                    image = existing_image
                    created = False
                else:
                    image = Image.objects.create(image=uploaded_image)
                    created = True
                
                products = Product.objects.all()
                for product in products:
                    if product.images_names.filter(name=uploaded_image.name).exists():
                        product.images.add(image)
                        product.save()
            messages.success(request,"Successfully uploaded images!")
            return redirect('form')
        if type == 'bulk-create':
            try:
                xlsx_file = request.FILES.get("xlsx_file")
                if not (xlsx_file.name.endswith('.xlsx') or xlsx_file.name.endswith('.xls')):
                    messages.error(request,'File is not xlsx type')
                    return redirect("form")

                max_import_rows = 10000
                wb = load_workbook(xlsx_file, data_only=True)
                sheet = wb.active
                if sheet.max_row and (sheet.max_row - 1) > max_import_rows:
                    messages.error(request, f'Import limit is {max_import_rows} rows per upload. Please split the file and try again.')
                    return redirect("form")
                header_values = [str(c.value).strip().lower() if c.value is not None else '' for c in sheet[1]]

                is_valid, missing, extra, err_msg = _validate_import_columns(header_values)
                if not is_valid:
                    messages.error(request, err_msg)
                    return redirect("form")

                def find_idx(candidates):
                    for name in candidates:
                        if name in header_values:
                            return header_values.index(name)
                    return None
                idx_parent = find_idx(['parent code','parent_code'])
                idx_child = find_idx(['child code','child_code'])
                idx_location = find_idx(['location'])
                idx_stock = find_idx(['qty','stock','quantity'])
                idx_kpo = find_idx(['kpo'])
                idx_pairing = find_idx(['pairing set','pairing_set','pairing sets'])
                idx_weight = find_idx(['weight'])
                idx_base = find_idx(['base','thai baht','thai_baht','thb'])
                idx_increment = find_idx(['increment','inc'])
                idx_note = find_idx(['note'])
                idx_category = find_idx(['category','tag'])
                idx_unit = find_idx(['unit'])
                idx_description = find_idx(['product description','description'])
                idx_images_names = find_idx(['images','image names','images names','image_names'])
                idx_image_url = find_idx(['image url','image_url'])
                required_indices = [idx_parent, idx_child, idx_location, idx_stock, idx_weight, idx_base, idx_note]
                if any(i is None for i in required_indices):
                    messages.error(request,'Missing required columns in Excel header')
                    return redirect("form")
                for row_index, fields in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=1):
                    if row_index > max_import_rows:
                        break
                    if not fields or (idx_parent is not None and not fields[idx_parent]):
                        continue
                    parent_code = fields[idx_parent]
                    child_code = fields[idx_child]
                    location = fields[idx_location]
                    stock = fields[idx_stock]
                    kpo = fields[idx_kpo] if idx_kpo is not None else None
                    pairing_sets_data = None
                    if idx_pairing is not None and fields[idx_pairing]:
                        pairing_sets_data = str(fields[idx_pairing]).split(',')
                    try:
                        weight = Decimal(str(fields[idx_weight])) if fields[idx_weight] is not None else Decimal('0.00')
                    except (ValueError, TypeError):
                        weight = Decimal('0.00')
                    try:
                        base_val = Decimal(str(fields[idx_base])) if fields[idx_base] is not None else Decimal('0')
                    except (ValueError, TypeError):
                        base_val = Decimal('0')
                    try:
                        increment_val = Decimal(str(fields[idx_increment])) if idx_increment is not None and fields[idx_increment] is not None else Decimal('0')
                    except (ValueError, TypeError):
                        increment_val = Decimal('0')
                    note = fields[idx_note]
                    description = fields[idx_description] if idx_description is not None else None
                    unit = fields[idx_unit] if idx_unit is not None else None
                    category_name = fields[idx_category] if idx_category is not None else None
                    images_names_data = None
                    if idx_images_names is not None and len(fields) > idx_images_names and fields[idx_images_names]:
                        images_names_data = str(fields[idx_images_names]).split(',')
                    image_url_data = None
                    if idx_image_url is not None and len(fields) > idx_image_url:
                        image_url_data = str(fields[idx_image_url] or '').split(',')
                    product =  Product.objects.filter(child_code=child_code).first()
                    if product:
                        product.location = location
                        product.stock = stock
                        product.kpo = kpo
                        product.weight = weight
                        product.base = base_val
                        product.increment = increment_val
                        product.note = note
                        if description is not None:
                            product.description = description
                        if unit is not None:
                            product.unit = unit
                        if category_name:
                            tag_obj, _ = Tag.objects.get_or_create(name=str(category_name).strip())
                            product.tag = tag_obj
                        product.save()
                    else:      
                        product = Product.objects.create(parent_code=parent_code, child_code=child_code, location=location, stock=stock, kpo=kpo, weight=weight, base=base_val, increment=increment_val, note=note, description=description or None, unit=unit or None)
                        if category_name:
                            try:
                                tag_obj, _ = Tag.objects.get_or_create(name=str(category_name).strip())
                                product.tag = tag_obj
                                product.save()
                            except Exception:
                                pass
                    pairing_sets = []
                    if pairing_sets_data:
                        for pair_value in pairing_sets_data:
                            pairing_set_obj, created = PairingSet.objects.get_or_create(pair_value=str(pair_value).strip())
                            pairing_sets.append(pairing_set_obj)
                        product.pairing_set.set(pairing_sets)
                    if images_names_data:
                        images_names = []
                        for image_name in images_names_data:
                            image_name_obj, created = ImageName.objects.get_or_create(name=str(image_name).strip())
                            images_names.append(image_name_obj)
                        product.images_names.set(images_names)
                        image_files = Image.objects.filter(image__in=[f'product_images/{img_name.name}' for img_name in images_names])
                        if image_url_data is None:
                            product.images.set(image_files)
                        product.save()

                    if image_url_data is not None:
                        _download_and_link_external_images(product, image_url_data, request.build_absolute_uri('/'))
                        product.save()

                    if not product.qrcode_image:
                        qr_img = make(child_code)
                        qr_img_name = f'{str(time())}.png'
                        buffer = io.BytesIO()
                        qr_img.save(buffer, format='PNG')
                        buffer.seek(0)
                        product.qrcode_image.save(qr_img_name, ImageFile(buffer), save=True)

                    if not product.barcode_image:
                        barcode_obj = Code39(child_code, writer=ImageWriter())
                        buffer = io.BytesIO()
                        barcode_obj.write(buffer)
                        buffer.seek(0)
                        product.barcode_image.save(f'{child_code}.png', ImageFile(buffer), save=True)
                    
                    product.save()
                    
            except Exception as e:
                messages.error(request, f"Error processing Excel file: {str(e)}. Please check your file format and data.")
                return redirect('form')
                
            messages.success(request,"Successfully saved data from file!")
            return redirect('form')
        
        if type == 'product-grouping':
            tag = data.get('tag')
            products_ids = request.POST.getlist('products')
            tag_obj, created = Tag.objects.get_or_create(name=tag)
            products = Product.objects.filter(id__in=products_ids).update(tag=tag_obj)
            messages.success(request,"Successfully Grouping!")
            return redirect('form')

        if type == 'create':
            parent_code = data.get('parent_code')
            child_code = data.get('child_code')
            location = data.get('location')
            stock = data.get('stock')
            kpo = data.get('kpo')
            status = data.get('status')
            pairing_set_ids = request.POST.getlist('pairing_set')
            weight = data.get('weight')
            base = data.get('base')
            increment = data.get('increment')
            note = data.get('note')
            description = data.get('description')
            unit = data.get('unit')
            tag_id = data.get('tag_id')
            uploaded_images = request.FILES.getlist("images")

            product_check =  Product.objects.filter(child_code=child_code).first()
            if product_check:
                messages.error(request, "Product with this child code already exists!")
                return redirect('form')

            product = Product.objects.create(parent_code=parent_code, child_code=child_code, location=location, stock=stock, kpo=kpo, weight=weight, base=base, increment=increment, note=note, description=description, unit=unit)
            product.pairing_set.add(*pairing_set_ids)
            if tag_id:
                try:
                    product.tag = Tag.objects.get(id=int(tag_id))
                    product.save()
                except Exception:
                    pass

            if uploaded_images:
                for uploaded_image in uploaded_images:
                    image, created = Image.objects.get_or_create(image__exact=uploaded_image.name)
                    if created:
                        image.image = uploaded_image
                        image.save()
                    product.images.add(image)
                    product.save()

            qr_img = make(child_code)
            qr_img_name = f'{str(time())}.png'
            buffer = io.BytesIO()
            qr_img.save(buffer, format='PNG')
            buffer.seek(0)
            product.qrcode_image.save(qr_img_name, ImageFile(buffer), save=True)

            barcode_obj = Code39(child_code, writer=ImageWriter())
            buffer = io.BytesIO()
            barcode_obj.write(buffer)
            buffer.seek(0)
            product.barcode_image.save(f'{child_code}.png', ImageFile(buffer), save=True)

            product.save()
            messages.success(request,"Successfully Created!")
            return redirect('form')
        if type == 'update':
            pro_id = data.get('pro_id')
            parent_code = data.get('parent_code')
            child_code = data.get('child_code')
            location = data.get('location')
            stock = data.get('stock')
            kpo = data.get('kpo')
            status = data.get('status')
            pairing_set_ids = request.POST.getlist('pairing_set')
            weight = data.get('weight')
            base = data.get('base')
            increment = data.get('increment')
            note = data.get('note')
            description = data.get('description')
            unit = data.get('unit')
            tag_id = data.get('tag_id')

            product_check =  Product.objects.filter(child_code=child_code).exclude(id=pro_id).first()
            if product_check:
                messages.error(request, "Product with this child code already exists!")
                return redirect('form')

            product = Product.objects.get(id=pro_id)

            product.parent_code=parent_code
            product.child_code=child_code
            product.location=location
            product.stock=stock
            product.kpo=kpo
            product.weight=weight
            product.base=base
            product.increment=increment
            product.note=note
            product.description=description
            product.unit=unit
            product.pairing_set.clear()
            product.pairing_set.add(*pairing_set_ids)
            if tag_id:
                try:
                    product.tag = Tag.objects.get(id=int(tag_id))
                except Exception:
                    product.tag = None
            
            # Handle image uploads for updates
            uploaded_images = request.FILES.getlist("images")
            if uploaded_images:
                for uploaded_image in uploaded_images:
                    image, created = Image.objects.get_or_create(image__exact=uploaded_image.name)
                    if created:
                        image.image = uploaded_image
                        image.save()
                    product.images.add(image)
                    product.save()

            qr_img = make(child_code)
            qr_img_name = f'{str(time())}.png'
            buffer = io.BytesIO()
            qr_img.save(buffer, format='PNG')
            buffer.seek(0)
            product.qrcode_image.save(qr_img_name, ImageFile(buffer), save=True)

            barcode_obj = Code39(child_code, writer=ImageWriter())
            buffer = io.BytesIO()
            barcode_obj.write(buffer)
            buffer.seek(0)
            product.barcode_image.save(f'{child_code}.png', ImageFile(buffer), save=True)
            
            product.save()
            messages.success(request,"Successfully Updated!")
            return redirect('form')
        elif type == 'delete':
            pro_id = data.get('product_id')
            Product.objects.get(id=pro_id).delete()
            messages.success(request,"Successfully Deleted!")
            return redirect('form')
        elif type == 'delete_all_products':
            Product.objects.all().delete()
            messages.success(request,"Successfully deleted all products!")
            return redirect('form')
    elif request.method == 'DELETE':
        if not user_can(request.user, 'products_delete'):
            return deny(request)
        data = json.loads(request.body)
        if data.get('type') == 'bulk-delete':
            product_ids = data.get('ids', [])
            if product_ids:
                Product.objects.filter(id__in=product_ids).delete()
                return JsonResponse({'success': True, 'message': 'Selected products deleted successfully'})
            return JsonResponse({'success': False, 'message': 'No products selected'})
    elif request.method == "GET":
        get_req_type = request.GET.get('get_req_type','all')
        if get_req_type == "individual":
            pro_id = request.GET.get('pro_id')
            pro_obj = Product.objects.prefetch_related('pairing_set', 'images').filter(id=pro_id).first()
            pro_json = {
                'parent_code':pro_obj.parent_code,
                'child_code':pro_obj.child_code,
                'location':pro_obj.location,
                'stock':pro_obj.stock,
                'kpo':pro_obj.kpo,
                'weight':pro_obj.weight,
                'base':str(pro_obj.base),
                'increment':str(pro_obj.increment),
                'bi': _compute_bi_value(pro_obj),
                'note': pro_obj.note,
                'description': pro_obj.description,
                'unit': pro_obj.unit,
                'tag_id': pro_obj.tag.id if pro_obj.tag else None,
                'tag_name': pro_obj.tag.name if pro_obj.tag else '',
                'pairing_set':[ps.id for ps in pro_obj.pairing_set.all()],
                'images': [{'url': image.image.url} for image in pro_obj.images.all()]
            }
            return JsonResponse(pro_json,status=200)
        elif get_req_type == "all":
            pairing_set_obj = PairingSet.objects.all().order_by('-id')
            tags_obj = Tag.objects.all().order_by('name')
            
        return render(request, 'form.html', {
            'pairing_set' : pairing_set_obj,
            'tags': tags_obj,
            'price_setting': PriceSetting.get_singleton(),
        })

@login_required
@perm_required('products')
def product_api(request):
    # Handle sorting
    sort_by = request.GET.get('sort_by', 'id')
    sort_order = request.GET.get('sort_order', 'desc')
    
    # Map frontend sort fields to model fields
    sort_field_mapping = {
        'parent_code': 'parent_code',
        'child_code': 'child_code',
        'location': 'location',
        'kpo': 'kpo',
        'price': 'base',
        'stock': 'stock',
        'weight': 'weight',
        'id': 'id'
    }
    
    # Get the actual field name
    actual_sort_field = sort_field_mapping.get(sort_by, 'id')
    
    # Apply sort order
    if sort_order == 'desc':
        order_by = f'-{actual_sort_field}'
    else:
        order_by = actual_sort_field
    
    product_obj = Product.objects.prefetch_related('images_names', 'images', 'pairing_set').order_by(order_by)
    product_obj = ProductFilter(request.GET, queryset=product_obj)
    ps = PriceSetting.get_singleton()
    data = [{
        'id': pro_obj.id,
        'image':pro_obj.images.first().image.url if pro_obj.images.exists() else '',
        'parent_code':pro_obj.parent_code,
        'child_code':pro_obj.child_code,
        'location':pro_obj.location,
        'stock':pro_obj.stock,
        'kpo':pro_obj.kpo,
        'weight':pro_obj.weight,
        'base':str(pro_obj.base),
        'increment':str(pro_obj.increment),
        **(prices := _get_computed_prices(pro_obj, ps)),
        'bi': _compute_bi_value(pro_obj),
        'note': pro_obj.note,
        'description': pro_obj.description,
        'unit': pro_obj.unit,
        'tag':pro_obj.tag.name if pro_obj.tag else '',
        'pairing_set':[ps.id for ps in pro_obj.pairing_set.all()],
        'image_count': pro_obj.images.count()
    } for pro_obj in product_obj.qs]

    data = custom_paginate_queryset(
        queryset=data,
        page=int(request.GET.get('page', 1)),
        page_size=int(request.GET.get('page_size', 10))
    )
    return JsonResponse(data, safe=False)



@login_required()     
@perm_required('products')
def product_detail(request, id):
    prod = Product.objects.get(id=id)
    prices = _get_computed_prices(prod)
    return render(request, 'product_detail.html', {'prod' : prod, 'prices' : prices, 'bi': _compute_bi_value(prod)})

# @cache_page(60 * 60 * 24)  # Cache for one day (24 hours)
@csrf_exempt
@login_required()
def single_product(request):
    # SKU lookup is shared by the Products tab and the receiving workflow.
    if not (user_can(request.user, 'products') or user_can(request.user, 'orders_receive')):
        return deny(request)
    if request.method == 'GET':
        # Support multiple search parameters
        child_code = request.GET.get('child_code')
        parent_code = request.GET.get('parent_code')
        code = request.GET.get('code')  # Generic code parameter for QR/barcode scanning
        
        # Trim whitespace from all code parameters
        if code:
            code = code.strip()
        if child_code:
            child_code = child_code.strip()
        if parent_code:
            parent_code = parent_code.strip()

        # If generic 'code' parameter is provided, search in both child_code and parent_code
        if code:
            # Try exact match first
            prod = Product.objects.prefetch_related('pairing_set', 'images').filter(
                Q(child_code=code) | Q(parent_code=code)
            ).first()
            # Try case-insensitive match
            if not prod:
                prod = Product.objects.prefetch_related('pairing_set', 'images').filter(
                    Q(child_code__iexact=code) | Q(parent_code__iexact=code)
                ).first()
            # Try combined code lookup (split on last hyphen)
            if not prod and '-' in code:
                try:
                    parent_part, child_part = code.rsplit('-', 1)
                    prod = Product.objects.prefetch_related('pairing_set', 'images').filter(
                        parent_code__iexact=parent_part.strip(),
                        child_code__iexact=child_part.strip()
                    ).first()
                except ValueError:
                    prod = None
        elif child_code:
            prod = Product.objects.prefetch_related('pairing_set', 'images').filter(child_code__iexact=child_code).first()
        elif parent_code:
            prod = Product.objects.prefetch_related('pairing_set', 'images').filter(parent_code__iexact=parent_code).first()
        else:
            return JsonResponse({'error': 'Please provide code, child_code, or parent_code parameter'}, status=400)
            
        if prod:
            prices = _get_computed_prices(prod)
            # Get matching items (products with the same parent code)
            matching_items = []
            matching_products = Product.objects.prefetch_related('images').filter(
                parent_code=prod.parent_code
            ).exclude(id=prod.id)[:20]
            for matching_prod in matching_products:
                matching_items.append({
                    'code': matching_prod.child_code,
                    'parent_code': matching_prod.parent_code,
                    'image_url': matching_prod.images.first().image.url if matching_prod.images.exists() else None
                })
            
            prod_data = {
                'success': True,
                'id': prod.id,
                'parent_code': prod.parent_code,
                'child_code': prod.child_code,
                'location': prod.location,
                'stock': prod.stock,
                'kpo': prod.kpo,
                'pairing_set': [{'value': pairing.pair_value} for pairing in prod.pairing_set.all()],
                'weight': str(prod.weight),
                'base': str(prod.base),
                'increment': str(prod.increment),
                'thai_baht': prices['thai_baht'],
                'usd_rate': prices['usd_rate'],
                'euro_rate': prices['euro_rate'],
                'bi': _compute_bi_value(prod),
                'note': prod.note,
                'description': prod.description,
                'unit': prod.unit,
                'tag': prod.tag.name if prod.tag else '',
                'level': str((PriceSetting.get_singleton().silver_price or Decimal('0')) - Decimal('10')),
                'images': [{'url': request.build_absolute_uri(image.image.url)} for image in prod.images.all()],
                'qrcode_image': request.build_absolute_uri(prod.qrcode_image.url) if prod.qrcode_image else None,
                'barcode_image': request.build_absolute_uri(prod.barcode_image.url) if prod.barcode_image else None,
                'matching_items': matching_items,
                'created_at': prod.created_at.isoformat() if prod.created_at else None,
                'updated_at': prod.updated_at.isoformat() if prod.updated_at else None,
            }
            return JsonResponse(prod_data, safe=False)
        else:
            return JsonResponse({
                'success': False,
                'message': 'Product not found',
                'error': 'No product found with the provided code'
            }, status=404)
    return JsonResponse({'error': 'Only GET requests are allowed'}, status=405)
    
def login_view(request):
    if request.user.is_authenticated:
            return redirect("dashboard")
    if request.method == 'POST':
        data = request.POST
        username = data['username']
        password = data['password']
        user = authenticate(request, username=username, password=password)
        if user is not None:
            auth_login(request, user)
            messages.success(request,"Successfully Login!")
            return redirect("dashboard")
        else:
            messages.warning(request,"Incorrect email or password!")        
    return render(request, 'login.html')

@login_required()
def change_password(request):
    if request.method == "POST":
        p1 = request.POST.get('password1')
        p2 = request.POST.get('password2')
        user = User.objects.get(username=request.user.username)
        if p1 == p2:
            user.set_password(p1) 
            user.save()   
            messages.success(request,"Successfully Change Password!")
            return redirect('dashboard')
        else:
            messages.error(request,"Password does not match!")
            return redirect('change_password')
    return render(request, 'change_password.html')

@login_required()
def user_logout(request):
    try:
        logout(request)
        messages.success(request, 'Logout Successfully!')
        return redirect('login')
    except:
            messages.warning(request, 'Request is not responed please check your internet connection and try again!')
            return redirect('login')

def custom_paginate_queryset(queryset, page, page_size):
    total_items = len(queryset)
    total_pages = ceil(total_items / page_size)
    start_index = (page - 1) * page_size
    end_index = start_index + page_size
    paginated_queryset = queryset[start_index:end_index]
    
    return {
        'pagination': {
            'next': page + 1 if page < total_pages else None,
            'previous': page - 1 if page > 1 else None,
            'count': total_items,
            'total_pages': total_pages,
            'current_page': page,
        },
        'results': paginated_queryset
    }


@login_required()
@perm_required('products_export')
def export_to_excel(request):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Karen Data'

    columns = ['Parent Code', 'Child Code', 'Location', 'QTY', 'kpo', 'pairing_set', 'weight', 'base', 'increment', 'Category', 'Unit', 'Product Description', 'B/I', 'Note', 'Image URL']
    ws.append(columns)

    product = Product.objects.prefetch_related('pairing_set', 'images').order_by('-id')

    for prod in product:
        pairing_set_values = ', '.join([ps.pair_value for ps in prod.pairing_set.all()]) if prod.pairing_set.exists() else None
        ws.append([
            prod.parent_code,
            prod.child_code,
            prod.location,
            prod.stock,
            prod.kpo,
            pairing_set_values,
            prod.weight,
            prod.base,
            prod.increment,
            (prod.tag.name if getattr(prod, 'tag', None) else ''),
            (prod.unit or ''),
            (prod.description or ''),
            _compute_bi_value(prod),
            prod.note,
            request.build_absolute_uri(prod.images.first().image.url) if prod.images.exists() else '',
        ])

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = 'attachment; filename=karen_report.xlsx'
    wb.save(response)

    return response


@login_required()
@perm_required('products_export')
def export_selected_to_excel(request):
    if request.method == 'POST':
        selected_ids = request.POST.getlist('ids[]')  
        if not selected_ids:
            return JsonResponse({'success': False, 'error': 'No products selected.'}, status=400)

        wb = Workbook()
        ws = wb.active
        ws.title = 'Selected Products'

        columns = ['Parent Code', 'Child Code', 'Location', 'QTY', 'kpo', 'pairing_set', 'weight', 'base', 'increment', 'Category', 'Unit', 'Product Description', 'B/I', 'Note', 'Image URL']
        ws.append(columns)

        products = Product.objects.filter(id__in=selected_ids).prefetch_related('pairing_set', 'images')

        for prod in products:
            pairing_set_values = ', '.join([ps.pair_value for ps in prod.pairing_set.all()]) if prod.pairing_set.exists() else None
            ws.append([
                prod.parent_code,
                prod.child_code,
                prod.location,
                prod.stock,
                prod.kpo,
                pairing_set_values,
                prod.weight,
                prod.base,
                prod.increment,
                (prod.tag.name if getattr(prod, 'tag', None) else ''),
                (prod.unit or ''),
                (prod.description or ''),
                _compute_bi_value(prod),
                prod.note,
                request.build_absolute_uri(prod.images.first().image.url) if prod.images.exists() else '',
            ])

        response = HttpResponse(
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = 'attachment; filename=selected_products.xlsx'
        wb.save(response)

        return response

    return JsonResponse({'success': False, 'error': 'Invalid request method.'}, status=405)


@login_required
@perm_required('products_pairing')
def pairing_set_api(request):
    pair_objs = PairingSet.objects.all().order_by('-id')
    pair_objs = ProductFilter(request.GET, queryset=pair_objs)
    data = [{'id': pair_obj.id, 'name': pair_obj.pair_value} for pair_obj in pair_objs.qs]

    data = custom_paginate_queryset(
        queryset=data,
        page=int(request.GET.get('page', 1)),
        page_size=int(request.GET.get('page_size', 10))
    )
    return JsonResponse(data, safe=False)


@csrf_exempt
@login_required()
@perm_required('products_images')
def image_api(request):
    """API endpoint for image management operations"""
    if request.method == 'GET':
        # Get all images with pagination
        images = Image.objects.prefetch_related('product_set').all().order_by('-id')
        
        # Apply search filter if provided
        search_query = request.GET.get('search', '').strip()
        if search_query:
            images = images.filter(image__icontains=search_query)
        
        # Apply filter for linked/unlinked images
        filter_type = request.GET.get('filter', '').strip()
        if filter_type == 'linked':
            images = images.filter(product__isnull=False).distinct()
        elif filter_type == 'unlinked':
            images = images.filter(product__isnull=True)
        
        # Prepare image data
        image_data = []
        for image in images:
            linked_products = image.product_set.all()
            image_data.append({
                'id': image.id,
                'alt_text': image.image.name.split('/')[-1] if image.image else 'No name',
                'image_url': image.image.url if image.image else '',
                'uploaded_at': image.id,  # Using id as placeholder for upload date
                'linked_products_count': linked_products.count(),
                'linked_products': [{
                    'id': p.id,
                    'name': f"{p.parent_code} - {p.child_code}",
                    'parent_code': p.parent_code,
                    'child_code': p.child_code
                } for p in linked_products[:5]]  # Limit to first 5 for performance
            })
        
        # Apply pagination
        paginated_data = custom_paginate_queryset(
            queryset=image_data,
            page=int(request.GET.get('page', 1)),
            page_size=int(request.GET.get('page_size', 20))
        )
        
        return JsonResponse(paginated_data, safe=False)
    
    elif request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'link_products':
            image_id = request.POST.get('image_id')
            product_ids = request.POST.get('product_ids', '').split(',')
            
            try:
                image = Image.objects.get(id=image_id)
                selected_product_ids = [int(pid) for pid in product_ids if pid.strip()]
                
                # Get currently linked products
                current_linked_products = set(image.product_set.values_list('id', flat=True))
                selected_products_set = set(selected_product_ids)
                
                # Add new links (products that are selected but not currently linked)
                products_to_add = selected_products_set - current_linked_products
                for product_id in products_to_add:
                    try:
                        product = Product.objects.get(id=product_id)
                        product.images.add(image)
                        
                        # Create persistent link
                        ProductImageLink.objects.get_or_create(
                            image=image,
                            parent_code=product.parent_code,
                            child_code=product.child_code
                        )
                    except Product.DoesNotExist:
                        continue
                
                # Remove links (products that were linked but are no longer selected)
                products_to_remove = current_linked_products - selected_products_set
                for product_id in products_to_remove:
                    try:
                        product = Product.objects.get(id=product_id)
                        product.images.remove(image)
                        
                        # Remove persistent link when manually unlinked
                        ProductImageLink.objects.filter(
                            image=image,
                            parent_code=product.parent_code,
                            child_code=product.child_code
                        ).delete()
                    except Product.DoesNotExist:
                        continue
                
                return JsonResponse({'success': True, 'message': 'Products linked successfully'})
            except Image.DoesNotExist:
                return JsonResponse({'success': False, 'message': 'Image not found'})
            except Exception as e:
                return JsonResponse({'success': False, 'message': str(e)})
        
        elif action == 'auto_link':
            try:
                linked_count = 0
                images = Image.objects.all()
                
                for image in images:
                    if image.image:
                        # Extract filename without extension
                        filename = image.image.name.split('/')[-1].split('.')[0]
                        
                        # Try to match with product codes
                        matching_products = Product.objects.filter(
                            models.Q(parent_code__icontains=filename) |
                            models.Q(child_code__icontains=filename)
                        )
                        
                        if matching_products.exists():
                            for product in matching_products:
                                if not product.images.filter(id=image.id).exists():
                                    product.images.add(image)
                                    
                                    # Create persistent link
                                    ProductImageLink.objects.get_or_create(
                                        image=image,
                                        parent_code=product.parent_code,
                                        child_code=product.child_code
                                    )
                            linked_count += 1
                
                return JsonResponse({
                    'success': True, 
                    'message': f'Auto-linking completed successfully',
                    'linked_count': linked_count
                })
            except Exception as e:
                return JsonResponse({'success': False, 'message': str(e)})
        
        elif action == 'delete_image':
            image_id = request.POST.get('image_id')
            
            try:
                image = Image.objects.get(id=image_id)
                
                # Store the file path before deleting the object
                image_path = None
                if image.image:
                    image_path = image.image.path
                
                # Remove the image from all products first
                for product in image.product_set.all():
                    product.images.remove(image)
                
                # Delete the image object from database
                image.delete()
                
                # Delete the physical file from media folder
                if image_path and os.path.exists(image_path):
                    os.remove(image_path)
                
                return JsonResponse({
                    'success': True, 
                    'message': 'Image deleted successfully'
                })
            except Image.DoesNotExist:
                return JsonResponse({'success': False, 'message': 'Image not found'})
            except Exception as e:
                return JsonResponse({'success': False, 'message': f'Error deleting image: {str(e)}'})
        
        elif action == 'bulk_delete_images':
            image_ids_str = request.POST.get('image_ids')
            
            if not image_ids_str:
                return JsonResponse({'success': False, 'message': 'No image IDs provided'})
            
            try:
                # Parse the comma-separated image IDs
                image_ids = [int(id.strip()) for id in image_ids_str.split(',') if id.strip()]
                
                if not image_ids:
                    return JsonResponse({'success': False, 'message': 'No valid image IDs provided'})
                
                # Get all images to delete
                images_to_delete = Image.objects.filter(id__in=image_ids)
                
                if not images_to_delete.exists():
                    return JsonResponse({'success': False, 'message': 'No images found with the provided IDs'})
                
                deleted_count = 0
                failed_deletions = []
                
                for image in images_to_delete:
                    try:
                        # Store the file path before deleting the object
                        image_path = None
                        if image.image:
                            image_path = image.image.path
                        
                        # Remove the image from all products first
                        for product in image.product_set.all():
                            product.images.remove(image)
                        
                        # Delete the image object from database
                        image.delete()
                        
                        # Delete the physical file from media folder
                        if image_path and os.path.exists(image_path):
                            os.remove(image_path)
                        
                        deleted_count += 1
                        
                    except Exception as e:
                        failed_deletions.append(f"Image ID {image.id}: {str(e)}")
                
                if failed_deletions:
                    return JsonResponse({
                        'success': True,
                        'deleted_count': deleted_count,
                        'message': f'Deleted {deleted_count} images. Failed to delete some images: {"; ".join(failed_deletions)}'
                    })
                else:
                    return JsonResponse({
                        'success': True,
                        'deleted_count': deleted_count,
                        'message': f'Successfully deleted {deleted_count} images'
                    })
                    
            except ValueError:
                return JsonResponse({'success': False, 'message': 'Invalid image ID format'})
            except Exception as e:
                return JsonResponse({'success': False, 'message': f'Error during bulk deletion: {str(e)}'})

        elif action == 'delete_all_images':
            try:
                images = Image.objects.all()
                image_paths = []
                for image in images:
                    try:
                        if image.image and hasattr(image.image, 'path'):
                            image_paths.append(image.image.path)
                    except Exception:
                        continue

                deleted_count = images.count()
                images.delete()

                for image_path in image_paths:
                    try:
                        if image_path and os.path.exists(image_path):
                            os.remove(image_path)
                    except Exception:
                        continue

                return JsonResponse({
                    'success': True,
                    'deleted_count': deleted_count,
                    'message': f'Successfully deleted {deleted_count} images'
                })
            except Exception as e:
                return JsonResponse({'success': False, 'message': f'Error deleting all images: {str(e)}'})
        
        return JsonResponse({'success': False, 'message': 'Invalid action'})
    
    return JsonResponse({'success': False, 'message': 'Invalid request method'})


@csrf_exempt
@login_required
@perm_required('products')
def product_images_api(request, product_id):
    """API endpoint to get all images for a specific product"""
    if request.method == 'GET':
        try:
            product = Product.objects.get(id=product_id)
            images = product.image.all() if hasattr(product, 'image') else []
            
            image_data = []
            for image in images:
                if image.image:
                    image_data.append({
                        'url': image.image.url,
                        'alt': f'{product.parent_code} - {product.child_code}'
                    })
            
            # If no images found, return the main product image if available
            if not image_data and product.images.exists():
                main_image = product.images.first()
                image_data.append({
                    'url': main_image.image.url,
                    'alt': f'{product.parent_code} - {product.child_code}'
                })
            
            return JsonResponse({
                'success': True,
                'images': image_data,
                'product_name': f'{product.parent_code} - {product.child_code}',
                'image_count': len(image_data)
            })
            
        except Product.DoesNotExist:
            return JsonResponse({
                'success': False,
                'message': 'Product not found'
            })
    
    return JsonResponse({'success': False, 'message': 'Invalid request method'})

@login_required
@perm_required('products_pairing')
def pairing_set_view(request):
    if request.method == 'POST':
        data = request.POST
        method_type = data.get('method_type')
        if method_type == 'export':
            # Export pairing sets to Excel
            pairing_sets = PairingSet.objects.all().order_by('pair_value')
            
            # Create workbook and worksheet
            workbook = Workbook()
            worksheet = workbook.active
            worksheet.title = "Pairing Sets"
            
            # Add headers
            headers = ['ID', 'Pair Value']
            for col_num, header in enumerate(headers, 1):
                worksheet.cell(row=1, column=col_num, value=header)
            
            # Add data
            for row_num, pairing_set in enumerate(pairing_sets, 2):
                worksheet.cell(row=row_num, column=1, value=pairing_set.id)
                worksheet.cell(row=row_num, column=2, value=pairing_set.pair_value)
            
            # Create HTTP response with Excel file
            response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            response['Content-Disposition'] = 'attachment; filename=pairing_sets_export.xlsx'
            workbook.save(response)
            return response
        elif method_type == 'create':
            # Check if Excel file is uploaded
            if 'excel_file' in request.FILES:
                excel_file = request.FILES['excel_file']
                
                # Validate file extension
                if not excel_file.name.endswith(('.xlsx', '.xls')):
                    messages.error(request, 'Please upload a valid Excel file (.xlsx or .xls).')
                    return redirect('pairing_set')
                
                try:
                    # Load the Excel file
                    workbook = load_workbook(excel_file)
                    sheet = workbook.active
                    
                    pair_values = []
                    # Read values from the first column, starting from row 1
                    for row in sheet.iter_rows(min_row=1, max_col=1, values_only=True):
                        if row[0] is not None:
                            value = str(row[0]).strip()
                            if value:  # Only add non-empty values
                                pair_values.append(value)
                    
                    if not pair_values:
                        messages.error(request, 'No valid pairing set values found in the Excel file.')
                        return redirect('pairing_set')
                    
                    # Process the values
                    created_count = 0
                    skipped_count = 0
                    error_values = []
                    
                    for single_pair_value in pair_values:
                        if not single_pair_value:
                            continue
                        
                        # Check if pairing set already exists
                        if PairingSet.objects.filter(pair_value=single_pair_value).exists():
                            skipped_count += 1
                            continue
                        
                        try:
                            PairingSet.objects.create(pair_value=single_pair_value)
                            created_count += 1
                        except Exception as e:
                            error_values.append(f"{single_pair_value}: {str(e)}")
                    
                    # Prepare success message
                    success_msg = f'Excel upload completed: {created_count} pairing set(s) created'
                    if skipped_count > 0:
                        success_msg += f', {skipped_count} skipped (already exist)'
                    
                    if error_values:
                        success_msg += f'. Errors: {"; ".join(error_values[:3])}'  # Show first 3 errors
                        if len(error_values) > 3:
                            success_msg += f' and {len(error_values) - 3} more errors.'
                    
                    if created_count > 0:
                        messages.success(request, success_msg)
                    else:
                        messages.warning(request, success_msg)
                        
                except Exception as e:
                    messages.error(request, f'Error processing Excel file: {str(e)}')
                
                return redirect('pairing_set')
            else:
                messages.error(request, 'Please upload an Excel file.')
                return redirect('pairing_set')
        elif method_type == 'delete':
            pair_id = data.get('product_id')
            if not pair_id:
                messages.error(request, 'Invalid pair ID.')
                return redirect('pairing_set')
            pair = PairingSet.objects.get(id=pair_id)
            pair.delete()
            messages.success(request, 'Successfully deleted pairing set.')
            return redirect('pairing_set')
        elif method_type == 'delete_multiple':
            selected_ids = request.POST.getlist('selected_pairs')
            if not selected_ids:
                messages.error(request, 'No pairing sets selected for deletion.')
                return redirect('pairing_set')
            try:
                deleted_count = PairingSet.objects.filter(id__in=selected_ids).count()
                PairingSet.objects.filter(id__in=selected_ids).delete()
                messages.success(request, f'Successfully deleted {deleted_count} pairing set(s).')
            except Exception as e:
                messages.error(request, f'Error deleting pairing sets: {str(e)}')
            return redirect('pairing_set')
        elif method_type == 'delete_all':
            PairingSet.objects.all().delete()
            messages.success(request, 'Successfully deleted all pairing sets!')
            return redirect('pairing_set')
    return render(request, 'pairing_set.html')

@login_required
@perm_required('products_pairing')
def pairing_set_print_view(request):
    """Print view for pairing sets"""
    pairing_sets = PairingSet.objects.all().order_by('pair_value')
    context = {
        'pairing_sets': pairing_sets,
        'total_count': pairing_sets.count()
    }
    return render(request, 'pairing_set_print.html', context)


@csrf_exempt
@login_required()
@perm_required('products')
def get_unlinked_images(request):
    """API endpoint to get images that are not linked to any products"""
    if request.method == 'GET':
        # Get images that have no products linked
        unlinked_images = Image.objects.filter(product__isnull=True).distinct()
        
        image_data = []
        for image in unlinked_images:
            image_data.append({
                'id': image.id,
                'name': image.image.name.split('/')[-1] if image.image else 'No name',
                'url': image.image.url if image.image else ''
            })
        
        return JsonResponse({
            'success': True,
            'images': image_data,
            'count': len(image_data)
        })
    
    return JsonResponse({'success': False, 'message': 'Invalid request method'})


@csrf_exempt
@login_required()
def search_products_for_linking(request):
    """API endpoint to search products for image linking (used by the
    Image Management page, so ``products_images`` counts too)."""
    if not (user_can(request.user, 'products')
            or user_can(request.user, 'products_images')):
        return deny(request)
    if request.method == 'GET':
        query = request.GET.get('q', '').strip()
        
        if not query:
            return JsonResponse({'success': False, 'message': 'No search query provided'})
        
        # Search products by parent_code or child_code
        products = Product.objects.filter(
            Q(parent_code__icontains=query) | Q(child_code__icontains=query)
        ).order_by('child_code')[:20]  # Limit to 20 results
        
        product_data = []
        for product in products:
            product_data.append({
                'id': product.id,
                'parent_code': product.parent_code,
                'child_code': product.child_code,
                'location': product.location,
                'display_name': f"{product.parent_code} - {product.child_code}"
            })
        
        return JsonResponse({
            'success': True,
            'products': product_data,
            'count': len(product_data)
        })
    
    return JsonResponse({'success': False, 'message': 'Invalid request method'})


@csrf_exempt
def product_search_android(request):
    """Public product search endpoint for the Android app autocomplete.

    Searches products by parent_code or child_code (case-insensitive,
    partial match) and returns up to 20 results with code, location and
    image url so the app can display a dropdown similar to the web's
    Cart Management -> Add Product to Cart product selector.
    """
    if request.method != 'GET':
        return JsonResponse({'error': 'Only GET method allowed'}, status=405)
    query = (request.GET.get('q') or '').strip()
    if len(query) < 1:
        return JsonResponse({'success': True, 'products': []})
    products = Product.objects.prefetch_related('images').filter(
        Q(parent_code__icontains=query) | Q(child_code__icontains=query)
    ).order_by('parent_code', 'child_code')[:20]
    results = []
    for p in products:
        image_url = ''
        if p.images.exists():
            first_image = p.images.first()
            image_url = request.build_absolute_uri(first_image.image.url)
        results.append({
            'id': p.id,
            'parent_code': p.parent_code,
            'child_code': p.child_code,
            'code': f"{p.parent_code}-{p.child_code}",
            'location': p.location or '',
            'image_url': image_url,
        })
    return JsonResponse({'success': True, 'products': results, 'count': len(results)})



# print('delete')
# print(Image.objects.count())
# a = Product.objects.all()
# for i in a:
#     if i.images_names:
#         for img in i.images_names.all():
#             print(i.id, img.name)

# lis = ['BS0598-6.5', 'ES2314', 'NS0429ME-18', 'ES2313', 'ES2311', 'ES1652', 'ES2312', 'PS0789', 'BS0595ME-6.5', 'NS0429ME-18', 'NS0427ME-18', 'RS0425MPSE-6', 'RS0425MPSE-7', 'RS0425MPSE-8', 'RS0425MPSE-9']
# for i in lis:
#     check_obj = PairingSet.objects.filter(pair_value=i).first()
#     if not check_obj:
#         PairingSet.objects.create(pair_value=i)
#         print('save')

# def generate_QR_code(request):
#     if request.method == 'POST':
#         data = request.POST['text']
#         img = make(data)
#         img_name = f'{str(time())}.png'
#         img.save(settings.MEDIA_ROOT + '/' + img_name)
#         context = {
#             'img_name': img_name
#         }
#         return render(request, 'index.html', context)
#     return render(request, 'index.html')
# for i in Product.objects.all():
#     if i.qrcode_image and i.barcode_image:
#         print(i.id)




# import os
# from datetime import datetime
# import shutil

# def db_backup():
#     db_path = settings.DATABASES['default']['NAME']
#     backup_dir = os.path.join(settings.BASE_DIR, 'db_backup_files') 
#     os.makedirs(backup_dir, exist_ok=True)
    
#     timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
#     backup_file = os.path.join(backup_dir, f'db_backup_{timestamp}.sqlite3')
    
#     try:
#         shutil.copy2(db_path, backup_file)
#     except Exception as e:
#         print(f'Error during backup: {e}')

# db_backup()
# PairingSet.objects.all().delete()
# ImageName.objects.all().delete()
# Product.objects.all().delete()


# Utility functions for image management
def auto_link_images_to_products():
    """Utility function to automatically link images to products based on naming patterns"""
    unlinked_images = Image.objects.filter(product__isnull=True)
    linked_count = 0
    
    for image in unlinked_images:
        if image.image:
            # Extract filename without extension
            filename = image.image.name.split('/')[-1].split('.')[0]
            
            # Try to find matching products
            matching_products = Product.objects.filter(
                Q(parent_code__icontains=filename) | Q(child_code__icontains=filename)
            )
            
            for product in matching_products:
                if not product.images.filter(id=image.id).exists():
                    product.images.add(image)
                    
                    # Create persistent link
                    ProductImageLink.objects.get_or_create(
                        image=image,
                        parent_code=product.parent_code,
                        child_code=product.child_code
                    )
                    linked_count += 1
    
    return linked_count

# Cart Management Views
@login_required
@perm_required('cart')
def cart_management_view(request):
    """Main cart management view with customer list"""
    customers = Customer.objects.all().order_by('name')
    
    # Handle customer creation
    if request.method == 'POST' and request.POST.get('action') == 'create_customer':
        if not user_can(request.user, 'customers_manage'):
            return deny(request)
        customer_name = request.POST.get('customer_name', '').strip()
        if customer_name:
            customer = Customer.objects.create(name=customer_name)
            messages.success(request, f'Customer "{customer_name}" created successfully!')
            return redirect('cart_management')
        else:
            messages.error(request, 'Customer name is required!')
    
    context = {
        'customers': customers,
    }
    return render(request, 'cart_management.html', context)

@csrf_exempt
@login_required
@perm_required('cart')
def customer_api(request):
    """API for customer CRUD operations"""
    if request.method != 'GET' and not user_can(request.user, 'customers_manage'):
        return deny(request)
    if request.method == 'GET':
        customers = Customer.objects.all().order_by('name')
        customer_list = []
        for customer in customers:
            active_cart = customer.carts.filter(is_active=True).first()
            customer_list.append({
                'id': customer.id,
                'name': customer.name,
                'locked': customer.locked,
                'created_at': customer.created_at.strftime('%Y-%m-%d %H:%M'),
                'cart_count': customer.carts.count(),
                'active_cart_id': active_cart.id if active_cart else None,
                'active_cart_items': active_cart.get_total_items() if active_cart else 0,
                'company_name': getattr(customer, 'company_name', '') or '',
                'contact_person': getattr(customer, 'contact_person', '') or '',
                'address': getattr(customer, 'address', '') or '',
                'phone': getattr(customer, 'phone', '') or '',
                'email': getattr(customer, 'email', '') or '',
            })
        return JsonResponse({'customers': customer_list})
    
    elif request.method == 'POST':
        try:
            data = json.loads(request.body)
            action = data.get('action')
            
            if action == 'create':
                name = data.get('name', '').strip()
                if not name:
                    return JsonResponse({'success': False, 'message': 'Customer name is required'})
                
                customer = Customer.objects.create(name=name)
                return JsonResponse({
                    'success': True, 
                    'message': f'Customer "{name}" created successfully!',
                    'customer': {
                        'id': customer.id,
                        'name': customer.name,
                        'created_at': customer.created_at.strftime('%Y-%m-%d %H:%M'),
                        'cart_count': 0,
                        'active_cart_id': None,
                        'active_cart_items': 0,
                    }
                })
            
            elif action == 'update':
                customer_id = data.get('id')
                name = data.get('name', '').strip()

                if not customer_id or not name:
                    return JsonResponse({'success': False, 'message': 'Customer ID and name are required'})

                customer = get_object_or_404(Customer, id=customer_id)
                customer.name = name
                customer.company_name = (data.get('company_name') or '').strip() or None
                customer.contact_person = (data.get('contact_person') or '').strip() or None
                customer.address = (data.get('address') or '').strip() or None
                customer.phone = (data.get('phone') or '').strip() or None
                customer.email = (data.get('email') or '').strip() or None
                customer.save()

                return JsonResponse({
                    'success': True,
                    'message': f'Customer updated successfully!',
                    'customer': {
                        'id': customer.id,
                        'name': customer.name,
                        'created_at': customer.created_at.strftime('%Y-%m-%d %H:%M'),
                    }
                })
            
            elif action == 'delete':
                customer_id = data.get('id')
                if not customer_id:
                    return JsonResponse({'success': False, 'message': 'Customer ID is required'})
                
                customer = get_object_or_404(Customer, id=customer_id)
                customer_name = customer.name
                customer.delete()
                
                return JsonResponse({
                    'success': True,
                    'message': f'Customer "{customer_name}" deleted successfully!'
                })
            
            else:
                return JsonResponse({'success': False, 'message': 'Invalid action'})
                
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'message': 'Invalid JSON data'})
        except Exception as e:
            return JsonResponse({'success': False, 'message': str(e)})


@login_required
@perm_required('cart')
def export_customer_cart_excel(request, customer_id):
    """Export customer cart to Excel file"""
    try:
        customer = get_object_or_404(Customer, id=customer_id)
        cart, created = Cart.objects.get_or_create(customer=customer, is_active=True)
        cart_items = CartItem.objects.filter(cart=cart).select_related('product', 'product__tag')

        # Create workbook and worksheet
        wb = Workbook()
        ws = wb.active
        ws.title = f'Cart - {customer.name}'

        currency = (request.GET.get('currency') or 'THB').upper()
        if currency not in ('THB', 'USD', 'EUR'):
            currency = 'THB'

        def to_float(val):
            try:
                return float(val)
            except Exception:
                try:
                    return float(str(val).strip())
                except Exception:
                    return 0.0

        # --- CENTER SECTION: 39-column flat table ---
        headers = [
            'Invoice Date', 'Doc. Ref.', 'Doc. Note', 'Parent Code', 'Child Code',
            'Description', 'Weight', 'Quantity', 'Unit Price', 'Amount',
            'Total', 'Shipping Cost', 'Deposit', 'Grand Total',
            'Total Style', 'Total Quantity', 'Total Net Weight', 'Total Gross Weight',
            'Earring', 'Ring', 'Pendant', 'Necklace', 'Bracelet and Bangle', 'Anklet', 'Other Accessories',
            'Category', 'Pairing_Set', 'Base', 'Incremental', 'Location', 'KPO', 'Note', 'Image URL',
            'Customer Code', 'Company Name', 'Contact Person', 'Address', 'Phone', 'Email', 'Salesperson',
        ]

        # Pre-compute cart-level data
        items_for_export = []
        total_amount = 0.0
        total_quantity = 0
        total_weight = 0.0
        categories = {}
        for ci in cart_items:
            p = ci.product
            computed = _get_computed_prices(p)
            if currency == 'USD':
                unit = to_float(computed['usd_rate'] or 0)
            elif currency == 'EUR':
                unit = to_float(computed['euro_rate'] or 0)
            else:
                unit = to_float(computed['thai_baht'] or 0)
            amount = unit * ci.quantity if unit else 0.0
            total_amount += amount
            total_quantity += ci.quantity
            total_weight += to_float(p.weight or 0) * ci.quantity
            tag_name = (p.tag.name if p.tag else '') or 'Other'
            categories[tag_name] = categories.get(tag_name, 0) + ci.quantity
            pairing_values = ', '.join([ps.pair_value for ps in p.pairing_set.all()]) if hasattr(p, 'pairing_set') else ''
            items_for_export.append({
                'parent_code': p.parent_code or '',
                'child_code': p.child_code or '',
                'description': p.description or '',
                'weight': to_float(p.weight or 0),
                'quantity': ci.quantity,
                'unit': unit,
                'amount': amount,
                'tag_name': tag_name,
                'pairing_set': pairing_values,
                'base': to_float(p.base or 0),
                'increment': to_float(p.increment or 0),
                'location': p.location or '',
                'kpo': p.kpo or '',
                'note': (ci.note if ci.note else (p.note or '')),
                'image_url': request.build_absolute_uri(p.images.first().image.url) if p.images.exists() else '',
            })

        shipping_amount = float(cart.shipping_amount or 0)
        deposit_amount = float(cart.deposit_amount or 0)
        grand_total = (total_amount + shipping_amount) - deposit_amount
        gross_weight_val = float(cart.gross_weight or 0)
        total_styles = len(items_for_export)

        def cat_count(name):
            return categories.get(name, 0)
        earring_count = cat_count('Earring')
        ring_count = cat_count('Ring')
        pendant_count = cat_count('Pendant')
        necklace_count = cat_count('Necklace')
        bracelet_count = cat_count('Bracelet and Bangle')
        anklet_count = cat_count('Anklet')
        other_count = cat_count('Other Accessories')

        customer_code = str(getattr(cart, 'customer_code', None) or getattr(customer, 'id', ''))
        company_name = getattr(customer, 'company_name', '') or ''
        contact_person = getattr(customer, 'contact_person', '') or customer.name or ''
        phone = getattr(customer, 'phone', '') or ''
        email = getattr(customer, 'email', '') or ''
        address_display = (cart.address_override or getattr(customer, 'address', '') or '')
        salesperson_name = (getattr(cart, 'sales_person', None) or getattr(request.user, 'get_full_name', lambda: '')() or getattr(request.user, 'username', ''))
        doc_ref = str(getattr(cart, 'doc_ref', None) or getattr(cart, 'id', ''))
        doc_note = cart.notes or ''
        invoice_date = timezone.now().strftime('%Y-%m-%d %H:%M')

        product_header_row = 1
        ws.append(headers)

        for it in items_for_export:
            row = [
                invoice_date,
                doc_ref,
                doc_note,
                it['parent_code'],
                it['child_code'],
                it['description'],
                it['weight'],
                it['quantity'],
                it['unit'],
                it['amount'],
                total_amount,
                shipping_amount,
                deposit_amount,
                grand_total,
                total_styles,
                total_quantity,
                total_weight,
                gross_weight_val,
                earring_count,
                ring_count,
                pendant_count,
                necklace_count,
                bracelet_count,
                anklet_count,
                other_count,
                it['tag_name'],
                it['pairing_set'],
                it['base'],
                it['increment'],
                it['location'],
                it['kpo'],
                it['note'],
                it['image_url'],
                customer_code,
                company_name,
                contact_person,
                address_display,
                phone,
                email,
                salesperson_name,
            ]
            ws.append(row)

        # Style the product header row
        from openpyxl.styles import Font, Alignment, PatternFill
        header_fill = PatternFill(start_color="000000", end_color="000000", fill_type="solid")
        for row in ws.iter_rows(min_row=product_header_row, max_row=product_header_row):
            for cell in row:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center")

        # Auto-adjust column widths (safe for merged cells)
        from openpyxl.utils import get_column_letter
        for i, column_cells in enumerate(
            ws.iter_cols(min_col=1, max_col=ws.max_column, min_row=product_header_row, max_row=ws.max_row), start=1
        ):
            max_length = 0
            for cell in column_cells:
                try:
                    if cell.value is None:
                        continue
                    val_len = len(str(cell.value))
                    if val_len > max_length:
                        max_length = val_len
                except Exception:
                    pass
            ws.column_dimensions[get_column_letter(i)].width = min(max_length + 2, 50)

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        response = HttpResponse(
            output.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = (
            f'attachment; filename="cart_{customer.name}_{customer.id}_{timezone.now().strftime("%Y%m%d_%H%M%S")}.xlsx"'
        )
        return response

    except Exception as e:
        messages.error(request, f'Error exporting cart: {str(e)}')
        return redirect('customer_cart', customer_id=customer_id)


@login_required
@perm_required('cart')
def print_customer_cart(request, customer_id):
    """Render customer cart for printing"""
    try:
        customer = get_object_or_404(Customer, id=customer_id)
        cart, created = Cart.objects.get_or_create(customer=customer, is_active=True)
        cart_items = CartItem.objects.filter(cart=cart).select_related('product')
        currency = (request.GET.get('currency') or 'THB').upper()
        if currency not in ('THB', 'USD', 'EUR'):
            currency = 'THB'
        
        # Parse selected columns
        selected_cols_param = request.GET.get('cols', '')
        selected_columns = [c.strip() for c in selected_cols_param.split(',') if c.strip()] or [
            'product_code','qty','wt_g','price_thb','amount_thb','location','bi','note','usd','euro'
        ]

        # Ensure Picture column appears immediately after Product Code
        if 'picture' in selected_columns and 'product_code' in selected_columns:
            selected_columns.remove('picture')
            idx = selected_columns.index('product_code')
            selected_columns.insert(idx + 1, 'picture')
        elif 'picture' in selected_columns and 'product_code' not in selected_columns:
            # If product_code not selected, put picture first
            selected_columns.remove('picture')
            selected_columns.insert(0, 'picture')

        # Helpers
        def to_float(val):
            d = _parse_price_to_decimal(val)
            if d is not None:
                try:
                    return float(d)
                except Exception:
                    return 0.0
            try:
                return float(val)
            except Exception:
                try:
                    return float(str(val).strip())
                except Exception:
                    return 0.0

        # Build items for print in selected currency
        items_for_print = []
        total_amount = 0.0
        for ci in cart_items:
            p = ci.product
            computed = _get_computed_prices(p)
            if currency == 'USD':
                unit = to_float(computed['usd_rate'] or 0)
                label = 'USD'
            elif currency == 'EUR':
                unit = to_float(computed['euro_rate'] or 0)
                label = 'EUR'
            else:
                unit = to_float(computed['thai_baht'] or 0)
                label = 'THB'
            amount = unit * ci.quantity if unit else 0.0
            total_amount += amount
            # extra fields for dynamic columns
            try:
                image_url = ''
                if p.images.exists():
                    first_image = p.images.first()
                    image_url = request.build_absolute_uri(first_image.image.url)
            except Exception:
                image_url = ''
            pairing_values = []
            try:
                pairing_values = [ps.pair_value for ps in p.pairing_set.all()]
            except Exception:
                pairing_values = []
            items_for_print.append({
                'code': f"{p.child_code}",
                'description': p.description or '-',
                'location': p.location or '-',
                'quantity': ci.quantity,
                'unit': unit,
                'amount': amount,
                'currency_label': label,
                'image_url': image_url,
                'weight': to_float(p.weight or 0),
                'kpo': p.kpo or '-',
                'bi': _compute_bi_value(p) or '-',
                'note': (ci.note if ci.note else (p.note or '')),
                'pairing_set': ", ".join(pairing_values) if pairing_values else '-',
                'thb': to_float(computed['thai_baht'] or 0),
                'usd': to_float(computed['usd_rate'] or 0),
                'eur': to_float(computed['euro_rate'] or 0),
            })

        # Calculate totals and annotate items with computed fields
        total_quantity = sum(item.quantity for item in cart_items)
        total_weight = sum(
            float(item.product.weight or 0) * item.quantity 
            for item in cart_items
        )
        for item in cart_items:
            try:
                item_computed = _get_computed_prices(item.product)
                item.amount_thb = to_float(item_computed['thai_baht']) * item.quantity
            except Exception:
                item.amount_thb = 0.0
        shipping = float(cart.shipping_amount or 0)
        deposit = float(cart.deposit_amount or 0)
        grand_total = total_amount + shipping - deposit

        # Dynamic category summary based on actual cart product tags
        categories = {}
        for item in cart_items:
            tag_name = (item.product.tag.name if item.product.tag else '') or 'Other'
            categories[tag_name] = categories.get(tag_name, 0) + item.quantity
        
        context = {
            'customer': customer,
            'cart': cart,
            'items_for_print': items_for_print,
            'total_items': cart_items.count(),
            'total_quantity': total_quantity,
            'total_weight': total_weight,
            'total_amount': total_amount,
            'shipping': shipping,
            'deposit': deposit,
            'grand_total': grand_total,
            'print_date': timezone.now(),
            'selected_columns': selected_columns,
            'categories': categories,
            'currency_label': currency,
            'salesperson_name': (getattr(cart, 'sales_person', None) or getattr(request.user, 'get_full_name', lambda: '')() or getattr(request.user, 'username', '')),
            'doc_ref': str(getattr(cart, 'doc_ref', None) or getattr(cart, 'id', '')),
            'customer_code': str(getattr(cart, 'customer_code', None) or getattr(customer, 'id', '')),
            'address_display': (cart.address_override or getattr(customer, 'address', '') or ''),
            'special_notes': cart.special_notes or '',
        }
        
        return render(request, 'customer_cart_print.html', context)
        
    except Exception as e:
        messages.error(request, f'Error preparing cart for printing: {str(e)}')
        return redirect('customer_cart', customer_id=customer_id)
    
    return JsonResponse({'success': False, 'message': 'Method not allowed'})

@login_required
@perm_required('cart')
def customer_cart_view(request, customer_id):
    """View customer's cart details"""
    customer = get_object_or_404(Customer, id=customer_id)
    
    # Get or create active cart for customer
    cart, created = Cart.objects.get_or_create(
        customer=customer,
        is_active=True,
        defaults={'customer': customer}
    )
    
    cart_items = cart.items.select_related('product').all()
    
    context = {
        'customer': customer,
        'cart': cart,
        'cart_items': cart_items,
        'products': Product.objects.prefetch_related('images').all().order_by('parent_code', 'child_code'),
    }
    return render(request, 'customer_cart.html', context)

@csrf_exempt
@login_required
@perm_required('cart_edit')
def import_customer_cart_excel(request, customer_id):
    """Import products (code, quantity) from Excel into customer's active cart.
    Expected columns: Product Code, Quantity. Non-existent products are ignored.
    """
    customer = get_object_or_404(Customer, id=customer_id)
    if request.method == 'POST':
        file = request.FILES.get('file')
        if not file:
            return JsonResponse({'success': False, 'message': 'No file uploaded'})
        try:
            wb = load_workbook(filename=file, data_only=True)
            ws = wb.active

            # Detect header row and indices for code/quantity
            start_row = 1
            idx_code = 0
            idx_qty = 1
            first_row_values = [cell.value for cell in ws[1]] if ws.max_row >= 1 else []
            if first_row_values:
                header_lower = [str(v).strip().lower() if v is not None else '' for v in first_row_values]
                def find_index(options, default=None):
                    for opt in options:
                        if opt in header_lower:
                            return header_lower.index(opt)
                    return default
                code_idx = find_index(['product code', 'code'])
                qty_idx = find_index(['quantity', 'qty'])
                headers_present = (code_idx is not None) or (qty_idx is not None)
                if code_idx is not None:
                    idx_code = code_idx
                if qty_idx is not None:
                    idx_qty = qty_idx
                if headers_present:
                    start_row = 2

            cart, _ = Cart.objects.get_or_create(customer=customer, is_active=True)
            added = updated = ignored = processed = 0

            for row in ws.iter_rows(min_row=start_row, values_only=True):
                if not row:
                    continue
                code_cell = row[idx_code] if len(row) > idx_code else None
                qty_cell = row[idx_qty] if len(row) > idx_qty else 1
                if code_cell is None:
                    ignored += 1
                    continue
                code_str = str(code_cell).strip()
                try:
                    # Allow numeric cells like 2.0
                    qty = int(float(qty_cell)) if qty_cell is not None else 1
                except Exception:
                    qty = 1
                if not code_str or qty <= 0:
                    ignored += 1
                    continue

                # Find product by combined code or parent/child code
                product = None
                if '-' in code_str:
                    # Split on the LAST hyphen because parent_code may itself contain hyphens
                    try:
                        parent, child = code_str.rsplit('-', 1)
                        product = Product.objects.filter(parent_code=parent.strip(), child_code=child.strip()).first()
                    except ValueError:
                        product = None
                if not product:
                    product = Product.objects.filter(Q(child_code=code_str) | Q(parent_code=code_str)).first()
                if not product:
                    ignored += 1
                    continue

                cart_item, created = CartItem.objects.get_or_create(
                    cart=cart,
                    product=product,
                    defaults={'quantity': qty}
                )
                if created:
                    added += 1
                else:
                    cart_item.quantity += qty
                    cart_item.save()
                    updated += 1
                processed += 1

            return JsonResponse({
                'success': True,
                'message': f'Imported {processed} lines. Added: {added}, Updated: {updated}, Ignored: {ignored}.'
            })
        except Exception as e:
            return JsonResponse({'success': False, 'message': f'Import error: {str(e)}'})

    return JsonResponse({'success': False, 'message': 'Method not allowed'})

@csrf_exempt
@login_required
@perm_required('cart')
def cart_api(request, customer_id):
    """API for cart operations"""
    customer = get_object_or_404(Customer, id=customer_id)
    
    if request.method != 'GET' and not user_can(request.user, 'cart_edit'):
        return deny(request)
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            action = data.get('action')
            
            # Get or create active cart
            cart, created = Cart.objects.get_or_create(
                customer=customer,
                is_active=True,
                defaults={'customer': customer}
            )
            
            if action == 'add_item':
                product_id = data.get('product_id')
                quantity = int(data.get('quantity', 1))
                
                if not product_id:
                    return JsonResponse({'success': False, 'message': 'Product ID is required'})
                
                product = get_object_or_404(Product, id=product_id)
                
                cart_item, created = CartItem.objects.get_or_create(
                    cart=cart,
                    product=product,
                    defaults={'quantity': quantity}
                )
                
                if not created:
                    cart_item.quantity += quantity
                    cart_item.save()
                
                return JsonResponse({
                    'success': True,
                    'message': f'Added {quantity}x {product.child_code} to cart'
                })
            
            elif action == 'update_item':
                item_id = data.get('item_id')
                quantity = int(data.get('quantity', 1))
                
                if not item_id:
                    return JsonResponse({'success': False, 'message': 'Item ID is required'})
                
                cart_item = get_object_or_404(CartItem, id=item_id, cart=cart)
                if quantity <= 0:
                    cart_item.delete()
                    return JsonResponse({
                        'success': True,
                        'message': 'Item removed from cart'
                    })
                cart_item.quantity = quantity
                cart_item.save()
                
                return JsonResponse({
                    'success': True,
                    'message': 'Cart item updated successfully'
                })

            elif action == 'update_note':
                item_id = data.get('item_id')
                note = data.get('note', '')
                
                if not item_id:
                    return JsonResponse({'success': False, 'message': 'Item ID is required'})
                
                cart_item = get_object_or_404(CartItem, id=item_id, cart=cart)
                cart_item.note = note
                cart_item.save(update_fields=['note'])
                
                return JsonResponse({
                    'success': True,
                    'message': 'Note updated successfully'
                })
            
            elif action == 'remove_item':
                item_id = data.get('item_id')
                
                if not item_id:
                    return JsonResponse({'success': False, 'message': 'Item ID is required'})
                
                cart_item = get_object_or_404(CartItem, id=item_id, cart=cart)
                product_name = f"{cart_item.product.child_code}"
                cart_item.delete()
                
                return JsonResponse({
                    'success': True,
                    'message': f'Removed {product_name} from cart'
                })
            
            elif action == 'clear_cart':
                cart.items.all().delete()
                return JsonResponse({
                    'success': True,
                    'message': 'Cart cleared successfully'
                })
            
            elif action == 'update_cart_info':
                address_override = data.get('address_override')
                shipping_amount = data.get('shipping_amount')
                deposit_amount = data.get('deposit_amount')
                notes = data.get('notes')
                special_notes = data.get('special_notes')
                sales_person = data.get('sales_person')
                doc_ref = data.get('doc_ref')
                customer_code = data.get('customer_code')
                gross_weight = data.get('gross_weight')
                try:
                    cart.address_override = address_override
                    cart.shipping_amount = Decimal(str(shipping_amount or 0))
                    cart.deposit_amount = Decimal(str(deposit_amount or 0))
                    cart.notes = notes
                    cart.special_notes = special_notes
                    cart.sales_person = sales_person
                    cart.doc_ref = doc_ref
                    cart.customer_code = customer_code
                    cart.gross_weight = Decimal(str(gross_weight or 0))
                    cart.save()
                    return JsonResponse({'success': True, 'message': 'Cart details updated'})
                except Exception as e:
                    return JsonResponse({'success': False, 'message': str(e)})
            else:
                return JsonResponse({'success': False, 'message': 'Invalid action'})

        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'message': 'Invalid JSON data'})
        except Exception as e:
            return JsonResponse({'success': False, 'message': str(e)})

    elif request.method == 'GET':
        # Get cart details
        cart = Cart.objects.filter(customer=customer, is_active=True).first()
        if not cart:
            return JsonResponse({'cart_items': []})
        
        currency = (request.GET.get('currency') or '').upper()
        if currency not in ('THB', 'USD', 'EUR'):
            currency = ''  # empty means auto-preferred fallback

        cart_items = []
        for item in cart.items.select_related('product').all():
            product = item.product
            # Get computed prices for this product
            computed = _get_computed_prices(product)
            # Determine price based on requested currency or fallback
            price_val = None
            price_raw = None
            currency_note = None
            if currency == 'THB':
                currency_note = 'THB'
                price_raw = computed['thai_baht']
                price_val = _parse_price_to_decimal(computed['thai_baht'])
            elif currency == 'USD':
                currency_note = 'USD'
                price_raw = computed['usd_rate']
                price_val = _parse_price_to_decimal(computed['usd_rate'])
            elif currency == 'EUR':
                currency_note = 'EUR'
                price_raw = computed['euro_rate']
                price_val = _parse_price_to_decimal(computed['euro_rate'])
            else:
                if computed['thai_baht'] and computed['thai_baht'] != '0':
                    currency_note = 'THB'
                    price_raw = computed['thai_baht']
                    price_val = _parse_price_to_decimal(computed['thai_baht'])
                elif computed['usd_rate'] and computed['usd_rate'] != '0':
                    currency_note = 'USD'
                    price_raw = computed['usd_rate']
                    price_val = _parse_price_to_decimal(computed['usd_rate'])
                elif computed['euro_rate'] and computed['euro_rate'] != '0':
                    currency_note = 'EUR'
                    price_raw = computed['euro_rate']
                    price_val = _parse_price_to_decimal(computed['euro_rate'])

            amount_val = None
            if price_val is not None:
                try:
                    amount_val = price_val * Decimal(str(item.quantity))
                except Exception:
                    amount_val = None

            try:
                item_image_url = request.build_absolute_uri(product.images.first().image.url) if product.images.exists() else ''
            except Exception:
                item_image_url = ''
            pairing_values = ''
            try:
                pairing_values = ', '.join([ps.pair_value for ps in product.pairing_set.all()])
            except Exception:
                pairing_values = ''

            cart_items.append({
                'id': item.id,
                'product_id': product.id,
                'product_code': f"{product.child_code}",
                'product_location': product.location,
                'description': product.description or '',
                'weight': float(product.weight or 0),
                'kpo': product.kpo or '',
                'pairing_set': pairing_values,
                'bi': _compute_bi_value(product) or '',
                'image_url': item_image_url,
                'quantity': item.quantity,
                'price': float(price_val) if price_val is not None else None,
                'price_raw': (str(price_raw).strip() if price_raw is not None else None),
                'currency_note': currency_note,
                'amount': float(amount_val) if amount_val is not None else None,
                'added_at': item.added_at.strftime('%Y-%m-%d %H:%M'),
                'note': item.note if item.note else (product.note or ''),
            })
        
        cart_info = {
            'address_override': cart.address_override,
            'shipping_amount': float(cart.shipping_amount or 0),
            'deposit_amount': float(cart.deposit_amount or 0),
            'notes': cart.notes,
            'special_notes': cart.special_notes,
            'sales_person': getattr(cart, 'sales_person', None),
            'doc_ref': getattr(cart, 'doc_ref', None),
            'customer_code': getattr(cart, 'customer_code', None),
            'gross_weight': float(getattr(cart, 'gross_weight', 0) or 0),
        }
        return JsonResponse({'cart_items': cart_items, 'cart_info': cart_info})

    return JsonResponse({'success': False, 'message': 'Method not allowed'})


@csrf_exempt
def add_to_cart_api(request):
    """API endpoint for Android app to create customer and add product to cart"""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method allowed'}, status=405)
    
    try:
        data = json.loads(request.body)
        customer_name = data.get('customer_name', '').strip()
        product_code = data.get('product_code', '').strip()
        quantity = int(data.get('quantity', 1))
        
        # Validate input
        if not customer_name:
            return JsonResponse({'error': 'Customer name is required'}, status=400)
        
        if not product_code:
            return JsonResponse({'error': 'Product code is required'}, status=400)
        
        if quantity <= 0:
            return JsonResponse({'error': 'Quantity must be greater than 0'}, status=400)
        
        # Find the product by searching in both child_code and parent_code
        # This matches the logic used in single_product API
        product = Product.objects.filter(
            Q(child_code=product_code) | Q(parent_code=product_code)
        ).first()
        
        if not product:
            product = Product.objects.filter(
                Q(child_code__iexact=product_code) | Q(parent_code__iexact=product_code)
            ).first()
        if not product and '-' in product_code:
            try:
                parent_part, child_part = product_code.rsplit('-', 1)
                product = Product.objects.filter(parent_code__iexact=parent_part.strip(), child_code__iexact=child_part.strip()).first()
            except ValueError:
                product = None
        
        if not product:
            return JsonResponse({'error': 'Product not found'}, status=404)
        
        # Create or get customer
        customer, created = Customer.objects.get_or_create(
            name=customer_name,
            defaults={'created_at': timezone.now()}
        )
        
        # Get or create active cart for customer
        cart, cart_created = Cart.objects.get_or_create(
            customer=customer,
            is_active=True,
            defaults={'created_at': timezone.now()}
        )
        
        # Check if product already exists in cart
        cart_item, item_created = CartItem.objects.get_or_create(
            cart=cart,
            product=product,
            defaults={
                'quantity': quantity,
                'added_at': timezone.now()
            }
        )
        
        if not item_created:
            # Product already in cart, update quantity
            cart_item.quantity += quantity
            cart_item.save()
            message = f"Updated quantity for {product_code}. New quantity: {cart_item.quantity}"
        else:
            message = f"Added {quantity} x {product_code} to {customer_name}'s cart"
        
        return JsonResponse({
            'success': True,
            'message': message,
            'customer_id': customer.id,
            'customer_name': customer.name,
            'product_code': product_code,
            'quantity': cart_item.quantity,
            'cart_id': cart.id
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON data'}, status=400)
    except ValueError as e:
        return JsonResponse({'error': f'Invalid data: {str(e)}'}, status=400)
    except Exception as e:
        return JsonResponse({'error': f'Server error: {str(e)}'}, status=500)


@csrf_exempt
def customers_android_api(request):
    """API endpoint for Android app to get customer data without authentication"""
    if request.method == 'GET':
        customers = Customer.objects.all().order_by('name')
        customer_list = []
        for customer in customers:
            active_cart = customer.carts.filter(is_active=True).first()
            customer_list.append({
                'id': customer.id,
                'name': customer.name,
                'created_at': customer.created_at.strftime('%Y-%m-%d %H:%M'),
                'cart_count': customer.carts.count(),
                'active_cart_id': active_cart.id if active_cart else None,
                'active_cart_items': active_cart.get_total_items() if active_cart else 0,
            })
        return JsonResponse({'customers': customer_list})
    
    return JsonResponse({'error': 'Only GET method allowed'}, status=405)


@csrf_exempt
def customers_android_delete_api(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method allowed'}, status=405)
    try:
        data = json.loads(request.body)
        ids = data.get('ids', [])
        if not isinstance(ids, list) or not ids:
            return JsonResponse({'error': 'ids must be a non-empty list'}, status=400)
        deleted_ids = []
        for cid in ids:
            try:
                customer = Customer.objects.get(id=cid)
                customer.delete()
                deleted_ids.append(cid)
            except Customer.DoesNotExist:
                pass
        return JsonResponse({'success': True, 'deleted_ids': deleted_ids, 'deleted_count': len(deleted_ids)})
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON data'}, status=400)


@csrf_exempt
def customers_android_create_api(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method allowed'}, status=405)
    try:
        data = json.loads(request.body)
        name = data.get('name', '').strip()
        if not name:
            return JsonResponse({'error': 'Name is required'}, status=400)
        customer = Customer.objects.create(
            name=name,
            company_name=(data.get('company_name') or '').strip() or None,
            contact_person=(data.get('contact_person') or '').strip() or None,
            address=(data.get('address') or '').strip() or None,
            phone=(data.get('phone') or '').strip() or None,
            email=(data.get('email') or '').strip() or None,
        )
        return JsonResponse({'success': True, 'customer': {'id': customer.id, 'name': customer.name}})
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON data'}, status=400)


@csrf_exempt
def customers_android_lock_api(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method allowed'}, status=405)
    try:
        data = json.loads(request.body)
        ids = data.get('ids', [])
        lock = bool(data.get('lock', True))
        if not isinstance(ids, list) or not ids:
            return JsonResponse({'error': 'ids must be a non-empty list'}, status=400)
        updated = 0
        for cid in ids:
            try:
                customer = Customer.objects.get(id=cid)
                customer.locked = lock
                customer.save(update_fields=['locked'])
                updated += 1
            except Customer.DoesNotExist:
                continue
        return JsonResponse({'success': True, 'updated_count': updated, 'locked': lock})
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON data'}, status=400)


@csrf_exempt
def add_to_cart_bulk_android(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method allowed'}, status=405)
    try:
        data = json.loads(request.body)
        product_code = data.get('product_code', '').strip()
        quantity = int(data.get('quantity', 1))
        for_locked = bool(data.get('for_locked', True))
        mode = data.get('mode', 'add')  # "add" (default) or "set"
        if not product_code:
            return JsonResponse({'error': 'Product code is required'}, status=400)
        if quantity <= 0 and mode != 'set':
            return JsonResponse({'error': 'Quantity must be greater than 0'}, status=400)

        product = Product.objects.filter(Q(child_code=product_code) | Q(parent_code=product_code)).first()
        if not product:
            product = Product.objects.filter(Q(child_code__iexact=product_code) | Q(parent_code__iexact=product_code)).first()
        if not product and '-' in product_code:
            try:
                parent_part, child_part = product_code.rsplit('-', 1)
                product = Product.objects.filter(parent_code__iexact=parent_part.strip(), child_code__iexact=child_part.strip()).first()
            except ValueError:
                product = None
        if not product:
            return JsonResponse({'error': 'Product not found'}, status=404)

        if for_locked:
            customers = Customer.objects.filter(locked=True)
        else:
            customer_ids = data.get('customer_ids', [])
            customers = Customer.objects.filter(id__in=customer_ids)

        # quantity == 0 in set mode: remove the product from all matching carts
        if quantity <= 0 and mode == 'set':
            removed_count = 0
            for customer in customers:
                cart, _ = Cart.objects.get_or_create(customer=customer, is_active=True)
                deleted, _ = CartItem.objects.filter(cart=cart, product=product).delete()
                removed_count += 1 if deleted else 0
            return JsonResponse({'success': True, 'affected_customers': removed_count, 'removed': True})

        added_count = 0
        for customer in customers:
            cart, _ = Cart.objects.get_or_create(customer=customer, is_active=True)
            item, created = CartItem.objects.get_or_create(cart=cart, product=product, defaults={'quantity': quantity})
            if not created:
                if mode == 'set':
                    item.quantity = quantity
                else:
                    item.quantity += quantity
                item.save(update_fields=['quantity'])
            added_count += 1

        return JsonResponse({'success': True, 'affected_customers': added_count})
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON data'}, status=400)


@csrf_exempt
def product_quantity_in_locked_carts(request):
    """Return the total quantity of a given product across all locked customers'
    active carts. Used by the Android Add-to-Cart screen to show the current
    total and switch between add/update (set) modes."""
    if request.method != 'GET':
        return JsonResponse({'error': 'Only GET method allowed'}, status=405)
    product_code = (request.GET.get('product_code') or '').strip()
    if not product_code:
        return JsonResponse({'error': 'Product code is required'}, status=400)

    product = Product.objects.filter(Q(child_code__iexact=product_code) | Q(parent_code__iexact=product_code)).first()
    if not product and '-' in product_code:
        try:
            parent_part, child_part = product_code.rsplit('-', 1)
            product = Product.objects.filter(parent_code__iexact=parent_part.strip(), child_code__iexact=child_part.strip()).first()
        except ValueError:
            product = None
    if not product:
        return JsonResponse({'error': 'Product not found'}, status=404)

    locked_customers = Customer.objects.filter(locked=True)
    total_qty = CartItem.objects.filter(
        cart__customer__in=locked_customers,
        cart__is_active=True,
        product=product
    ).aggregate(total=Sum('quantity'))['total'] or 0
    customer_count = CartItem.objects.filter(
        cart__customer__in=locked_customers,
        cart__is_active=True,
        product=product
    ).values('cart__customer').distinct().count()

    return JsonResponse({
        'total_quantity': total_qty,
        'customer_count': customer_count,
        'locked_customer_count': locked_customers.count(),
    })


@csrf_exempt
def cart_android_api(request, customer_id):
    """API endpoint for Android app to get and modify customer cart data without authentication"""
    try:
        customer = get_object_or_404(Customer, id=customer_id)
        cart = customer.carts.filter(is_active=True).first()
        if not cart:
            cart = Cart.objects.create(customer=customer, is_active=True)

        if request.method == 'GET':
            cart_items = []
            for item in cart.items.all():
                image_url = ''
                if item.product.images.exists():
                    first_image = item.product.images.first()
                    image_url = request.build_absolute_uri(first_image.image.url)

                computed = _get_computed_prices(item.product)
                product_full = {
                    'id': item.product.id,
                    'image': item.product.images.first().image.url if item.product.images.exists() else '',
                    'parent_code': item.product.parent_code,
                    'child_code': item.product.child_code,
                    'location': item.product.location,
                    'stock': item.product.stock,
                    'kpo': item.product.kpo,
                    'weight': item.product.weight,
                    'thai_baht': computed['thai_baht'],
                    'thai_baht_value': computed['thai_baht_value'],
                    'usd_rate': computed['usd_rate'],
                    'usd_rate_value': computed['usd_rate_value'],
                    'euro_rate': computed['euro_rate'],
                    'euro_rate_value': computed['euro_rate_value'],
                    'bi': _compute_bi_value(item.product),
                    'note': item.product.note,
                    'description': item.product.description,
                    'tag': item.product.tag.name if item.product.tag else '',
                    'pairing_set': [ps.id for ps in item.product.pairing_set.all()],
                    'image_count': item.product.images.count(),
                }

                price_dec = _parse_price_to_decimal(computed['thai_baht'])
                price_num = float(price_dec) if price_dec is not None else 0.0
                cart_items.append({
                    'id': item.id,
                    'product_id': item.product.id,
                    'product_code': f"{item.product.child_code}",
                    'product_name': f"{item.product.child_code}",
                    'product_location': item.product.location or '',
                    'price': price_num,
                    'price_raw': computed['thai_baht'],
                    'image_url': image_url,
                    'quantity': item.quantity,
                    'added_at': item.added_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'note': item.note if item.note else (item.product.note or ''),
                    'product': product_full,
                })

            cart_info = {
                'address_override': cart.address_override,
                'shipping_amount': float(cart.shipping_amount or 0),
                'deposit_amount': float(cart.deposit_amount or 0),
                'notes': cart.notes,
                'special_notes': cart.special_notes,
                'sales_person': getattr(cart, 'sales_person', None),
                'doc_ref': getattr(cart, 'doc_ref', None),
                'customer_code': getattr(cart, 'customer_code', None),
                'gross_weight': float(getattr(cart, 'gross_weight', 0) or 0),
            }
            customer_info = {
                'name': customer.name or '',
                'company_name': getattr(customer, 'company_name', '') or '',
                'contact_person': getattr(customer, 'contact_person', '') or '',
                'address': getattr(customer, 'address', '') or '',
                'phone': getattr(customer, 'phone', '') or '',
                'email': getattr(customer, 'email', '') or '',
            }
            return JsonResponse({'cart_items': cart_items, 'cart_info': cart_info, 'customer_info': customer_info})

        elif request.method == 'POST':
            try:
                data = json.loads(request.body)
            except json.JSONDecodeError:
                return JsonResponse({'error': 'Invalid JSON data'}, status=400)

            action = data.get('action')
            if action == 'bulk_update':
                updates = data.get('updates', [])
                updated_count = 0
                for upd in updates:
                    item_id = upd.get('item_id')
                    quantity = int(upd.get('quantity', 1))
                    if not item_id:
                        continue
                    try:
                        cart_item = CartItem.objects.get(id=item_id, cart=cart)
                        if quantity <= 0:
                            cart_item.delete()
                        else:
                            cart_item.quantity = quantity
                            cart_item.save()
                        updated_count += 1
                    except CartItem.DoesNotExist:
                        continue

                return JsonResponse({'success': True, 'updated_count': updated_count})

            elif action == 'bulk_remove':
                item_ids = data.get('item_ids', [])
                deleted_count = 0
                for item_id in item_ids:
                    try:
                        cart_item = CartItem.objects.get(id=item_id, cart=cart)
                        cart_item.delete()
                        deleted_count += 1
                    except CartItem.DoesNotExist:
                        continue

                return JsonResponse({'success': True, 'deleted_count': deleted_count})

            elif action == 'update_item':
                item_id = data.get('item_id')
                quantity = int(data.get('quantity', 1))
                if not item_id:
                    return JsonResponse({'success': False, 'message': 'Item ID is required'})
                try:
                    cart_item = CartItem.objects.get(id=item_id, cart=cart)
                    if quantity <= 0:
                        cart_item.delete()
                        return JsonResponse({'success': True, 'message': 'Item removed'})
                    cart_item.quantity = quantity
                    cart_item.save()
                    return JsonResponse({'success': True, 'message': 'Cart item updated successfully'})
                except CartItem.DoesNotExist:
                    return JsonResponse({'success': False, 'message': 'Cart item not found'})

            elif action == 'remove_item':
                item_id = data.get('item_id')
                if not item_id:
                    return JsonResponse({'success': False, 'message': 'Item ID is required'})
                try:
                    cart_item = CartItem.objects.get(id=item_id, cart=cart)
                    cart_item.delete()
                    return JsonResponse({'success': True, 'message': 'Cart item removed'})
                except CartItem.DoesNotExist:
                    return JsonResponse({'success': False, 'message': 'Cart item not found'})

            elif action == 'update_cart_info':
                address_override = data.get('address_override')
                shipping_amount = data.get('shipping_amount')
                deposit_amount = data.get('deposit_amount')
                notes = data.get('notes')
                special_notes = data.get('special_notes')
                sales_person = data.get('sales_person')
                doc_ref = data.get('doc_ref')
                customer_code = data.get('customer_code')
                gross_weight = data.get('gross_weight')
                try:
                    cart.address_override = address_override
                    cart.shipping_amount = Decimal(str(shipping_amount or 0))
                    cart.deposit_amount = Decimal(str(deposit_amount or 0))
                    cart.notes = notes
                    cart.special_notes = special_notes
                    cart.sales_person = sales_person
                    cart.doc_ref = doc_ref
                    cart.customer_code = customer_code
                    cart.gross_weight = Decimal(str(gross_weight or 0))
                    cart.save()
                    return JsonResponse({'success': True, 'message': 'Cart details updated'})
                except Exception as e:
                    return JsonResponse({'success': False, 'message': str(e)})
            else:
                return JsonResponse({'success': False, 'message': 'Invalid action'})

        return JsonResponse({'error': 'Method not allowed'}, status=405)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
@csrf_exempt
def customers_android_locked_count_api(request):
    if request.method != 'GET':
        return JsonResponse({'error': 'Only GET method allowed'}, status=405)
    count = Customer.objects.filter(locked=True).count()
    return JsonResponse({'locked_count': count})


@csrf_exempt
def customers_android_locked_ids_api(request):
    if request.method != 'GET':
        return JsonResponse({'error': 'Only GET method allowed'}, status=405)
    ids = list(Customer.objects.filter(locked=True).values_list('id', flat=True))
    return JsonResponse({'locked_ids': ids})


@login_required
@perm_required('products_price')
def price_setting_api(request):
    ps = PriceSetting.get_singleton()
    if request.method == 'GET':
        return JsonResponse({
            'silver_price': float(ps.silver_price),
            'usd_rate': float(ps.usd_rate),
            'euro_rate': float(ps.euro_rate),
            'updated_at': ps.updated_at.isoformat() if ps.updated_at else None,
        })
    elif request.method == 'POST':
        try:
            data = json.loads(request.body)
            silver = data.get('silver_price')
            usd = data.get('usd_rate')
            eur = data.get('euro_rate')
            if silver is not None:
                ps.silver_price = Decimal(str(silver))
            if usd is not None:
                ps.usd_rate = Decimal(str(usd))
            if eur is not None:
                ps.euro_rate = Decimal(str(eur))
            ps.save()
            return JsonResponse({
                'success': True,
                'message': 'Price settings updated',
                'silver_price': float(ps.silver_price),
                'usd_rate': float(ps.usd_rate),
                'euro_rate': float(ps.euro_rate),
            })
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'message': 'Invalid JSON data'}, status=400)
        except Exception as e:
            return JsonResponse({'success': False, 'message': str(e)}, status=500)
    return JsonResponse({'error': 'Method not allowed'}, status=405)


# ==============================================================
# Quotation Management
# ==============================================================

QUOTATION_CURRENCIES = ('THB', 'USD', 'EUR')
_QUOTATION_CURRENCY_FIELD = {'THB': 'thai_baht', 'USD': 'usd_rate', 'EUR': 'euro_rate'}


def _next_quotation_number():
    """QT-<year>-<seq> — sequence resets each year."""
    prefix = f"QT-{timezone.now().year}-"
    last = (Quotation.objects
            .filter(quotation_number__startswith=prefix)
            .order_by('-quotation_number')
            .values_list('quotation_number', flat=True)
            .first())
    seq = 1
    if last:
        try:
            seq = int(last.rsplit('-', 1)[-1]) + 1
        except (ValueError, IndexError):
            seq = Quotation.objects.filter(quotation_number__startswith=prefix).count() + 1
    return f"{prefix}{seq:04d}"


def _quotation_unit_price(product, currency, price_setting=None):
    """Snapshot unit price for a product in the quotation's currency."""
    field = _QUOTATION_CURRENCY_FIELD.get(currency, 'thai_baht')
    computed = _get_computed_prices(product, price_setting)
    return _parse_price_to_decimal(computed.get(field)) or Decimal('0')


def _recalc_quotation_total(quotation):
    total = sum(
        (item.line_amount or Decimal('0')) for item in quotation.items.all()
    )
    quotation.total_amount = Decimal(total).quantize(Decimal('0.01'))
    quotation.save(update_fields=['total_amount', 'updated_at'])


def _serialize_quotation_item(item):
    product = item.product
    try:
        image_url = product.images.first().image.url if product and product.images.exists() else ''
    except Exception:
        image_url = ''
    return {
        'id': item.id,
        'product_id': product.id if product else None,
        'product_code': item.product_code or (product.child_code if product else ''),
        'description': item.description or (product.description if product else '') or '',
        'image_url': image_url,
        'location': product.location if product else '',
        'quantity': item.quantity,
        'unit_price': float(item.unit_price or 0),
        'line_amount': float(item.line_amount or 0),
        'note': item.note or '',
    }


def _serialize_quotation(q):
    return {
        'id': q.id,
        'quotation_number': q.quotation_number or '',
        'customer_id': q.customer_id,
        'customer_name': q.customer.name if q.customer else '',
        'quotation_date': q.quotation_date.strftime('%Y-%m-%d') if q.quotation_date else '',
        'validity_date': q.validity_date.strftime('%Y-%m-%d') if q.validity_date else '',
        'status': q.status,
        'status_display': q.get_status_display(),
        'currency': q.currency,
        'total_amount': float(q.total_amount or 0),
        'item_count': q.items.count(),
        'created_by': (q.created_by.get_full_name() or q.created_by.username) if q.created_by else '',
        'created_at': q.created_at.strftime('%Y-%m-%d %H:%M') if q.created_at else '',
    }


@csrf_exempt
@login_required
@perm_required('quotations_create')
def create_quotation_from_cart(request, customer_id):
    """Create a quotation snapshot from the customer's active cart."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)
    customer = get_object_or_404(Customer, id=customer_id)
    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'message': 'Invalid JSON data'}, status=400)

    currency = (data.get('currency') or 'THB').upper()
    if currency not in QUOTATION_CURRENCIES:
        currency = 'THB'

    cart = Cart.objects.filter(customer=customer, is_active=True).first()
    if not cart:
        return JsonResponse({'success': False, 'message': 'No active cart for this customer'})
    cart_items = list(cart.items.select_related('product').all())
    if not cart_items:
        return JsonResponse({'success': False, 'message': 'Cart is empty — add items before creating a quotation'})

    price_setting = PriceSetting.get_singleton()
    quotation = None
    for attempt in range(5):
        try:
            with transaction.atomic():
                quotation = Quotation.objects.create(
                    quotation_number=_next_quotation_number(),
                    customer=customer,
                    cart=cart,
                    quotation_date=timezone.now().date(),
                    status=Quotation.Status.DRAFT,
                    currency=currency,
                    created_by=request.user,
                )
                total = Decimal('0')
                for ci in cart_items:
                    unit = _quotation_unit_price(ci.product, currency, price_setting)
                    line = (unit * ci.quantity).quantize(Decimal('0.01'))
                    QuotationItem.objects.create(
                        quotation=quotation,
                        product=ci.product,
                        source_cart_item=ci,
                        product_code=ci.product.child_code or '',
                        description=ci.product.description or '',
                        quantity=ci.quantity,
                        unit_price=unit.quantize(Decimal('0.01')),
                        line_amount=line,
                        note=ci.note or '',
                    )
                    total += line
                quotation.total_amount = total.quantize(Decimal('0.01'))
                quotation.save(update_fields=['total_amount', 'updated_at'])
            break
        except IntegrityError:
            if attempt == 4:
                return JsonResponse({'success': False, 'message': 'Could not allocate a quotation number, please retry'}, status=500)

    return JsonResponse({
        'success': True,
        'message': f'Quotation {quotation.quotation_number} created',
        'quotation_id': quotation.id,
        'quotation_number': quotation.quotation_number,
        'redirect_url': f'/quotations/{quotation.id}/',
    })


@login_required
@perm_required('quotations')
def quotation_list_view(request):
    """Quotations dashboard page"""
    return render(request, 'quotations.html', {'statuses': Quotation.Status.choices})


@login_required
@perm_required('quotations')
def quotation_list_api(request):
    """Filtered quotation list for the dashboard table."""
    qs = Quotation.objects.select_related('customer', 'created_by').prefetch_related('items')
    q = (request.GET.get('q') or '').strip()
    status = (request.GET.get('status') or '').strip()
    date_from = (request.GET.get('date_from') or '').strip()
    date_to = (request.GET.get('date_to') or '').strip()
    if q:
        qs = qs.filter(Q(quotation_number__icontains=q) | Q(customer__name__icontains=q))
    if status:
        qs = qs.filter(status=status)
    if date_from:
        qs = qs.filter(quotation_date__gte=date_from)
    if date_to:
        qs = qs.filter(quotation_date__lte=date_to)
    return JsonResponse({'quotations': [_serialize_quotation(q) for q in qs]})


@login_required
@perm_required('quotations')
def quotation_detail_view(request, quotation_id):
    """Quotation detail/edit page"""
    quotation = get_object_or_404(
        Quotation.objects.select_related('customer', 'cart', 'created_by'),
        id=quotation_id,
    )
    items = quotation.items.select_related('product').prefetch_related('product__images')
    documents = quotation.documents.select_related('uploaded_by').all()
    badge_class = {'draft': 'secondary', 'sent': 'info', 'accepted': 'success', 'rejected': 'danger'}.get(quotation.status, 'secondary')

    # Follow-ups: pending first (soonest scheduled first), then completed
    now = timezone.now()
    follow_ups = list(quotation.follow_ups.select_related('created_by').all())
    follow_ups.sort(key=lambda f: (f.is_done, f.follow_up_at or timezone.datetime.max.replace(tzinfo=now.tzinfo)))
    for fu in follow_ups:
        fu.state = _follow_up_state(fu, now)
    next_follow_up = (quotation.follow_ups
                      .filter(is_done=False, follow_up_at__isnull=False)
                      .order_by('follow_up_at')
                      .select_related('created_by')
                      .first())
    last_follow_up = (quotation.follow_ups
                      .filter(is_done=True)
                      .order_by('-completed_at')
                      .select_related('created_by')
                      .first())
    if next_follow_up:
        next_follow_up.state = _follow_up_state(next_follow_up, now)

    context = {
        'quotation': quotation,
        'items': items,
        'documents': documents,
        'badge_class': badge_class,
        'customer': quotation.customer,
        'products': Product.objects.prefetch_related('images').all().order_by('parent_code', 'child_code'),
        'statuses': Quotation.Status.choices,
        'currencies': QUOTATION_CURRENCIES,
        'follow_ups': follow_ups,
        'follow_ups_json': [_serialize_follow_up(fu) for fu in follow_ups],
        'next_follow_up': next_follow_up,
        'last_follow_up': last_follow_up,
        'channels': QuotationFollowUp.Channel.choices,
        'existing_po': quotation.purchase_orders.exclude(status=PurchaseOrder.Status.CANCELLED).first(),
        'now': now,
    }
    return render(request, 'quotation_detail.html', context)


@csrf_exempt
@login_required
@perm_required('quotations')
def quotation_api(request, quotation_id):
    """API for quotation update/status/items/documents/delete"""
    quotation = get_object_or_404(Quotation, id=quotation_id)

    # Multipart upload (documents) arrives as form POST, not JSON
    if request.method == 'POST' and request.POST.get('action') == 'upload_document':
        if not user_can(request.user, 'quotations_edit'):
            return deny(request)
        upload = request.FILES.get('file')
        if not upload:
            return JsonResponse({'success': False, 'message': 'No file provided'})
        doc = QuotationDocument.objects.create(
            quotation=quotation,
            file=upload,
            name=(request.POST.get('name') or upload.name or '')[:255],
            uploaded_by=request.user,
        )
        return JsonResponse({'success': True, 'message': 'Document uploaded', 'document_id': doc.id})

    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'message': 'Invalid JSON data'}, status=400)

    action = data.get('action')
    required_perm = 'quotations_delete' if action == 'delete' else 'quotations_edit'
    if not user_can(request.user, required_perm):
        return deny(request)
    try:
        if action == 'update':
            for f in ('quotation_date', 'validity_date'):
                raw = (data.get(f) or '').strip()
                parsed = parse_date(raw) if raw else None
                if raw and parsed is None:
                    return JsonResponse({'success': False, 'message': f'Invalid {f.replace("_", " ")} — use YYYY-MM-DD'})
                setattr(quotation, f, parsed)
            currency = (data.get('currency') or quotation.currency or 'THB').upper()
            if currency not in QUOTATION_CURRENCIES:
                return JsonResponse({'success': False, 'message': 'Invalid currency'})
            quotation.currency = currency
            quotation.terms_conditions = data.get('terms_conditions', quotation.terms_conditions)
            quotation.remarks = data.get('remarks', quotation.remarks)
            quotation.save()
            return JsonResponse({'success': True, 'message': 'Quotation updated'})

        elif action == 'update_status':
            status = data.get('status')
            valid = {c[0] for c in Quotation.Status.choices}
            if status not in valid:
                return JsonResponse({'success': False, 'message': 'Invalid status'})
            quotation.status = status
            quotation.save(update_fields=['status', 'updated_at'])
            return JsonResponse({'success': True, 'message': f'Status updated to {dict(Quotation.Status.choices)[status]}'})

        elif action == 'update_item':
            item = get_object_or_404(QuotationItem, id=data.get('item_id'), quotation=quotation)
            quantity = int(data.get('quantity', item.quantity))
            if quantity <= 0:
                return JsonResponse({'success': False, 'message': 'Quantity must be at least 1'})
            unit_price = _parse_price_to_decimal(data.get('unit_price'))
            if unit_price is None or unit_price < 0:
                return JsonResponse({'success': False, 'message': 'Invalid unit price'})
            item.quantity = quantity
            item.unit_price = unit_price.quantize(Decimal('0.01'))
            item.line_amount = (item.unit_price * quantity).quantize(Decimal('0.01'))
            if 'note' in data:
                item.note = data.get('note') or ''
            item.save()
            _recalc_quotation_total(quotation)
            return JsonResponse({
                'success': True, 'message': 'Item updated',
                'item': _serialize_quotation_item(item),
                'total_amount': float(quotation.total_amount),
            })

        elif action == 'add_item':
            product_id = data.get('product_id')
            quantity = int(data.get('quantity', 1))
            if not product_id:
                return JsonResponse({'success': False, 'message': 'Product ID is required'})
            if quantity <= 0:
                return JsonResponse({'success': False, 'message': 'Quantity must be at least 1'})
            product = get_object_or_404(Product, id=product_id)
            existing = quotation.items.filter(product=product).first()
            if existing:
                existing.quantity += quantity
                existing.line_amount = (existing.unit_price * existing.quantity).quantize(Decimal('0.01'))
                existing.save(update_fields=['quantity', 'line_amount'])
                item = existing
            else:
                unit = _quotation_unit_price(product, quotation.currency)
                item = QuotationItem.objects.create(
                    quotation=quotation,
                    product=product,
                    product_code=product.child_code or '',
                    description=product.description or '',
                    quantity=quantity,
                    unit_price=unit.quantize(Decimal('0.01')),
                    line_amount=(unit * quantity).quantize(Decimal('0.01')),
                )
            _recalc_quotation_total(quotation)
            return JsonResponse({
                'success': True, 'message': f'Added {quantity}x {product.child_code}',
                'item': _serialize_quotation_item(item),
                'total_amount': float(quotation.total_amount),
            })

        elif action == 'remove_item':
            item = get_object_or_404(QuotationItem, id=data.get('item_id'), quotation=quotation)
            code = item.product_code
            item.delete()
            _recalc_quotation_total(quotation)
            return JsonResponse({'success': True, 'message': f'Removed {code}', 'total_amount': float(quotation.total_amount)})

        elif action == 'delete_document':
            doc = get_object_or_404(QuotationDocument, id=data.get('document_id'), quotation=quotation)
            try:
                if doc.file and doc.file.name and doc.file.storage.exists(doc.file.name):
                    doc.file.storage.delete(doc.file.name)
            except Exception:
                pass
            doc.delete()
            return JsonResponse({'success': True, 'message': 'Document deleted'})

        elif action == 'delete':
            number = quotation.quotation_number
            quotation.delete()
            return JsonResponse({'success': True, 'message': f'Quotation {number} deleted'})

        return JsonResponse({'success': False, 'message': 'Invalid action'})
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)


@login_required
@perm_required('quotations')
def quotation_print_view(request, quotation_id):
    """Render a quotation for printing / save-as-PDF"""
    try:
        quotation = get_object_or_404(
            Quotation.objects.select_related('customer', 'cart', 'created_by'),
            id=quotation_id,
        )
        items = quotation.items.select_related('product').prefetch_related('product__images')
        customer = quotation.customer

        items_for_print = []
        for it in items:
            p = it.product
            image_url = ''
            try:
                if p and p.images.exists():
                    image_url = request.build_absolute_uri(p.images.first().image.url)
            except Exception:
                image_url = ''
            items_for_print.append({
                'code': it.product_code or (p.child_code if p else '-'),
                'description': it.description or (p.description if p else '') or '-',
                'quantity': it.quantity,
                'unit': float(it.unit_price or 0),
                'amount': float(it.line_amount or 0),
                'note': it.note or (p.note if p else '') or '',
                'image_url': image_url,
            })

        salesperson = ''
        if quotation.created_by:
            salesperson = quotation.created_by.get_full_name() or quotation.created_by.username
        address_display = ''
        if quotation.cart and quotation.cart.address_override:
            address_display = quotation.cart.address_override
        elif customer and customer.address:
            address_display = customer.address

        context = {
            'quotation': quotation,
            'customer': customer,
            'items_for_print': items_for_print,
            'total_amount': float(quotation.total_amount or 0),
            'total_quantity': sum(i['quantity'] for i in items_for_print),
            'total_items': len(items_for_print),
            'currency_label': quotation.currency or 'THB',
            'print_date': timezone.now(),
            'salesperson_name': salesperson,
            'doc_ref': getattr(quotation.cart, 'doc_ref', None) or '',
            'customer_code': getattr(quotation.cart, 'customer_code', None) or '',
            'address_display': address_display,
        }
        return render(request, 'quotation_print.html', context)
    except Exception as e:
        messages.error(request, f'Error preparing quotation for printing: {str(e)}')
        return redirect('quotation_detail', quotation_id=quotation_id)


# ==============================================================
# Quotation Follow-Up Management
# ==============================================================

def _follow_up_state(fu, now=None):
    """Computed state: completed / overdue / upcoming."""
    now = now or timezone.now()
    if fu.is_done:
        return 'completed'
    if fu.follow_up_at and fu.follow_up_at < now:
        return 'overdue'
    return 'upcoming'


def _serialize_follow_up(fu):
    proof_url = ''
    proof_name = ''
    try:
        if fu.proof and fu.proof.name:
            proof_url = fu.proof.url
            proof_name = fu.proof.name.rsplit('/', 1)[-1]
    except Exception:
        pass
    return {
        'id': fu.id,
        'follow_up_at': fu.follow_up_at.strftime('%Y-%m-%d %H:%M') if fu.follow_up_at else '',
        'follow_up_at_input': fu.follow_up_at.strftime('%Y-%m-%dT%H:%M') if fu.follow_up_at else '',
        'channel': fu.channel,
        'channel_display': fu.get_channel_display(),
        'notes': fu.notes or '',
        'is_done': fu.is_done,
        'completed_at': fu.completed_at.strftime('%Y-%m-%d %H:%M') if fu.completed_at else '',
        'state': _follow_up_state(fu),
        'proof_url': proof_url,
        'proof_name': proof_name,
        'created_by': (fu.created_by.get_full_name() or fu.created_by.username) if fu.created_by else '',
        'created_at': fu.created_at.strftime('%Y-%m-%d %H:%M') if fu.created_at else '',
    }


def _follow_up_payload(quotation):
    """Full serialized list + next/last summary for in-place UI refresh."""
    follow_ups = [ _serialize_follow_up(fu) for fu in
        sorted(
            quotation.follow_ups.select_related('created_by').all(),
            key=lambda f: (f.is_done, f.follow_up_at or timezone.datetime.max.replace(tzinfo=timezone.get_current_timezone())),
        ) ]
    next_fu = (quotation.follow_ups
               .filter(is_done=False, follow_up_at__isnull=False)
               .order_by('follow_up_at')
               .select_related('created_by')
               .first())
    last_fu = (quotation.follow_ups
               .filter(is_done=True)
               .order_by('-completed_at')
               .select_related('created_by')
               .first())
    return {
        'follow_ups': follow_ups,
        'summary': {
            'next': _serialize_follow_up(next_fu) if next_fu else None,
            'last': _serialize_follow_up(last_fu) if last_fu else None,
        },
    }


def _parse_follow_up_at(raw):
    """Parse a datetime-local / ISO value into an aware datetime, or None."""
    raw = (raw or '').strip()
    if not raw:
        return None
    parsed = parse_datetime(raw)
    if parsed is None:
        try:
            parsed = timezone.datetime.strptime(raw, '%Y-%m-%d %H:%M')
        except (ValueError, TypeError):
            return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _read_follow_up_fields(request):
    """Unified field extraction from JSON body or multipart POST."""
    if request.content_type and 'multipart' in request.content_type:
        return {
            'follow_up_at': request.POST.get('follow_up_at'),
            'channel': request.POST.get('channel'),
            'notes': request.POST.get('notes'),
            'proof': request.FILES.get('proof'),
        }
    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return None
    return {
        'follow_up_at': data.get('follow_up_at'),
        'channel': data.get('channel'),
        'notes': data.get('notes'),
        'proof': None,
        'action': data.get('action'),
    }


def _validate_follow_up_fields(fields):
    """Returns (follow_up_at, channel, notes, error_response)."""
    if fields is None:
        return None, None, None, JsonResponse({'success': False, 'message': 'Invalid JSON data'}, status=400)
    follow_up_at = _parse_follow_up_at(fields.get('follow_up_at'))
    if not follow_up_at:
        return None, None, None, JsonResponse({'success': False, 'message': 'A valid follow-up date/time is required'})
    channel = (fields.get('channel') or QuotationFollowUp.Channel.CALL).strip()
    if channel not in {c[0] for c in QuotationFollowUp.Channel.choices}:
        return None, None, None, JsonResponse({'success': False, 'message': 'Invalid channel'})
    return follow_up_at, channel, (fields.get('notes') or '').strip(), None


@csrf_exempt
@login_required
@perm_required('quotations_followups')
def follow_up_api(request, quotation_id):
    """Create a follow-up on a quotation (JSON or multipart with proof)."""
    quotation = get_object_or_404(Quotation, id=quotation_id)
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

    fields = _read_follow_up_fields(request)
    follow_up_at, channel, notes, error = _validate_follow_up_fields(fields)
    if error:
        return error

    fu = QuotationFollowUp.objects.create(
        quotation=quotation,
        follow_up_at=follow_up_at,
        channel=channel,
        notes=notes,
        created_by=request.user,
    )
    if fields.get('proof'):
        fu.proof = fields['proof']
        fu.save(update_fields=['proof'])

    payload = _follow_up_payload(quotation)
    payload.update({'success': True, 'message': 'Follow-up created'})
    return JsonResponse(payload)


@csrf_exempt
@login_required
@perm_required('quotations_followups')
def follow_up_detail_api(request, follow_up_id):
    """Update / complete / reopen / delete / remove_proof on a follow-up."""
    fu = get_object_or_404(QuotationFollowUp.objects.select_related('quotation'), id=follow_up_id)
    quotation = fu.quotation
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

    # Action comes from POST (multipart) or JSON
    if request.content_type and 'multipart' in request.content_type:
        action = request.POST.get('action')
        fields = _read_follow_up_fields(request)
    else:
        fields = _read_follow_up_fields(request)
        if fields is None:
            return JsonResponse({'success': False, 'message': 'Invalid JSON data'}, status=400)
        action = fields.get('action')

    try:
        if action == 'update':
            follow_up_at, channel, notes, error = _validate_follow_up_fields(fields)
            if error:
                return error
            fu.follow_up_at = follow_up_at
            fu.channel = channel
            fu.notes = notes
            if fields.get('proof'):
                try:
                    if fu.proof and fu.proof.name and fu.proof.storage.exists(fu.proof.name):
                        fu.proof.storage.delete(fu.proof.name)
                except Exception:
                    pass
                fu.proof = fields['proof']
            fu.save()
            payload = _follow_up_payload(quotation)
            payload.update({'success': True, 'message': 'Follow-up updated'})
            return JsonResponse(payload)

        elif action == 'complete':
            fu.is_done = True
            fu.completed_at = timezone.now()
            fu.save(update_fields=['is_done', 'completed_at'])
            payload = _follow_up_payload(quotation)
            payload.update({'success': True, 'message': 'Follow-up marked as completed'})
            return JsonResponse(payload)

        elif action == 'reopen':
            fu.is_done = False
            fu.completed_at = None
            fu.save(update_fields=['is_done', 'completed_at'])
            payload = _follow_up_payload(quotation)
            payload.update({'success': True, 'message': 'Follow-up reopened'})
            return JsonResponse(payload)

        elif action == 'delete':
            try:
                if fu.proof and fu.proof.name and fu.proof.storage.exists(fu.proof.name):
                    fu.proof.storage.delete(fu.proof.name)
            except Exception:
                pass
            fu.delete()
            payload = _follow_up_payload(quotation)
            payload.update({'success': True, 'message': 'Follow-up deleted'})
            return JsonResponse(payload)

        elif action == 'remove_proof':
            try:
                if fu.proof and fu.proof.name and fu.proof.storage.exists(fu.proof.name):
                    fu.proof.storage.delete(fu.proof.name)
            except Exception:
                pass
            fu.proof = None
            fu.save(update_fields=['proof'])
            payload = _follow_up_payload(quotation)
            payload.update({'success': True, 'message': 'Proof removed'})
            return JsonResponse(payload)

        return JsonResponse({'success': False, 'message': 'Invalid action'})
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)


# ==============================================================
# Purchase Order / Order Management
# ==============================================================


def _next_po_number():
    """PO-<year>-<seq> — sequence resets each year."""
    prefix = f"PO-{timezone.now().year}-"
    last = (PurchaseOrder.objects
            .filter(po_number__startswith=prefix)
            .order_by('-po_number')
            .values_list('po_number', flat=True)
            .first())
    seq = 1
    if last:
        try:
            seq = int(last.rsplit('-', 1)[-1]) + 1
        except (ValueError, IndexError):
            seq = PurchaseOrder.objects.filter(po_number__startswith=prefix).count() + 1
    return f"{prefix}{seq:04d}"


def _is_overdue(po, today=None):
    """An open order past its due date is overdue."""
    today = today or timezone.now().date()
    return bool(po.expected_date and po.expected_date < today
                and po.status in _OPEN_PO_STATUSES)


def _serialize_order(po):
    ordered = 0
    remaining = 0
    for item in po.items.all():
        ordered += item.ordered_quantity
        remaining += item.remaining_quantity
    quotation = po.quotation
    customer = po.customer
    return {
        'id': po.id,
        'po_number': po.po_number or '',
        'quotation_id': quotation.id if quotation else None,
        'quotation_number': quotation.quotation_number if quotation else '',
        'customer_name': customer.name if customer else '',
        'order_date': po.order_date.strftime('%Y-%m-%d') if po.order_date else '',
        'expected_date': po.expected_date.strftime('%Y-%m-%d') if po.expected_date else '',
        'ordered_qty': ordered,
        'remaining_qty': remaining,
        'status': po.status,
        'status_display': po.get_status_display(),
        'is_overdue': _is_overdue(po),
        'created_by': (po.created_by.get_full_name() or po.created_by.username) if po.created_by else '',
        'created_at': po.created_at.strftime('%Y-%m-%d %H:%M') if po.created_at else '',
    }


@csrf_exempt
@login_required
@perm_required('quotations_openpo')
def create_order_from_quotation(request, quotation_id):
    """OPEN PO TO FACTORY — create a Purchase Order snapshot from an accepted quotation."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)
    quotation = get_object_or_404(
        Quotation.objects.select_related('customer').prefetch_related('items__product'),
        id=quotation_id,
    )
    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'message': 'Invalid JSON data'}, status=400)

    if quotation.status != Quotation.Status.ACCEPTED:
        return JsonResponse({
            'success': False,
            'message': 'Mark the quotation as Accepted first (customer confirms outside the system)',
        }, status=400)

    existing = quotation.purchase_orders.exclude(status=PurchaseOrder.Status.CANCELLED).first()
    if existing:
        return JsonResponse({
            'success': False,
            'message': f'Purchase Order {existing.po_number or existing.id} already exists for this quotation',
            'existing_po_id': existing.id,
            'redirect_url': f'/orders/{existing.id}/',
        }, status=400)

    items = list(quotation.items.select_related('product').all())
    if not items:
        return JsonResponse({'success': False, 'message': 'Quotation has no items to order'})

    expected_raw = (data.get('expected_date') or '').strip()
    expected_date = parse_date(expected_raw) if expected_raw else None
    if not expected_date:
        return JsonResponse({'success': False, 'message': 'A valid Due Date is required'}, status=400)
    remarks = (data.get('remarks') or '').strip()

    order = None
    for attempt in range(5):
        try:
            with transaction.atomic():
                order = PurchaseOrder.objects.create(
                    po_number=_next_po_number(),
                    quotation=quotation,
                    order_date=timezone.now().date(),
                    expected_date=expected_date,
                    status=PurchaseOrder.Status.ORDERED,
                    remarks=remarks,
                    created_by=request.user,
                )
                for it in items:
                    PurchaseOrderItem.objects.create(
                        purchase_order=order,
                        product=it.product,
                        quotation_item=it,
                        product_code=it.product_code or (it.product.child_code if it.product else ''),
                        description=it.description or (it.product.description if it.product else ''),
                        ordered_quantity=it.quantity,
                        note=it.note or '',
                    )
            break
        except IntegrityError:
            if attempt == 4:
                return JsonResponse({'success': False, 'message': 'Could not allocate a PO number, please retry'}, status=500)

    return JsonResponse({
        'success': True,
        'message': f'Purchase Order {order.po_number} created',
        'order_id': order.id,
        'po_number': order.po_number,
        'redirect_url': f'/orders/{order.id}/',
    })


@login_required
@perm_required('orders')
def order_list_view(request):
    """Orders / Purchase Orders dashboard page"""
    return render(request, 'orders.html', {'statuses': PurchaseOrder.Status.choices})


@login_required
@perm_required('orders')
def order_list_api(request):
    """Filtered order list for the dashboard table."""
    qs = (PurchaseOrder.objects
          .select_related('quotation__customer', 'created_by')
          .prefetch_related('items'))
    q = (request.GET.get('q') or '').strip()
    status = (request.GET.get('status') or '').strip()
    date_from = (request.GET.get('date_from') or '').strip()
    date_to = (request.GET.get('date_to') or '').strip()
    if q:
        qs = qs.filter(Q(po_number__icontains=q)
                       | Q(quotation__quotation_number__icontains=q)
                       | Q(quotation__customer__name__icontains=q))
    if status == 'overdue':
        qs = qs.filter(expected_date__lt=timezone.now().date(), status__in=_OPEN_PO_STATUSES)
    elif status:
        qs = qs.filter(status=status)
    if date_from:
        qs = qs.filter(order_date__gte=date_from)
    if date_to:
        qs = qs.filter(order_date__lte=date_to)
    return JsonResponse({'orders': [_serialize_order(po) for po in qs]})


@login_required
@perm_required('orders')
def order_detail_view(request, order_id):
    """Order / Purchase Order detail page"""
    order = get_object_or_404(
        PurchaseOrder.objects.select_related('quotation__customer', 'quotation__cart', 'created_by'),
        id=order_id,
    )
    items = order.items.select_related('product', 'quotation_item').prefetch_related('product__images')
    quotation = order.quotation
    customer = order.customer
    badge_class = {'draft': 'secondary', 'ordered': 'info', 'partially_received': 'warning',
                   'received': 'success', 'cancelled': 'danger'}.get(order.status, 'secondary')

    receiving_records = (order.receiving_records
                         .select_related('order_item', 'product', 'received_by').all())

    context = {
        'order': order,
        'items': items,
        'quotation': quotation,
        'customer': customer,
        'statuses': PurchaseOrder.Status.choices,
        'badge_class': badge_class,
        'is_overdue': _is_overdue(order),
        'today': timezone.now().date(),
        'total_ordered': sum(i.ordered_quantity for i in items),
        'total_received': sum(i.received_quantity for i in items),
        'total_remaining': sum(i.remaining_quantity for i in items),
        'receiving_records': receiving_records,
        'conditions': ReceivingRecord.Condition.choices,
        'can_receive': order.status in _OPEN_PO_STATUSES and user_can(request.user, 'orders_receive'),
    }
    return render(request, 'order_detail.html', context)


@login_required
@perm_required('orders')
def order_print_view(request, order_id):
    """Render a Purchase Order for printing / save-as-PDF (factory document)"""
    try:
        order = get_object_or_404(
            PurchaseOrder.objects.select_related('quotation__customer', 'quotation__cart', 'created_by'),
            id=order_id,
        )
        items = order.items.select_related('product', 'quotation_item').prefetch_related('product__images')
        quotation = order.quotation
        customer = order.customer
        currency_label = quotation.currency if quotation and quotation.currency else 'THB'

        items_for_print = []
        for it in items:
            p = it.product
            image_url = ''
            try:
                if p and p.images.exists():
                    image_url = request.build_absolute_uri(p.images.first().image.url)
            except Exception:
                image_url = ''
            qi = it.quotation_item
            items_for_print.append({
                'code': it.product_code or (p.child_code if p else '-'),
                'description': it.description or (p.description if p else '') or '-',
                'quantity': it.ordered_quantity,
                'unit': float(qi.unit_price or 0) if qi else 0,
                'amount': float(qi.line_amount or 0) if qi else 0,
                'note': it.note or (p.note if p else '') or '',
                'image_url': image_url,
            })

        salesperson = ''
        if order.created_by:
            salesperson = order.created_by.get_full_name() or order.created_by.username
        address_display = ''
        cart = getattr(quotation, 'cart', None) if quotation else None
        if cart and cart.address_override:
            address_display = cart.address_override
        elif customer and customer.address:
            address_display = customer.address

        context = {
            'order': order,
            'quotation': quotation,
            'customer': customer,
            'items_for_print': items_for_print,
            'total_amount': float(quotation.total_amount or 0) if quotation else 0,
            'total_quantity': sum(i['quantity'] for i in items_for_print),
            'total_items': len(items_for_print),
            'currency_label': currency_label,
            'print_date': timezone.now(),
            'salesperson_name': salesperson,
            'doc_ref': getattr(cart, 'doc_ref', None) or '',
            'customer_code': getattr(cart, 'customer_code', None) or '',
            'address_display': address_display,
        }
        return render(request, 'order_print.html', context)
    except Exception as e:
        messages.error(request, f'Error preparing purchase order for printing: {str(e)}')
        return redirect('order_detail', order_id=order_id)


@csrf_exempt
@login_required
@perm_required('orders')
def order_api(request, order_id):
    """API for order update / delete (items are read-only quotation snapshots)."""
    order = get_object_or_404(PurchaseOrder, id=order_id)
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'message': 'Invalid JSON data'}, status=400)

    action = data.get('action')
    required_perm = 'orders_delete' if action == 'delete' else 'orders_edit'
    if not user_can(request.user, required_perm):
        return deny(request)
    try:
        if action == 'update':
            if 'order_date' in data:
                raw = (data.get('order_date') or '').strip()
                parsed = parse_date(raw) if raw else None
                if raw and not parsed:
                    return JsonResponse({'success': False, 'message': 'Invalid order date'}, status=400)
                order.order_date = parsed
            if 'expected_date' in data:
                raw = (data.get('expected_date') or '').strip()
                parsed = parse_date(raw) if raw else None
                if not parsed:
                    return JsonResponse({'success': False, 'message': 'A valid Due Date is required'}, status=400)
                order.expected_date = parsed
            if 'status' in data:
                status = (data.get('status') or '').strip()
                if status not in {c[0] for c in PurchaseOrder.Status.choices}:
                    return JsonResponse({'success': False, 'message': 'Invalid status'}, status=400)
                if status == PurchaseOrder.Status.RECEIVED:
                    remaining = sum(i.remaining_quantity for i in order.items.all())
                    if remaining > 0:
                        return JsonResponse({
                            'success': False,
                            'message': f'Cannot mark Received while {remaining} unit(s) are still undelivered',
                        }, status=400)
                order.status = status
            if 'remarks' in data:
                order.remarks = (data.get('remarks') or '').strip()
            order.save()
            return JsonResponse({'success': True, 'message': 'Order updated', 'order': _serialize_order(order)})

        elif action == 'delete':
            number = order.po_number
            order.delete()
            return JsonResponse({'success': True, 'message': f'Purchase Order {number} deleted'})

        return JsonResponse({'success': False, 'message': 'Invalid action'})
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)


# ==============================================================
# Receiving Workflow
# ==============================================================

def _serialize_order_item(item):
    product = item.product
    return {
        'id': item.id,
        'product_id': product.id if product else None,
        'product_code': item.product_code or (product.child_code if product else ''),
        'description': item.description or (product.description if product else '') or '',
        'ordered_quantity': item.ordered_quantity,
        'received_quantity': item.received_quantity,
        'remaining_quantity': item.remaining_quantity,
        'note': item.note or '',
    }


def _serialize_receiving_record(rec):
    photo_url = ''
    photo_name = ''
    try:
        if rec.photo and rec.photo.name:
            photo_url = rec.photo.url
            photo_name = rec.photo.name.rsplit('/', 1)[-1]
    except Exception:
        pass
    return {
        'id': rec.id,
        'received_at': rec.received_at.strftime('%Y-%m-%d %H:%M') if rec.received_at else '',
        'order_item_id': rec.order_item_id,
        'product_id': rec.product_id,
        'product_code': rec.product_code or (rec.product.child_code if rec.product else ''),
        'quantity': rec.quantity,
        'condition': rec.condition,
        'condition_display': rec.get_condition_display(),
        'notes': rec.notes or '',
        'photo_url': photo_url,
        'photo_name': photo_name,
        'received_by': (rec.received_by.get_full_name() or rec.received_by.username) if rec.received_by else '',
    }


def _order_payload(order):
    items = order.items.select_related('product', 'quotation_item').all()
    records = order.receiving_records.select_related('order_item', 'product', 'received_by').all()
    return {
        'order': _serialize_order(order),
        'items': [_serialize_order_item(i) for i in items],
        'records': [_serialize_receiving_record(r) for r in records],
    }


def _recompute_order_status(order):
    """Set status from actual received quantities (open orders only)."""
    if order.status == PurchaseOrder.Status.CANCELLED:
        return
    items = list(order.items.all())
    if not items:
        return
    total_received = sum(i.received_quantity for i in items)
    if all(i.received_quantity >= i.ordered_quantity for i in items):
        new_status = PurchaseOrder.Status.RECEIVED
    elif total_received > 0:
        new_status = PurchaseOrder.Status.PARTIALLY_RECEIVED
    else:
        new_status = PurchaseOrder.Status.ORDERED
    if new_status != order.status:
        order.status = new_status
        order.save(update_fields=['status', 'updated_at'])


@csrf_exempt
@login_required
@perm_required('orders_receive')
def order_receive_api(request, order_id):
    """Record received quantities/conditions/photos for an order's items."""
    order = get_object_or_404(PurchaseOrder, id=order_id)
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)
    if order.status not in _OPEN_PO_STATUSES:
        return JsonResponse({
            'success': False,
            'message': f'Cannot receive on a {order.get_status_display()} order',
        }, status=400)

    # items payload arrives as JSON body or an 'items' form field (multipart w/ photos)
    if request.content_type and 'multipart' in request.content_type:
        raw_items = request.POST.get('items')
        try:
            entries = json.loads(raw_items) if raw_items else []
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'message': 'Invalid items data'}, status=400)
    else:
        try:
            data = json.loads(request.body) if request.body else {}
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'message': 'Invalid JSON data'}, status=400)
        entries = data.get('items') or []

    if not isinstance(entries, list) or not entries:
        return JsonResponse({'success': False, 'message': 'No items to receive'}, status=400)

    items_by_id = {i.id: i for i in order.items.select_related('product').all()}
    valid_conditions = {c[0] for c in ReceivingRecord.Condition.choices}
    cleaned = []
    for entry in entries:
        item = items_by_id.get(entry.get('order_item_id'))
        if not item:
            return JsonResponse({'success': False, 'message': 'Item does not belong to this order'}, status=400)
        try:
            qty = int(entry.get('quantity'))
        except (TypeError, ValueError):
            return JsonResponse({'success': False, 'message': f'Invalid quantity for {item.product_code or item.id}'}, status=400)
        if qty < 1:
            return JsonResponse({'success': False, 'message': f'Quantity must be at least 1 for {item.product_code or item.id}'}, status=400)
        if qty > item.remaining_quantity:
            return JsonResponse({
                'success': False,
                'message': f'Cannot receive {qty} x {item.product_code or item.id} — only {item.remaining_quantity} remaining',
            }, status=400)
        condition = (entry.get('condition') or ReceivingRecord.Condition.GOOD).strip()
        if condition not in valid_conditions:
            return JsonResponse({'success': False, 'message': 'Invalid condition'}, status=400)
        cleaned.append({
            'item': item,
            'quantity': qty,
            'condition': condition,
            'notes': (entry.get('notes') or '').strip(),
        })

    try:
        with transaction.atomic():
            for c in cleaned:
                item = c['item']
                ReceivingRecord.objects.create(
                    purchase_order=order,
                    order_item=item,
                    product=item.product,
                    product_code=item.product_code or (item.product.child_code if item.product else ''),
                    quantity=c['quantity'],
                    condition=c['condition'],
                    notes=c['notes'],
                    photo=request.FILES.get(f'photo_{item.id}'),
                    received_by=request.user,
                )
                item.received_quantity += c['quantity']
                item.save(update_fields=['received_quantity'])
            _recompute_order_status(order)
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)

    payload = _order_payload(order)
    total_units = sum(c['quantity'] for c in cleaned)
    payload.update({
        'success': True,
        'message': f'Received {total_units} unit(s) across {len(cleaned)} item(s) — order is now {order.get_status_display()}',
    })
    return JsonResponse(payload)


# ==============================================================
# Dashboard, Notifications & Reports
# ==============================================================

@login_required
@perm_required('dashboard')
def dashboard_view(request):
    """Management dashboard — summary stats + needs-attention list."""
    now = timezone.now()
    today = now.date()

    fu_groups = follow_up_items(now)
    po_groups = order_items(today)

    q_counts = {s: 0 for s, _ in Quotation.Status.choices}
    for row in Quotation.objects.values('status').annotate(n=models.Count('id')):
        q_counts[row['status']] = row['n']

    o_counts = {s: 0 for s, _ in PurchaseOrder.Status.choices}
    for row in PurchaseOrder.objects.values('status').annotate(n=models.Count('id')):
        o_counts[row['status']] = row['n']

    recv = ReceivingRecord.objects.aggregate(
        total_units=models.Sum('quantity'), records=models.Count('id'))
    recv_month = ReceivingRecord.objects.filter(
        received_at__year=today.year, received_at__month=today.month
    ).aggregate(units=models.Sum('quantity'))

    stats = {
        'quotations_total': Quotation.objects.count(),
        'quotations_by_status': q_counts,
        'orders_total': PurchaseOrder.objects.count(),
        'orders_by_status': o_counts,
        'orders_overdue': len(po_groups['overdue']),
        'orders_open': sum(len(po_groups[g]) for g in ('overdue', 'due_soon', 'open')),
        'fu_pending': QuotationFollowUp.objects.filter(is_done=False).count(),
        'fu_overdue': len(fu_groups['overdue']),
        'fu_due_today': len(fu_groups['due_today']),
        'fu_upcoming': len(fu_groups['upcoming']),
        'received_units': recv['total_units'] or 0,
        'received_records': recv['records'],
        'received_units_month': recv_month['units'] or 0,
    }

    context = {
        'stats': stats,
        'fu_groups': fu_groups,
        'po_groups': po_groups,
        'attention_items': build_notifications(now)['notifications'],
        'line_configured': line_configured(),
    }
    return render(request, 'dashboard.html', context)


@login_required
@perm_required('notifications')
def notifications_view(request):
    """Notifications page — grouped pending follow-ups and orders."""
    data = build_notifications()
    return render(request, 'notifications.html', {
        'groups': data['groups'],
        'line_configured': data['line_configured'],
    })


@login_required
@perm_required('notifications')
def notifications_api(request):
    """JSON feed for the navbar bell."""
    if request.method != 'GET':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)
    data = build_notifications()
    return JsonResponse({
        'notifications': data['notifications'],
        'count': data['count'],
        'line_configured': data['line_configured'],
    })


@csrf_exempt
@login_required
@perm_required('notifications')
def send_line_notification(request):
    """Push the staff reminder summary to LINE (staff-only trigger)."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)
    result = send_line_message(build_reminder_text())
    return JsonResponse({
        'success': result['sent'],
        'sent': result['sent'],
        'message': 'LINE reminder sent' if result['sent'] else result['reason'],
        'configured': result.get('configured', False),
    })


@login_required
@perm_required('reports')
def reports_view(request):
    """Reports page — charts + filterable tables."""
    return render(request, 'reports.html', {
        'quotation_statuses': Quotation.Status.choices,
        'order_statuses': PurchaseOrder.Status.choices,
        'fu_states': [('upcoming', 'Upcoming'), ('overdue', 'Overdue'), ('completed', 'Completed')],
        'conditions': ReceivingRecord.Condition.choices,
    })


_REPORT_TYPES = ('quotations', 'follow_ups', 'orders', 'receiving')


@login_required
@perm_required('reports')
def reports_api(request, report_type):
    """Filtered report rows for the reports page tables."""
    if report_type not in _REPORT_TYPES:
        return JsonResponse({'success': False, 'message': 'Invalid report type'}, status=400)

    q = (request.GET.get('q') or '').strip()
    status = (request.GET.get('status') or '').strip()
    date_from = (request.GET.get('date_from') or '').strip()
    date_to = (request.GET.get('date_to') or '').strip()
    now = timezone.now()

    if report_type == 'quotations':
        qs = Quotation.objects.select_related('customer', 'created_by').prefetch_related('items')
        if q:
            qs = qs.filter(Q(quotation_number__icontains=q) | Q(customer__name__icontains=q))
        if status:
            qs = qs.filter(status=status)
        if date_from:
            qs = qs.filter(quotation_date__gte=date_from)
        if date_to:
            qs = qs.filter(quotation_date__lte=date_to)
        rows = [{
            'quotation_number': x.quotation_number or f'#{x.id}',
            'quotation_date': x.quotation_date.strftime('%Y-%m-%d') if x.quotation_date else '',
            'validity_date': x.validity_date.strftime('%Y-%m-%d') if x.validity_date else '',
            'customer_name': x.customer.name if x.customer else '',
            'items_count': x.items.count(),
            'currency': x.currency,
            'total_amount': float(x.total_amount or 0),
            'status': x.status,
            'status_display': x.get_status_display(),
            'created_by': (x.created_by.get_full_name() or x.created_by.username) if x.created_by else '',
            'url': f'/quotations/{x.id}/',
        } for x in qs]
        return JsonResponse({'rows': rows, 'count': len(rows)})

    if report_type == 'follow_ups':
        qs = (QuotationFollowUp.objects
              .select_related('quotation', 'quotation__customer', 'created_by')
              .order_by('follow_up_at'))
        if q:
            qs = qs.filter(Q(quotation__quotation_number__icontains=q)
                           | Q(quotation__customer__name__icontains=q))
        if status == 'completed':
            qs = qs.filter(is_done=True)
        elif status == 'overdue':
            qs = qs.filter(is_done=False, follow_up_at__lt=now)
        elif status == 'upcoming':
            qs = qs.filter(is_done=False, follow_up_at__gte=now)
        if date_from:
            qs = qs.filter(follow_up_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(follow_up_at__date__lte=date_to)
        rows = []
        for fu in qs:
            state = 'completed' if fu.is_done else ('overdue' if fu.follow_up_at and fu.follow_up_at < now else 'upcoming')
            rows.append({
                'quotation_number': fu.quotation.quotation_number if fu.quotation else '-',
                'customer_name': fu.quotation.customer.name if fu.quotation and fu.quotation.customer else '-',
                'follow_up_at': fu.follow_up_at.strftime('%Y-%m-%d %H:%M') if fu.follow_up_at else '',
                'channel': fu.get_channel_display(),
                'state': state,
                'notes': fu.notes or '',
                'created_by': (fu.created_by.get_full_name() or fu.created_by.username) if fu.created_by else '',
                'url': f'/quotations/{fu.quotation_id}/' if fu.quotation_id else '',
            })
        return JsonResponse({'rows': rows, 'count': len(rows)})

    if report_type == 'orders':
        qs = (PurchaseOrder.objects
              .select_related('quotation', 'quotation__customer', 'created_by')
              .prefetch_related('items'))
        if q:
            qs = qs.filter(Q(po_number__icontains=q)
                           | Q(quotation__quotation_number__icontains=q)
                           | Q(quotation__customer__name__icontains=q))
        if status == 'overdue':
            qs = qs.filter(expected_date__lt=now.date(), status__in=_OPEN_PO_STATUSES)
        elif status:
            qs = qs.filter(status=status)
        if date_from:
            qs = qs.filter(order_date__gte=date_from)
        if date_to:
            qs = qs.filter(order_date__lte=date_to)
        rows = []
        for po in qs:
            ordered = sum(i.ordered_quantity for i in po.items.all())
            received = sum(i.received_quantity for i in po.items.all())
            rows.append({
                'po_number': po.po_number or f'#{po.id}',
                'quotation_number': po.quotation.quotation_number if po.quotation else '-',
                'customer_name': po.customer.name if po.customer else '-',
                'order_date': po.order_date.strftime('%Y-%m-%d') if po.order_date else '',
                'expected_date': po.expected_date.strftime('%Y-%m-%d') if po.expected_date else '',
                'ordered_qty': ordered,
                'received_qty': received,
                'remaining_qty': max(ordered - received, 0),
                'status': po.status,
                'status_display': po.get_status_display(),
                'is_overdue': _is_overdue(po, now.date()),
                'created_by': (po.created_by.get_full_name() or po.created_by.username) if po.created_by else '',
                'url': f'/orders/{po.id}/',
            })
        return JsonResponse({'rows': rows, 'count': len(rows)})

    # report_type == 'receiving'
    qs = (ReceivingRecord.objects
          .select_related('purchase_order', 'purchase_order__quotation', 'order_item', 'product', 'received_by')
          .order_by('-received_at'))
    if q:
        qs = qs.filter(Q(purchase_order__po_number__icontains=q)
                       | Q(product_code__icontains=q)
                       | Q(purchase_order__quotation__quotation_number__icontains=q))
    if status:
        qs = qs.filter(condition=status)
    if date_from:
        qs = qs.filter(received_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(received_at__date__lte=date_to)
    rows = [{
        'received_at': r.received_at.strftime('%Y-%m-%d %H:%M') if r.received_at else '',
        'po_number': r.purchase_order.po_number if r.purchase_order else '-',
        'quotation_number': (r.purchase_order.quotation.quotation_number
                             if r.purchase_order and r.purchase_order.quotation else '-'),
        'product_code': r.product_code or (r.product.child_code if r.product else '-'),
        'quantity': r.quantity,
        'condition': r.condition,
        'condition_display': r.get_condition_display(),
        'notes': r.notes or '',
        'received_by': (r.received_by.get_full_name() or r.received_by.username) if r.received_by else '',
        'url': f'/orders/{r.purchase_order_id}/' if r.purchase_order_id else '',
    } for r in qs]
    return JsonResponse({'rows': rows, 'count': len(rows)})

# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------

def _serialize_user(u):
    return {
        'id': u.id,
        'username': u.username,
        'email': u.email or '',
        'role': u.role,
        'role_display': 'Superadmin' if u.is_superuser else u.get_role_display(),
        'is_active': u.is_active,
        'is_superuser': u.is_superuser,
        'last_login': u.last_login.strftime('%Y-%m-%d %H:%M') if u.last_login else '',
        'permissions': {k: bool((u.permissions or {}).get(k, DEFAULT_STAFF_PERMISSIONS.get(k, False)))
                        for k in ALL_PERMISSION_KEYS},
    }


@login_required
@admin_required
def users_view(request):
    """Users management page - admin only."""
    users = User.objects.all().order_by('username')
    return render(request, 'users.html', {
        'users': users,
        'users_data': {u.id: _serialize_user(u) for u in users},
        'permission_groups': PERMISSION_GROUPS,
        'default_permissions': DEFAULT_STAFF_PERMISSIONS,
    })


@csrf_exempt
@login_required
@admin_required
def users_api(request):
    """API for user management: create / update (role+perms+active) / set_password."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'message': 'Invalid JSON data'}, status=400)

    action = data.get('action')

    if action == 'create':
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        role = data.get('role') or User.Role.STAFF
        if not username:
            return JsonResponse({'success': False, 'message': 'Username is required'}, status=400)
        if User.objects.filter(username=username).exists():
            return JsonResponse({'success': False, 'message': 'Username already exists'}, status=400)
        if role not in dict(User.Role.choices):
            return JsonResponse({'success': False, 'message': 'Invalid role'}, status=400)
        try:
            validate_password(password)
        except ValidationError as e:
            return JsonResponse({'success': False, 'message': ' '.join(e.messages)}, status=400)
        perms = _clean_permissions(data.get('permissions'), role)
        user = User.objects.create_user(
            username=username,
            password=password,
            email=(data.get('email') or '').strip(),
            role=role,
            permissions=perms,
        )
        return JsonResponse({'success': True, 'message': f'User "{username}" created', 'user': _serialize_user(user)})

    user_id = data.get('user_id')
    target = User.objects.filter(id=user_id).first() if user_id else None
    if not target:
        return JsonResponse({'success': False, 'message': 'User not found'}, status=404)

    if action == 'update':
        if target.id == request.user.id:
            return JsonResponse({'success': False, 'message': 'You cannot change your own role or permissions'}, status=400)
        if target.is_superuser:
            return JsonResponse({'success': False, 'message': 'Superuser account cannot be modified'}, status=400)
        role = data.get('role') or target.role
        if role not in dict(User.Role.choices):
            return JsonResponse({'success': False, 'message': 'Invalid role'}, status=400)
        target.role = role
        target.is_active = bool(data.get('is_active', target.is_active))
        target.permissions = _clean_permissions(data.get('permissions'), role)
        target.save()
        return JsonResponse({'success': True, 'message': f'User "{target.username}" updated', 'user': _serialize_user(target)})

    if action == 'set_password':
        if target.id == request.user.id:
            return JsonResponse({'success': False, 'message': 'Use Change Password to update your own password'}, status=400)
        if target.is_superuser:
            return JsonResponse({'success': False, 'message': 'Superuser account cannot be modified'}, status=400)
        password = data.get('password') or ''
        try:
            validate_password(password, user=target)
        except ValidationError as e:
            return JsonResponse({'success': False, 'message': ' '.join(e.messages)}, status=400)
        target.set_password(password)
        target.save()
        return JsonResponse({'success': True, 'message': f'Password updated for "{target.username}"'})

    return JsonResponse({'success': False, 'message': 'Invalid action'}, status=400)


def _clean_permissions(raw, role):
    """Normalize the permissions dict sent from the permission matrix.

    Admin users do not need stored perms (they always pass user_can),
    but keep the dict anyway so demoting them later is predictable.
    """
    raw = raw or {}
    return {key: bool(raw.get(key, DEFAULT_STAFF_PERMISSIONS.get(key, False)))
            for key in ALL_PERMISSION_KEYS}
