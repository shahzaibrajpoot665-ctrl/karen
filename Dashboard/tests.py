from django.test import TestCase

# Create your tests here.

import io
import json
import os
import tempfile
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from openpyxl import Workbook
from PIL import Image as PillowImage

from .models import (
    Image, Product, ProductImageLink, User, GoogleSheetConfig,
    Customer, Cart, CartItem, PriceSetting, Quotation, QuotationItem, QuotationDocument,
    QuotationFollowUp, PurchaseOrder, PurchaseOrderItem, ReceivingRecord,
)
from .views import (
    EXPECTED_IMPORT_COLUMNS, _download_and_link_external_images,
    _normalize_import_image_url, _process_product_import_job,
    _get_product_import_state, _read_import_image, _ImageRedirectHandler,
)


class ImportImageReplacementTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.media.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        data = io.BytesIO()
        PillowImage.new('RGB', (4, 4), 'white').save(data, 'PNG')
        self.image_bytes = data.getvalue()
        self.download = patch('Dashboard.views._read_import_image', return_value=(self.image_bytes, '.png')).start()
        self.addCleanup(patch.stopall)
        self.product = Product.objects.create(parent_code='P', child_code='P1', location='A', qrcode_image='qr.png', barcode_image='bar.png')

    def replace(self, urls):
        _download_and_link_external_images(self.product, urls, 'https://shop.example/')
        return set(self.product.images.values_list('source_url', flat=True))

    def test_replaces_five_with_three_and_keeps_shared_assets(self):
        urls = [f'https://images.example/{i}.png' for i in range(5)]
        self.replace(urls)
        shared = self.product.images.order_by('id').first()
        other = Product.objects.create(parent_code='Q', child_code='Q1', location='B')
        other.images.add(shared)
        self.assertEqual(self.replace(urls[2:]), set(urls[2:]))
        self.assertEqual(self.product.images.count(), 3)
        self.assertEqual(Image.objects.count(), 5)
        self.assertTrue(other.images.filter(pk=shared.pk).exists())
        self.assertEqual(ProductImageLink.objects.filter(child_code='P1').count(), 3)
        recreated = Product(parent_code='P', child_code='P1', location='A')
        recreated.save()
        self.assertEqual(set(recreated.images.values_list('pk', flat=True)), set(self.product.images.values_list('pk', flat=True)))

    def test_repeated_urls_and_storage_rename_are_idempotent(self):
        url = 'https://images.example/photo.png'
        self.replace([url])
        image = self.product.images.get()
        image.image.save('renamed.png', ContentFile(self.image_bytes), save=True)
        for _ in range(3):
            self.replace([url, '  ' + url + '  ', url + '#preview'])
        self.assertEqual(self.product.images.count(), 1)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(self.download.call_count, 1)

    def test_same_basename_different_hosts_and_queries_remain_distinct(self):
        urls = ['https://one.example/photo.png', 'https://two.example/photo.png',
                'https://one.example/photo.png?version=2', 'https://one.example/download?id=3']
        self.assertEqual(self.replace(urls + urls), set(urls))
        self.replace(urls)
        self.assertEqual(self.product.images.count(), 4)
        self.assertEqual(self.download.call_count, 4)

    def test_blank_clears_links_but_none_leaves_them_unchanged(self):
        self.replace(['https://images.example/photo.png'])
        self.replace(None)
        self.assertEqual(self.product.images.count(), 1)
        self.replace([' ', ''])
        self.assertEqual(self.product.images.count(), 0)
        self.assertFalse(ProductImageLink.objects.filter(child_code='P1').exists())
        self.assertEqual(Image.objects.count(), 1)

    def test_failed_replacement_preserves_entire_old_set(self):
        self.replace(['https://images.example/old.png'])
        previous = list(self.product.images.values_list('pk', flat=True))
        self.download.side_effect = [(self.image_bytes, '.png'), ValueError('unavailable')]
        with self.assertRaisesMessage(ValueError, 'previous product images were kept'):
            self.replace(['https://images.example/new.png', 'https://images.example/bad.png?secret=value'])
        self.assertEqual(list(self.product.images.values_list('pk', flat=True)), previous)
        self.assertEqual(list(ProductImageLink.objects.filter(child_code='P1').values_list('image_id', flat=True)), previous)

    def test_local_media_reuses_exact_path_not_remote_basename(self):
        local = Image.objects.create(image=ContentFile(self.image_bytes, name='photo.png'))
        self.replace(['https://shop.example' + local.image.url, 'photo.png'])
        self.assertEqual(list(self.product.images.values_list('pk', flat=True)), [local.pk])
        self.download.assert_not_called()
        self.replace(['https://other.example/media/product_images/photo.png'])
        self.assertNotEqual(self.product.images.get().pk, local.pk)

    def test_missing_local_file_fails_without_clearing(self):
        self.replace(['https://images.example/old.png'])
        with self.assertRaises(ValueError):
            self.replace(['missing.png'])
        self.assertEqual(self.product.images.count(), 1)

    def test_url_normalization_preserves_query_and_path_case(self):
        self.assertEqual(_normalize_import_image_url(' HTTPS://IMAGES.EXAMPLE:443/Photo.PNG?v=2#x '),
                         'https://images.example/Photo.PNG?v=2')
        with self.assertRaises(ValueError):
            _normalize_import_image_url('https://user:password@images.example/photo.png')

    def workbook(self, urls):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(EXPECTED_IMPORT_COLUMNS)
        values = {'Parent Code': 'P', 'Child Code': 'P1', 'Location': 'NEW', 'QTY': 2,
                  'weight': 1, 'base': 10, 'increment': 2, 'Note': 'updated', 'Image URL': urls}
        sheet.append([values.get(header, '') for header in EXPECTED_IMPORT_COLUMNS])
        stream = io.BytesIO()
        workbook.save(stream)
        return stream.getvalue()

    def test_shared_excel_google_worker_replaces_and_clears(self):
        for index, urls in enumerate(['https://images.example/a.png,https://images.example/b.png',
                                      'https://images.example/b.png', '']):
            path = os.path.join(self.media.name, f'import{index}.xlsx')
            with open(path, 'wb') as output:
                output.write(self.workbook(urls))
            _process_product_import_job(f'test-{index}', path, 100, 'https://shop.example/')
            state = _get_product_import_state(f'test-{index}')
            self.assertEqual(state['failed_count'], 0, state)
            self.assertEqual(self.product.images.count(), [2, 1, 0][index])
        self.product.refresh_from_db()
        self.assertEqual(self.product.location, 'NEW')
        self.assertEqual(self.product.note, 'updated')

    def test_legacy_excel_replaces_and_clears(self):
        self.client.force_login(User.objects.create_user(username='importer', role='admin'))
        for urls, expected in [('https://images.example/a.png,https://images.example/b.png', 2),
                               ('https://images.example/b.png', 1), ('', 0)]:
            upload = SimpleUploadedFile('products.xlsx', self.workbook(urls))
            response = self.client.post(reverse('form'), {'type': 'bulk-create', 'xlsx_file': upload})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.product.images.count(), expected)

    def test_google_entry_dispatches_shared_worker_with_origin(self):
        self.client.force_login(User.objects.create_user(username='sheet-importer', role='admin'))
        GoogleSheetConfig.objects.create(pk=1, spreadsheet_id='sheet', sheet_gid='0')
        path = os.path.join(self.media.name, 'sheet.xlsx')
        with open(path, 'wb') as output:
            output.write(self.workbook('https://images.example/photo.png'))
        with patch('Dashboard.views._download_google_sheet_to_xlsx', return_value=path), patch('Dashboard.views.threading.Thread') as thread:
            from .views import google_sheet_import_start
            from django.test import RequestFactory
            request = RequestFactory().post('/', HTTP_HOST='testserver')
            request.user = User.objects.get(username='sheet-importer')
            response = google_sheet_import_start(request)
            self.assertEqual(response.status_code, 200)
            self.assertIs(thread.call_args.kwargs['target'], _process_product_import_job)
            self.assertEqual(thread.call_args.kwargs['args'][-1], 'http://testserver/')


class ImportImageDownloadTests(TestCase):
    def test_invalid_image_response_is_rejected(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'not an image'
        with patch('Dashboard.views._validate_image_download_url'), patch('Dashboard.views.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            with self.assertRaises(Exception):
                _read_import_image('https://images.example/photo.png')

    def test_private_redirect_is_rejected(self):
        with patch('Dashboard.views.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 80))]):
            with self.assertRaises(ValueError):
                _ImageRedirectHandler().redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/private')


class QuotationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='staff', password='pw', role='admin')
        self.client.force_login(self.user)
        PriceSetting.get_singleton()
        self.customer = Customer.objects.create(name='Test Customer')
        self.product = Product.objects.create(
            parent_code='P', child_code='P-CHILD', location='A',
            base='100', increment='5', description='Test product',
        )
        self.cart = Cart.objects.create(customer=self.customer, is_active=True)
        self.cart_item = CartItem.objects.create(cart=self.cart, product=self.product, quantity=3, note='line note')

    def _create(self, currency='THB'):
        return self.client.post(
            reverse('create_quotation_from_cart', args=[self.customer.id]),
            data=json.dumps({'action': 'create', 'currency': currency}),
            content_type='application/json',
        )

    def test_create_from_cart_snapshots_items_and_links_cart(self):
        response = self._create()
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        quotation = Quotation.objects.get()
        self.assertEqual(quotation.customer, self.customer)
        self.assertEqual(quotation.cart, self.cart)
        self.assertEqual(quotation.status, 'draft')
        self.assertEqual(quotation.currency, 'THB')
        self.assertTrue(quotation.quotation_number.startswith('QT-'))
        self.assertEqual(quotation.created_by, self.user)
        item = quotation.items.get()
        self.assertEqual(item.product, self.product)
        self.assertEqual(item.source_cart_item, self.cart_item)
        self.assertEqual(item.product_code, 'P-CHILD')
        self.assertEqual(item.quantity, 3)
        self.assertEqual(item.note, 'line note')
        self.assertEqual(item.line_amount, item.unit_price * 3)
        self.assertEqual(quotation.total_amount, item.line_amount)
        # Cart is left untouched
        self.assertTrue(Cart.objects.filter(id=self.cart.id, is_active=True).exists())
        self.assertEqual(self.cart.items.count(), 1)

    def test_create_on_empty_cart_fails(self):
        self.cart_item.delete()
        response = self._create()
        self.assertFalse(response.json()['success'])
        self.assertEqual(Quotation.objects.count(), 0)

    def test_quotation_numbers_are_unique(self):
        first = Quotation.objects.create(customer=self.customer, quotation_number='QT-2099-0001')
        response = self._create()
        self.assertTrue(response.json()['success'])
        quotation = Quotation.objects.exclude(id=first.id).get()
        self.assertNotEqual(quotation.quotation_number, 'QT-2099-0001')
        self.assertRegex(quotation.quotation_number, r'^QT-\d{4}-\d{4}$')

    def test_update_item_recalculates_line_and_total(self):
        self._create()
        quotation = Quotation.objects.get()
        item = quotation.items.get()
        response = self.client.post(
            reverse('quotation_api', args=[quotation.id]),
            data=json.dumps({'action': 'update_item', 'item_id': item.id, 'quantity': 5, 'unit_price': '10.50'}),
            content_type='application/json',
        )
        self.assertTrue(response.json()['success'])
        item.refresh_from_db()
        quotation.refresh_from_db()
        self.assertEqual(item.quantity, 5)
        self.assertEqual(str(item.unit_price), '10.50')
        self.assertEqual(str(item.line_amount), '52.50')
        self.assertEqual(str(quotation.total_amount), '52.50')

    def test_add_and_remove_item_recalculates_total(self):
        self._create()
        quotation = Quotation.objects.get()
        other = Product.objects.create(parent_code='Q', child_code='Q-CHILD', location='B', base='40', increment='2')
        self.client.post(
            reverse('quotation_api', args=[quotation.id]),
            data=json.dumps({'action': 'add_item', 'product_id': other.id, 'quantity': 2}),
            content_type='application/json',
        )
        self.assertEqual(quotation.items.count(), 2)
        quotation.refresh_from_db()
        self.assertEqual(quotation.total_amount, sum(i.line_amount for i in quotation.items.all()))
        item = quotation.items.get(product=other)
        self.client.post(
            reverse('quotation_api', args=[quotation.id]),
            data=json.dumps({'action': 'remove_item', 'item_id': item.id}),
            content_type='application/json',
        )
        self.assertEqual(quotation.items.count(), 1)

    def test_update_status(self):
        self._create()
        quotation = Quotation.objects.get()
        for status in ('sent', 'accepted', 'rejected'):
            response = self.client.post(
                reverse('quotation_api', args=[quotation.id]),
                data=json.dumps({'action': 'update_status', 'status': status}),
                content_type='application/json',
            )
            self.assertTrue(response.json()['success'])
            quotation.refresh_from_db()
            self.assertEqual(quotation.status, status)
        bad = self.client.post(
            reverse('quotation_api', args=[quotation.id]),
            data=json.dumps({'action': 'update_status', 'status': 'bogus'}),
            content_type='application/json',
        )
        self.assertFalse(bad.json()['success'])

    def test_update_header_fields(self):
        self._create()
        quotation = Quotation.objects.get()
        response = self.client.post(
            reverse('quotation_api', args=[quotation.id]),
            data=json.dumps({'action': 'update', 'quotation_date': '2026-01-15', 'validity_date': '2026-02-15',
                             'currency': 'USD', 'terms_conditions': 'Net 30', 'remarks': 'Rush order'}),
            content_type='application/json',
        )
        self.assertTrue(response.json()['success'])
        quotation.refresh_from_db()
        self.assertEqual(str(quotation.quotation_date), '2026-01-15')
        self.assertEqual(str(quotation.validity_date), '2026-02-15')
        self.assertEqual(quotation.currency, 'USD')
        self.assertEqual(quotation.terms_conditions, 'Net 30')
        self.assertEqual(quotation.remarks, 'Rush order')

    def test_list_api_filters(self):
        self._create()
        q1 = Quotation.objects.get()
        other_customer = Customer.objects.create(name='Other Person')
        Quotation.objects.create(customer=other_customer, quotation_number='QT-2099-9999', status='accepted')
        url = reverse('quotation_list_api')
        self.assertEqual(len(self.client.get(url).json()['quotations']), 2)
        self.assertEqual(len(self.client.get(url, {'q': 'Other'}).json()['quotations']), 1)
        self.assertEqual(len(self.client.get(url, {'status': 'accepted'}).json()['quotations']), 1)
        self.assertEqual(len(self.client.get(url, {'q': q1.quotation_number}).json()['quotations']), 1)

    def test_detail_and_print_pages_render(self):
        self._create()
        quotation = Quotation.objects.get()
        detail = self.client.get(reverse('quotation_detail', args=[quotation.id]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, quotation.quotation_number)
        print_response = self.client.get(reverse('quotation_print', args=[quotation.id]))
        self.assertEqual(print_response.status_code, 200)
        self.assertContains(print_response, quotation.quotation_number)
        self.assertContains(print_response, 'P-CHILD')

    def test_delete_quotation_keeps_related_records(self):
        self._create()
        quotation = Quotation.objects.get()
        QuotationDocument.objects.create(quotation=quotation, name='doc', uploaded_by=self.user)
        response = self.client.post(
            reverse('quotation_api', args=[quotation.id]),
            data=json.dumps({'action': 'delete'}),
            content_type='application/json',
        )
        self.assertTrue(response.json()['success'])
        self.assertEqual(Quotation.objects.count(), 0)
        self.assertEqual(QuotationItem.objects.count(), 0)
        self.assertEqual(QuotationDocument.objects.count(), 0)
        self.assertTrue(Customer.objects.filter(id=self.customer.id).exists())
        self.assertTrue(Product.objects.filter(id=self.product.id).exists())
        self.assertTrue(Cart.objects.filter(id=self.cart.id).exists())

    def test_item_survives_product_deletion_via_snapshot(self):
        self._create()
        quotation = Quotation.objects.get()
        product_id = self.product.id
        self.product.delete()
        item = quotation.items.get()
        self.assertIsNone(item.product)
        self.assertEqual(item.product_code, 'P-CHILD')
        self.assertEqual(item.quantity, 3)

    def test_unauthenticated_redirected(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('quotation_list')).status_code, 302)
        self.assertEqual(self.client.get(reverse('quotation_list_api')).status_code, 302)
        response = self.client.post(
            reverse('create_quotation_from_cart', args=[self.customer.id]),
            data=json.dumps({'action': 'create'}), content_type='application/json')
        self.assertEqual(response.status_code, 302)


class FollowUpTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.media.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.user = User.objects.create_user(username='staff', password='pw', role='admin')
        self.client.force_login(self.user)
        self.customer = Customer.objects.create(name='Test Customer')
        self.quotation = Quotation.objects.create(
            customer=self.customer, quotation_number='QT-2026-0001', status='draft')

    def _create(self, **overrides):
        payload = {'follow_up_at': '2099-01-01T10:00', 'channel': 'call', 'notes': 'first call'}
        payload.update(overrides)
        return self.client.post(
            reverse('follow_up_api', args=[self.quotation.id]),
            data=json.dumps(payload), content_type='application/json')

    def test_create_follow_up(self):
        response = self._create()
        self.assertTrue(response.json()['success'])
        fu = QuotationFollowUp.objects.get()
        self.assertEqual(fu.quotation, self.quotation)
        self.assertEqual(fu.channel, 'call')
        self.assertEqual(fu.notes, 'first call')
        self.assertEqual(fu.created_by, self.user)
        self.assertFalse(fu.is_done)
        self.assertEqual(str(fu.follow_up_at), '2099-01-01 10:00:00+00:00')

    def test_create_requires_valid_datetime(self):
        for bad in ({'follow_up_at': ''}, {'follow_up_at': 'not-a-date'}, {'follow_up_at': None}):
            response = self.client.post(
                reverse('follow_up_api', args=[self.quotation.id]),
                data=json.dumps(bad), content_type='application/json')
            self.assertFalse(response.json()['success'])
        self.assertEqual(QuotationFollowUp.objects.count(), 0)

    def test_create_rejects_invalid_channel(self):
        response = self._create(channel='smoke-signal')
        self.assertFalse(response.json()['success'])
        self.assertEqual(QuotationFollowUp.objects.count(), 0)

    def test_create_with_proof_multipart(self):
        upload = SimpleUploadedFile('proof.txt', b'call recording ref')
        response = self.client.post(
            reverse('follow_up_api', args=[self.quotation.id]),
            data={'follow_up_at': '2099-01-01T10:00', 'channel': 'call', 'notes': 'n', 'proof': upload})
        self.assertTrue(response.json()['success'])
        fu = QuotationFollowUp.objects.get()
        self.assertTrue(fu.proof.name.endswith('.txt'))
        self.assertTrue(response.json()['follow_ups'][0]['proof_url'])

    def test_update_follow_up(self):
        self._create()
        fu = QuotationFollowUp.objects.get()
        response = self.client.post(
            reverse('follow_up_detail_api', args=[fu.id]),
            data=json.dumps({'action': 'update', 'follow_up_at': '2099-02-02T15:30',
                             'channel': 'email', 'notes': 'updated'}),
            content_type='application/json')
        self.assertTrue(response.json()['success'])
        fu.refresh_from_db()
        self.assertEqual(fu.channel, 'email')
        self.assertEqual(fu.notes, 'updated')
        self.assertEqual(str(fu.follow_up_at), '2099-02-02 15:30:00+00:00')

    def test_complete_and_reopen(self):
        self._create()
        fu = QuotationFollowUp.objects.get()
        url = reverse('follow_up_detail_api', args=[fu.id])
        response = self.client.post(url, data=json.dumps({'action': 'complete'}), content_type='application/json')
        self.assertTrue(response.json()['success'])
        fu.refresh_from_db()
        self.assertTrue(fu.is_done)
        self.assertIsNotNone(fu.completed_at)
        response = self.client.post(url, data=json.dumps({'action': 'reopen'}), content_type='application/json')
        self.assertTrue(response.json()['success'])
        fu.refresh_from_db()
        self.assertFalse(fu.is_done)
        self.assertIsNone(fu.completed_at)

    def test_state_computation(self):
        self._create()  # future -> upcoming
        self._create(follow_up_at='2000-01-01T10:00', notes='old call')  # past -> overdue
        done = QuotationFollowUp.objects.create(
            quotation=self.quotation, follow_up_at=None, is_done=True)
        states = {fu.id: self._state(fu) for fu in QuotationFollowUp.objects.all()}
        self.assertEqual(states[done.id], 'completed')
        upcoming = QuotationFollowUp.objects.get(notes='first call')
        overdue = QuotationFollowUp.objects.get(notes='old call')
        self.assertEqual(states[upcoming.id], 'upcoming')
        self.assertEqual(states[overdue.id], 'overdue')

    def _state(self, fu):
        from .views import _follow_up_state
        return _follow_up_state(fu)

    def test_next_and_last_in_payload(self):
        self._create(follow_up_at='2099-01-05T10:00', notes='later')
        first = self._create(follow_up_at='2099-01-02T10:00', notes='sooner').json()
        self.assertEqual(first['summary']['next']['notes'], 'sooner')
        self.assertIsNone(first['summary']['last'])
        # complete the earlier one -> becomes last
        fu = QuotationFollowUp.objects.get(notes='sooner')
        response = self.client.post(
            reverse('follow_up_detail_api', args=[fu.id]),
            data=json.dumps({'action': 'complete'}), content_type='application/json')
        payload = response.json()
        self.assertEqual(payload['summary']['next']['notes'], 'later')
        self.assertEqual(payload['summary']['last']['notes'], 'sooner')

    def test_delete_removes_record_and_proof(self):
        upload = SimpleUploadedFile('proof.txt', b'x')
        self.client.post(
            reverse('follow_up_api', args=[self.quotation.id]),
            data={'follow_up_at': '2099-01-01T10:00', 'proof': upload})
        fu = QuotationFollowUp.objects.get()
        proof_path = fu.proof.path
        self.assertTrue(os.path.exists(proof_path))
        response = self.client.post(
            reverse('follow_up_detail_api', args=[fu.id]),
            data=json.dumps({'action': 'delete'}), content_type='application/json')
        self.assertTrue(response.json()['success'])
        self.assertEqual(QuotationFollowUp.objects.count(), 0)
        self.assertFalse(os.path.exists(proof_path))

    def test_multiple_follow_ups_per_quotation(self):
        self._create(notes='a')
        self._create(notes='b')
        self._create(notes='c')
        self.assertEqual(self.quotation.follow_ups.count(), 3)

    def test_detail_page_renders_follow_ups(self):
        self._create(notes='call the buyer')
        response = self.client.get(reverse('quotation_detail', args=[self.quotation.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Follow-Ups')
        self.assertContains(response, 'call the buyer')

    def test_quotation_delete_cascades_follow_ups(self):
        self._create()
        self.quotation.delete()
        self.assertEqual(QuotationFollowUp.objects.count(), 0)

    def test_unauthenticated_redirected(self):
        self.client.logout()
        response = self.client.post(
            reverse('follow_up_api', args=[self.quotation.id]),
            data=json.dumps({'follow_up_at': '2099-01-01T10:00'}), content_type='application/json')
        self.assertEqual(response.status_code, 302)


class PurchaseOrderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='staff', password='pw', role='admin')
        self.client.force_login(self.user)
        self.customer = Customer.objects.create(name='Test Customer')
        self.product = Product.objects.create(
            parent_code='P', child_code='SKU-PO-1', description='PO Test Product')
        self.quotation = Quotation.objects.create(
            customer=self.customer, quotation_number='QT-2026-0099',
            status='accepted', currency='THB', total_amount=100)
        self.qi = QuotationItem.objects.create(
            quotation=self.quotation, product=self.product,
            product_code='SKU-PO-1', description='PO Test Product',
            quantity=4, unit_price=25, line_amount=100)

    def _create(self, **payload):
        if 'expected_date' not in payload:
            payload['expected_date'] = '2099-06-30'
        return self.client.post(
            reverse('create_order_from_quotation', args=[self.quotation.id]),
            data=json.dumps(payload), content_type='application/json')

    def test_create_po_snapshots_items(self):
        response = self._create(remarks='factory note')
        self.assertTrue(response.json()['success'])
        po = PurchaseOrder.objects.get()
        self.assertEqual(po.quotation, self.quotation)
        self.assertEqual(po.status, 'ordered')
        self.assertEqual(str(po.expected_date), '2099-06-30')
        self.assertEqual(po.remarks, 'factory note')
        self.assertEqual(po.created_by, self.user)
        item = po.items.get()
        self.assertEqual(item.quotation_item, self.qi)
        self.assertEqual(item.product, self.product)
        self.assertEqual(item.product_code, 'SKU-PO-1')
        self.assertEqual(item.ordered_quantity, 4)
        self.assertEqual(item.received_quantity, 0)
        self.assertEqual(item.remaining_quantity, 4)

    def test_create_requires_accepted_quotation(self):
        for status in ('draft', 'sent', 'rejected'):
            self.quotation.status = status
            self.quotation.save()
            response = self._create()
            self.assertFalse(response.json()['success'], status)
        self.assertEqual(PurchaseOrder.objects.count(), 0)

    def test_duplicate_po_rejected_until_cancelled(self):
        self._create()
        response = self._create()
        self.assertFalse(response.json()['success'])
        self.assertEqual(response.json()['existing_po_id'], PurchaseOrder.objects.get().id)
        # Cancelling frees the quotation for a new PO
        po = PurchaseOrder.objects.get()
        po.status = 'cancelled'
        po.save()
        response = self._create()
        self.assertTrue(response.json()['success'])
        self.assertEqual(PurchaseOrder.objects.count(), 2)

    def test_po_number_format(self):
        self._create()
        po = PurchaseOrder.objects.get()
        self.assertRegex(po.po_number, r'^PO-\d{4}-\d{4}$')

    def test_due_date_required_and_valid(self):
        for bad in ({'expected_date': ''}, {'expected_date': 'not-a-date'}, {}):
            response = self.client.post(
                reverse('create_order_from_quotation', args=[self.quotation.id]),
                data=json.dumps(bad), content_type='application/json')
            self.assertFalse(response.json()['success'])
        self.assertEqual(PurchaseOrder.objects.count(), 0)

    def test_list_api_and_filters(self):
        self._create()
        other = Quotation.objects.create(
            customer=self.customer, quotation_number='QT-2026-0100', status='accepted')
        PurchaseOrder.objects.create(
            po_number='PO-2000-0001', quotation=other,
            order_date='2020-01-01', expected_date='2020-02-01', status='ordered')
        response = self.client.get(reverse('order_list_api'))
        self.assertEqual(len(response.json()['orders']), 2)
        # Filter by quotation number
        response = self.client.get(reverse('order_list_api'), {'q': 'QT-2026-0099'})
        orders = response.json()['orders']
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]['ordered_qty'], 4)
        self.assertEqual(orders[0]['remaining_qty'], 4)
        # Overdue pseudo-filter: PO-2000-0001 is past due and still open
        response = self.client.get(reverse('order_list_api'), {'status': 'overdue'})
        orders = response.json()['orders']
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]['po_number'], 'PO-2000-0001')
        self.assertTrue(orders[0]['is_overdue'])
        # Status filter
        response = self.client.get(reverse('order_list_api'), {'status': 'ordered'})
        self.assertEqual(len(response.json()['orders']), 2)
        # Date range
        response = self.client.get(reverse('order_list_api'), {'date_from': '2030-01-01'})
        self.assertEqual(len(response.json()['orders']), 0)

    def test_overdue_flag(self):
        PurchaseOrder.objects.create(
            po_number='PO-OD-1', quotation=self.quotation,
            expected_date='2000-01-01', status='ordered')
        PurchaseOrder.objects.create(
            po_number='PO-OD-2', quotation=self.quotation,
            expected_date='2000-01-01', status='received')
        PurchaseOrder.objects.create(
            po_number='PO-OD-3', quotation=self.quotation,
            expected_date='2000-01-01', status='cancelled')
        response = self.client.get(reverse('order_list_api'))
        overdue = {o['po_number']: o['is_overdue'] for o in response.json()['orders']}
        self.assertTrue(overdue['PO-OD-1'])
        self.assertFalse(overdue['PO-OD-2'])
        self.assertFalse(overdue['PO-OD-3'])

    def test_detail_page_renders(self):
        self._create()
        po = PurchaseOrder.objects.get()
        response = self.client.get(reverse('order_detail', args=[po.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, po.po_number)
        self.assertContains(response, 'QT-2026-0099')
        self.assertContains(response, 'SKU-PO-1')

    def test_print_page_renders(self):
        self._create()
        po = PurchaseOrder.objects.get()
        response = self.client.get(reverse('order_print', args=[po.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'PURCHASE ORDER')
        self.assertContains(response, po.po_number)
        self.assertContains(response, '2099-06-30')

    def test_update_action(self):
        self._create()
        po = PurchaseOrder.objects.get()
        response = self.client.post(
            reverse('order_api', args=[po.id]),
            data=json.dumps({'action': 'update', 'expected_date': '2099-12-31',
                             'status': 'partially_received', 'remarks': 'half arrived'}),
            content_type='application/json')
        self.assertTrue(response.json()['success'])
        po.refresh_from_db()
        self.assertEqual(str(po.expected_date), '2099-12-31')
        self.assertEqual(po.status, 'partially_received')
        self.assertEqual(po.remarks, 'half arrived')

    def test_update_rejects_invalid(self):
        self._create()
        po = PurchaseOrder.objects.get()
        url = reverse('order_api', args=[po.id])
        r1 = self.client.post(url, data=json.dumps({'action': 'update', 'status': 'bogus'}), content_type='application/json')
        self.assertFalse(r1.json()['success'])
        r2 = self.client.post(url, data=json.dumps({'action': 'update', 'expected_date': 'bad'}), content_type='application/json')
        self.assertFalse(r2.json()['success'])

    def test_delete_action(self):
        self._create()
        po = PurchaseOrder.objects.get()
        response = self.client.post(
            reverse('order_api', args=[po.id]),
            data=json.dumps({'action': 'delete'}), content_type='application/json')
        self.assertTrue(response.json()['success'])
        self.assertEqual(PurchaseOrder.objects.count(), 0)
        self.assertEqual(PurchaseOrderItem.objects.count(), 0)

    def test_quotation_delete_keeps_po(self):
        self._create()
        po = PurchaseOrder.objects.get()
        self.quotation.delete()
        po.refresh_from_db()
        self.assertIsNone(po.quotation)
        self.assertIsNone(po.customer)
        # Item snapshot still carries product code/description
        item = po.items.get()
        self.assertEqual(item.product_code, 'SKU-PO-1')

    def test_detail_page_lists_po_button(self):
        response = self.client.get(reverse('quotation_detail', args=[self.quotation.id]))
        self.assertContains(response, 'OPEN PO TO FACTORY')
        # After a PO exists, the button becomes a View PO link
        self._create()
        response = self.client.get(reverse('quotation_detail', args=[self.quotation.id]))
        self.assertContains(response, 'View PO')
        self.assertNotContains(response, 'id="openPoBtn"')

    def test_unauthenticated_redirected(self):
        self.client.logout()
        response = self.client.post(
            reverse('create_order_from_quotation', args=[self.quotation.id]),
            data=json.dumps({'expected_date': '2099-06-30'}), content_type='application/json')
        self.assertEqual(response.status_code, 302)


class ReceivingTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.media.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.user = User.objects.create_user(username='staff', password='pw', role='admin')
        self.client.force_login(self.user)
        self.customer = Customer.objects.create(name='Test Customer')
        self.product = Product.objects.create(
            parent_code='P', child_code='SKU-RCV-1', description='Receive Test')
        self.quotation = Quotation.objects.create(
            customer=self.customer, quotation_number='QT-2026-0200', status='accepted')
        self.order = PurchaseOrder.objects.create(
            po_number='PO-2026-0050', quotation=self.quotation,
            expected_date='2099-06-30', status='ordered')
        self.item = PurchaseOrderItem.objects.create(
            purchase_order=self.order, product=self.product,
            product_code='SKU-RCV-1', description='Receive Test', ordered_quantity=10)

    def _receive(self, entries, url=None):
        return self.client.post(
            url or reverse('order_receive_api', args=[self.order.id]),
            data=json.dumps({'items': entries}), content_type='application/json')

    def test_receive_creates_record_and_updates_item(self):
        response = self._receive([{'order_item_id': self.item.id, 'quantity': 4,
                                 'condition': 'good', 'notes': 'first batch'}])
        self.assertTrue(response.json()['success'])
        rec = ReceivingRecord.objects.get()
        self.assertEqual(rec.purchase_order, self.order)
        self.assertEqual(rec.order_item, self.item)
        self.assertEqual(rec.product, self.product)
        self.assertEqual(rec.product_code, 'SKU-RCV-1')
        self.assertEqual(rec.quantity, 4)
        self.assertEqual(rec.condition, 'good')
        self.assertEqual(rec.notes, 'first batch')
        self.assertEqual(rec.received_by, self.user)
        self.item.refresh_from_db()
        self.assertEqual(self.item.received_quantity, 4)
        self.assertEqual(self.item.remaining_quantity, 6)

    def test_partial_then_full_status_transitions(self):
        response = self._receive([{'order_item_id': self.item.id, 'quantity': 4}])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'partially_received')
        self.assertEqual(response.json()['order']['status'], 'partially_received')
        response = self._receive([{'order_item_id': self.item.id, 'quantity': 6}])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'received')
        self.assertEqual(response.json()['order']['status'], 'received')

    def test_over_receiving_rejected(self):
        response = self._receive([{'order_item_id': self.item.id, 'quantity': 11}])
        self.assertFalse(response.json()['success'])
        self.assertEqual(ReceivingRecord.objects.count(), 0)

    def test_wrong_order_item_rejected(self):
        other_item = PurchaseOrderItem.objects.create(
            purchase_order=PurchaseOrder.objects.create(
                po_number='PO-OTHER', status='ordered'),
            product_code='X', ordered_quantity=5)
        response = self._receive([{'order_item_id': other_item.id, 'quantity': 1}])
        self.assertFalse(response.json()['success'])

    def test_invalid_quantity_and_condition_rejected(self):
        for qty in (0, -1, 'abc', None):
            response = self._receive([{'order_item_id': self.item.id, 'quantity': qty}])
            self.assertFalse(response.json()['success'], qty)
        response = self._receive([{'order_item_id': self.item.id, 'quantity': 1, 'condition': 'broken'}])
        self.assertFalse(response.json()['success'])
        self.assertEqual(ReceivingRecord.objects.count(), 0)

    def test_blocked_on_terminal_statuses(self):
        for status in ('cancelled', 'received'):
            self.order.status = status
            self.order.save()
            response = self._receive([{'order_item_id': self.item.id, 'quantity': 1}])
            self.assertFalse(response.json()['success'], status)
        self.assertEqual(ReceivingRecord.objects.count(), 0)

    def test_receive_with_photo(self):
        upload = SimpleUploadedFile('proof.jpg', b'\xff\xd8\xff\xe0fakejpg', content_type='image/jpeg')
        response = self.client.post(
            reverse('order_receive_api', args=[self.order.id]),
            data={'items': json.dumps([{'order_item_id': self.item.id, 'quantity': 2,
                                        'condition': 'damaged', 'notes': 'dented'}]),
                  f'photo_{self.item.id}': upload})
        self.assertTrue(response.json()['success'])
        rec = ReceivingRecord.objects.get()
        self.assertTrue(rec.photo.name.endswith('.jpg'))
        self.assertIn('receiving_photos/', rec.photo.name)
        self.assertTrue(response.json()['records'][0]['photo_url'])

    def test_multiple_partial_receives_preserve_history(self):
        self._receive([{'order_item_id': self.item.id, 'quantity': 3, 'notes': 'batch 1'}])
        response = self._receive([{'order_item_id': self.item.id, 'quantity': 2, 'condition': 'damaged'}])
        self.assertEqual(ReceivingRecord.objects.count(), 2)
        self.item.refresh_from_db()
        self.assertEqual(self.item.received_quantity, 5)
        self.assertEqual(self.item.remaining_quantity, 5)
        records = response.json()['records']
        self.assertEqual(len(records), 2)
        # Most recent first (model ordering -received_at)
        self.assertEqual(records[0]['condition'], 'damaged')

    def test_detail_page_shows_history(self):
        self._receive([{'order_item_id': self.item.id, 'quantity': 4, 'notes': 'visible note'}])
        response = self.client.get(reverse('order_detail', args=[self.order.id]))
        self.assertContains(response, 'Receiving History')
        self.assertContains(response, 'visible note')
        self.assertContains(response, 'SKU-RCV-1')

    def test_manual_received_blocked_with_remaining(self):
        response = self.client.post(
            reverse('order_api', args=[self.order.id]),
            data=json.dumps({'action': 'update', 'status': 'received'}),
            content_type='application/json')
        self.assertFalse(response.json()['success'])
        # Fully received -> manual set allowed
        self._receive([{'order_item_id': self.item.id, 'quantity': 10}])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'received')

    def test_sku_lookup_finds_product(self):
        response = self.client.get(reverse('single_product'), {'code': 'SKU-RCV-1'})
        self.assertTrue(response.json()['success'])
        self.assertEqual(response.json()['id'], self.product.id)

    def test_receive_survives_product_deletion(self):
        self.product.delete()
        response = self._receive([{'order_item_id': self.item.id, 'quantity': 2}])
        self.assertTrue(response.json()['success'])
        rec = ReceivingRecord.objects.get()
        self.assertIsNone(rec.product)
        self.assertEqual(rec.product_code, 'SKU-RCV-1')

    def test_unauthenticated_redirected(self):
        self.client.logout()
        response = self.client.post(
            reverse('order_receive_api', args=[self.order.id]),
            data=json.dumps({'items': [{'order_item_id': self.item.id, 'quantity': 1}]}),
            content_type='application/json')
        self.assertEqual(response.status_code, 302)


class DashboardReportsNotificationTests(TestCase):
    def setUp(self):
        from django.utils import timezone as tz
        self.user = User.objects.create_user(username='staff', password='pw', role='admin')
        self.client.force_login(self.user)
        self.customer = Customer.objects.create(name='Test Customer')
        self.product = Product.objects.create(
            parent_code='P', child_code='SKU-D6', description='D6 Product')
        self.quotation = Quotation.objects.create(
            customer=self.customer, quotation_number='QT-2026-0300',
            status='accepted', currency='THB', total_amount=100,
            quotation_date=tz.now().date())
        self.qi = QuotationItem.objects.create(
            quotation=self.quotation, product=self.product,
            product_code='SKU-D6', quantity=4, unit_price=25, line_amount=100)
        self.order = PurchaseOrder.objects.create(
            po_number='PO-2026-0060', quotation=self.quotation,
            order_date=tz.now().date(), expected_date=tz.now().date() + timedelta(days=2),
            status='ordered')
        self.item = PurchaseOrderItem.objects.create(
            purchase_order=self.order, product=self.product,
            product_code='SKU-D6', ordered_quantity=4, received_quantity=1)
        now = tz.now()
        self.fu_overdue = QuotationFollowUp.objects.create(
            quotation=self.quotation, follow_up_at=now - timedelta(days=1), channel='call')
        self.fu_today = QuotationFollowUp.objects.create(
            quotation=self.quotation, follow_up_at=now + timedelta(hours=2), channel='line')
        self.fu_upcoming = QuotationFollowUp.objects.create(
            quotation=self.quotation, follow_up_at=now + timedelta(days=3), channel='email')
        self.fu_far = QuotationFollowUp.objects.create(
            quotation=self.quotation, follow_up_at=now + timedelta(days=30), channel='call')
        self.fu_done = QuotationFollowUp.objects.create(
            quotation=self.quotation, follow_up_at=now - timedelta(days=2), is_done=True)
        self.rec = ReceivingRecord.objects.create(
            purchase_order=self.order, order_item=self.item, product=self.product,
            product_code='SKU-D6', quantity=1, condition='good', received_by=self.user)
        self.overdue_order = PurchaseOrder.objects.create(
            po_number='PO-OD-6', quotation=self.quotation,
            order_date=tz.now().date() - timedelta(days=10),
            expected_date=tz.now().date() - timedelta(days=1), status='ordered')

    def test_dashboard_renders_real_counts(self):
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        stats = response.context['stats']
        self.assertEqual(stats['quotations_total'], 1)
        self.assertEqual(stats['quotations_by_status']['accepted'], 1)
        self.assertEqual(stats['orders_total'], 2)
        self.assertEqual(stats['orders_overdue'], 1)
        self.assertEqual(stats['fu_pending'], 4)
        self.assertEqual(stats['fu_overdue'], 1)
        self.assertEqual(stats['fu_due_today'], 1)
        self.assertEqual(stats['fu_upcoming'], 1)
        self.assertEqual(stats['received_units'], 1)
        self.assertEqual(stats['received_records'], 1)

    def test_notifications_api_classification(self):
        response = self.client.get(reverse('notifications_api'))
        data = response.json()
        kinds = [n['kind'] for n in data['notifications']]
        self.assertIn('follow_up_overdue', kinds)
        self.assertIn('follow_up_due', kinds)
        self.assertIn('follow_up_upcoming', kinds)
        self.assertIn('order_overdue', kinds)
        # due_soon: expected_date is +2 days (within 3)
        self.assertIn('order_due_soon', kinds)
        # completed follow-up must NOT appear; far-future (+30d) pending
        # follow-up is outside the 7-day upcoming window
        self.assertEqual(kinds.count('follow_up_overdue'), 1)
        self.assertEqual(kinds.count('follow_up_due'), 1)
        self.assertEqual(kinds.count('follow_up_upcoming'), 1)
        # every notification links to a real page
        for n in data['notifications']:
            self.assertTrue(n['url'].startswith(('/quotations/', '/orders/')))
        self.assertFalse(data['line_configured'])

    def test_notifications_page_renders(self):
        response = self.client.get(reverse('notifications_page'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Overdue')
        self.assertContains(response, 'PO-OD-6')

    def test_send_line_not_configured(self):
        response = self.client.post(reverse('send_line_notification'), data='{}',
                                    content_type='application/json')
        data = response.json()
        self.assertFalse(data['success'])
        self.assertFalse(data['configured'])
        self.assertIn('not configured', data['message'])

    @override_settings(LINE_CHANNEL_ACCESS_TOKEN='tok', LINE_TARGET_ID='U123')
    def test_send_line_configured_mocked(self):
        with patch('urllib.request.urlopen') as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = b'{}'
            response = self.client.post(reverse('send_line_notification'), data='{}',
                                        content_type='application/json')
        data = response.json()
        self.assertTrue(data['sent'])
        args = mock_open.call_args[0][0]
        self.assertEqual(args.full_url, 'https://api.line.me/v2/bot/message/push')
        self.assertIn(b'QT-2026-0300', args.data)

    @override_settings(LINE_CHANNEL_ACCESS_TOKEN='tok', LINE_TARGET_ID='U123')
    def test_send_line_api_error(self):
        import urllib.error
        with patch('urllib.request.urlopen',
                   side_effect=urllib.error.HTTPError('https://x', 401, 'unauth', {}, None)):
            response = self.client.post(reverse('send_line_notification'), data='{}',
                                        content_type='application/json')
        self.assertFalse(response.json()['sent'])
        self.assertIn('401', response.json()['message'])

    def test_reports_quotations(self):
        response = self.client.get(reverse('reports_api', args=['quotations']))
        rows = response.json()['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['quotation_number'], 'QT-2026-0300')
        self.assertEqual(rows[0]['total_amount'], 100.0)
        # filters
        r = self.client.get(reverse('reports_api', args=['quotations']), {'status': 'rejected'})
        self.assertEqual(len(r.json()['rows']), 0)
        r = self.client.get(reverse('reports_api', args=['quotations']), {'q': '0300'})
        self.assertEqual(len(r.json()['rows']), 1)
        r = self.client.get(reverse('reports_api', args=['quotations']),
                            {'date_from': '2999-01-01'})
        self.assertEqual(len(r.json()['rows']), 0)
        r = self.client.get(reverse('reports_api', args=['quotations']),
                            {'date_to': '2999-01-01'})
        self.assertEqual(len(r.json()['rows']), 1)

    def test_reports_follow_ups(self):
        response = self.client.get(reverse('reports_api', args=['follow_ups']))
        rows = response.json()['rows']
        self.assertEqual(len(rows), 5)
        states = {r['state'] for r in rows}
        self.assertEqual(states, {'upcoming', 'overdue', 'completed'})
        r = self.client.get(reverse('reports_api', args=['follow_ups']), {'status': 'overdue'})
        self.assertEqual(len(r.json()['rows']), 1)
        r = self.client.get(reverse('reports_api', args=['follow_ups']), {'status': 'completed'})
        self.assertEqual(len(r.json()['rows']), 1)

    def test_reports_orders(self):
        response = self.client.get(reverse('reports_api', args=['orders']))
        rows = {r['po_number']: r for r in response.json()['rows']}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows['PO-2026-0060']['ordered_qty'], 4)
        self.assertEqual(rows['PO-2026-0060']['received_qty'], 1)
        self.assertEqual(rows['PO-2026-0060']['remaining_qty'], 3)
        self.assertTrue(rows['PO-OD-6']['is_overdue'])
        self.assertFalse(rows['PO-2026-0060']['is_overdue'])
        r = self.client.get(reverse('reports_api', args=['orders']), {'status': 'overdue'})
        self.assertEqual(len(r.json()['rows']), 1)

    def test_reports_receiving(self):
        response = self.client.get(reverse('reports_api', args=['receiving']))
        rows = response.json()['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['quantity'], 1)
        self.assertEqual(rows[0]['product_code'], 'SKU-D6')
        self.assertEqual(rows[0]['po_number'], 'PO-2026-0060')
        r = self.client.get(reverse('reports_api', args=['receiving']), {'status': 'damaged'})
        self.assertEqual(len(r.json()['rows']), 0)

    def test_reports_invalid_type(self):
        response = self.client.get(reverse('reports_api', args=['bogus']))
        self.assertEqual(response.status_code, 400)

    def test_reports_page_renders(self):
        response = self.client.get(reverse('reports'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Reports')

    def test_send_reminders_command(self):
        from django.core.management import call_command
        out = io.StringIO()
        call_command('send_reminders', '--dry-run', stdout=out)
        text = out.getvalue()
        self.assertIn('QT-2026-0300', text)
        self.assertIn('PO-OD-6', text)
        # Not configured -> warning + text
        out = io.StringIO()
        call_command('send_reminders', stdout=out)
        self.assertIn('not configured', out.getvalue())

    def test_unauthenticated_redirected(self):
        self.client.logout()
        for name, args in [('dashboard', []), ('notifications_page', []),
                           ('notifications_api', []), ('reports', []),
                           ('reports_api', ['quotations'])]:
            response = self.client.get(reverse(name, args=args))
            self.assertEqual(response.status_code, 302, name)


class EndToEndWorkflowTests(TestCase):
    """Full acceptance test — drives the real workflow through the actual
    API endpoints: Customer → Cart → Quotation → (external confirm) →
    OPEN PO → Receive (partial + full) → Dashboard/Notifications/Reports,
    then verifies cascades keep relationships intact."""

    def post_json(self, url, data):
        return self.client.post(url, data=json.dumps(data),
                                content_type='application/json')

    def test_complete_workflow(self):
        from django.utils import timezone as tz
        user = User.objects.create_user(username='e2e_staff', password='pw', role='admin')
        self.client.force_login(user)
        product = Product.objects.create(
            parent_code='E2E-P', child_code='SKU-E2E', description='E2E Product')

        # --- 1. Customer through the existing system ---
        r = self.post_json(reverse('customer_api'),
                           {'action': 'create', 'name': 'E2E Customer'})
        self.assertTrue(r.json()['success'])
        customer_id = r.json()['customer']['id']

        # --- 2. Cart: add 100 units ---
        r = self.post_json(reverse('cart_api', args=[customer_id]),
                           {'action': 'add_item', 'product_id': product.id, 'quantity': 100})
        self.assertTrue(r.json()['success'])

        # --- 3. Quotation from cart ---
        r = self.post_json(reverse('create_quotation_from_cart', args=[customer_id]),
                           {'currency': 'THB'})
        data = r.json()
        self.assertTrue(data['success'])
        quotation_id = data['quotation_id']
        self.assertRegex(data['quotation_number'], r'^QT-\d{4}-\d{4}$')
        quotation = Quotation.objects.get(id=quotation_id)
        self.assertEqual(quotation.customer_id, customer_id)
        self.assertIsNotNone(quotation.cart_id)
        self.assertEqual(quotation.status, 'draft')
        qi = quotation.items.get()
        self.assertEqual(qi.product_id, product.id)
        self.assertEqual(qi.product_code, 'SKU-E2E')
        self.assertEqual(qi.quantity, 100)

        # --- 4. Quotation document ---
        self.assertEqual(self.client.get(
            reverse('quotation_detail', args=[quotation_id])).status_code, 200)
        self.assertEqual(self.client.get(
            reverse('quotation_print', args=[quotation_id])).status_code, 200)

        # --- 5. External confirmation gate: open-po rejected while not accepted ---
        open_po_url = reverse('create_order_from_quotation', args=[quotation_id])
        r = self.post_json(open_po_url, {'expected_date': '2099-12-31'})
        self.assertEqual(r.status_code, 400)
        for st in ('sent', 'accepted'):
            r = self.post_json(reverse('quotation_api', args=[quotation_id]),
                               {'action': 'update_status', 'status': st})
            self.assertTrue(r.json()['success'], st)

        # --- 6. OPEN PO TO FACTORY ---
        r = self.post_json(open_po_url, {'expected_date': '2099-12-31'})
        data = r.json()
        self.assertTrue(data['success'])
        order_id = data['order_id']
        self.assertRegex(data['po_number'], r'^PO-\d{4}-\d{4}$')
        order = PurchaseOrder.objects.get(id=order_id)
        self.assertEqual(order.quotation_id, quotation_id)
        self.assertEqual(order.customer.name, 'E2E Customer')
        self.assertEqual(order.expected_date.strftime('%Y-%m-%d'), '2099-12-31')
        poi = order.items.get()
        self.assertEqual(poi.quotation_item_id, qi.id)
        self.assertEqual(poi.product_id, product.id)
        self.assertEqual(poi.product_code, 'SKU-E2E')
        self.assertEqual(poi.ordered_quantity, 100)
        self.assertEqual(poi.received_quantity, 0)
        self.assertEqual(poi.remaining_quantity, 100)

        # --- duplicate PO creation blocked ---
        r = self.post_json(open_po_url, {'expected_date': '2099-12-31'})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['existing_po_id'], order_id)

        # --- 7. Factory PO document + order list/detail ---
        self.assertEqual(self.client.get(
            reverse('order_detail', args=[order_id])).status_code, 200)
        self.assertEqual(self.client.get(
            reverse('order_print', args=[order_id])).status_code, 200)
        r = self.client.get(reverse('order_list_api'))
        row = [o for o in r.json()['orders'] if o['po_number'] == data['po_number']][0]
        self.assertEqual(row['ordered_qty'], 100)
        self.assertEqual(row['remaining_qty'], 100)
        order_item_id = poi.id

        # --- 8. Follow-up: overdue notification, then complete ---
        past = (tz.now() - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        r = self.post_json(reverse('follow_up_api', args=[quotation_id]),
                           {'follow_up_at': past, 'channel': 'call', 'notes': 'e2e call'})
        self.assertTrue(r.json()['success'])
        fu = quotation.follow_ups.get()
        r = self.client.get(reverse('notifications_api'))
        kinds = [n['kind'] for n in r.json()['notifications']]
        self.assertIn('follow_up_overdue', kinds)
        r = self.post_json(reverse('follow_up_detail_api', args=[fu.id]),
                           {'action': 'complete'})
        self.assertTrue(r.json()['success'])
        fu.refresh_from_db()
        self.assertTrue(fu.is_done)
        r = self.client.get(reverse('notifications_api'))
        self.assertNotIn('follow_up_overdue',
                         [n['kind'] for n in r.json()['notifications']])

        # --- 9. SKU lookup identifies the product ---
        r = self.client.get(reverse('single_product'), {'code': 'SKU-E2E'})
        self.assertTrue(r.json()['success'])
        self.assertEqual(r.json()['id'], product.id)

        # --- 10. Scenario B: partial receive 40/100 ---
        receive_url = reverse('order_receive_api', args=[order_id])
        r = self.post_json(receive_url, {'items': [
            {'order_item_id': order_item_id, 'quantity': 40, 'condition': 'good'}]})
        self.assertTrue(r.json()['success'])
        order.refresh_from_db()
        self.assertEqual(order.status, 'partially_received')
        poi.refresh_from_db()
        self.assertEqual(poi.received_quantity, 40)
        self.assertEqual(poi.remaining_quantity, 60)

        # --- invalid: over-receiving prevented ---
        r = self.post_json(receive_url, {'items': [
            {'order_item_id': order_item_id, 'quantity': 61, 'condition': 'good'}]})
        self.assertEqual(r.status_code, 400)

        # --- receive remaining 60 -> Completed ---
        r = self.post_json(receive_url, {'items': [
            {'order_item_id': order_item_id, 'quantity': 60, 'condition': 'good'}]})
        self.assertTrue(r.json()['success'])
        order.refresh_from_db()
        self.assertEqual(order.status, 'received')
        poi.refresh_from_db()
        self.assertEqual(poi.received_quantity, 100)
        self.assertEqual(poi.remaining_quantity, 0)
        self.assertEqual(order.receiving_records.count(), 2)
        self.assertEqual(sum(x.quantity for x in order.receiving_records.all()), 100)

        # --- 11. Dashboard / notifications / reports reflect the final state ---
        r = self.client.get(reverse('dashboard'))
        stats = r.context['stats']
        self.assertEqual(stats['orders_total'], 1)
        self.assertEqual(stats['orders_by_status']['received'], 1)
        self.assertEqual(stats['orders_open'], 0)
        self.assertEqual(stats['received_units'], 100)
        self.assertEqual(stats['received_records'], 2)
        r = self.client.get(reverse('notifications_api'))
        urls = [n['url'] for n in r.json()['notifications']]
        self.assertNotIn(f'/orders/{order_id}/', urls)
        r = self.client.get(reverse('reports_api', args=['orders']))
        row = r.json()['rows'][0]
        self.assertEqual(row['ordered_qty'], 100)
        self.assertEqual(row['received_qty'], 100)
        self.assertEqual(row['remaining_qty'], 0)
        self.assertEqual(row['status'], 'received')
        r = self.client.get(reverse('reports_api', args=['receiving']),
                            {'q': order.po_number})
        self.assertEqual(len(r.json()['rows']), 2)
        r = self.client.get(reverse('reports_api', args=['quotations']),
                            {'q': quotation.quotation_number})
        self.assertEqual(len(r.json()['rows']), 1)

        # --- 12. Cleanup: SET_NULL + cascades behave correctly ---
        # deleting the quotation nulls PO.quotation but keeps the order
        r = self.post_json(reverse('quotation_api', args=[quotation_id]),
                           {'action': 'delete'})
        self.assertTrue(r.json()['success'])
        order.refresh_from_db()
        self.assertIsNone(order.quotation_id)
        self.assertEqual(order.items.count(), 1)          # snapshots survive
        self.assertEqual(QuotationItem.objects.filter(id=qi.id).count(), 0)
        self.assertEqual(QuotationFollowUp.objects.filter(quotation_id=quotation_id).count(), 0)
        # deleting the order cascades its receiving records
        r = self.post_json(reverse('order_api', args=[order_id]),
                           {'action': 'delete'})
        self.assertTrue(r.json()['success'])
        self.assertEqual(ReceivingRecord.objects.filter(purchase_order_id=order_id).count(), 0)
        self.assertEqual(PurchaseOrderItem.objects.filter(purchase_order_id=order_id).count(), 0)
        # deleting the customer cascades its cart
        r = self.post_json(reverse('customer_api'),
                           {'action': 'delete', 'id': customer_id})
        self.assertTrue(r.json()['success'])
        self.assertEqual(Cart.objects.filter(customer_id=customer_id).count(), 0)
        self.assertEqual(Customer.objects.filter(id=customer_id).count(), 0)


class UserManagementTests(TestCase):
    """Users tab, roles, per-user permissions, and the / -> Dashboard route."""

    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username='root', password='Sup3r!Pass', email='root@x.com')
        self.admin_user = User.objects.create_user(
            username='boss', password='Adm1n!Pass', role='admin')
        self.staff = User.objects.create_user(
            username='stf', password='St4ff!Pass', role='staff')

    # ---- helpers ----
    def post_json(self, url, data):
        return self.client.post(url, data=json.dumps(data),
                                content_type='application/json')

    def api(self, data):
        return self.post_json(reverse('users_api'), data)

    # ---- default route ----
    def test_root_serves_dashboard(self):
        self.client.force_login(self.admin_user)
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertTemplateUsed(r, 'dashboard.html')

    def test_products_moved_to_products_url(self):
        self.client.force_login(self.admin_user)
        r = self.client.get('/products/')
        self.assertEqual(r.status_code, 200)
        self.assertTemplateUsed(r, 'form.html')

    def test_legacy_dashboard_url_redirects(self):
        self.client.force_login(self.admin_user)
        r = self.client.get('/dashboard/')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, '/')

    def test_login_redirects_to_dashboard(self):
        r = self.client.post(reverse('login'),
                             {'username': 'stf', 'password': 'St4ff!Pass'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, '/')

    # ---- users page access ----
    def test_users_page_admin_only(self):
        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.get(reverse('users')).status_code, 200)
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(reverse('users')).status_code, 200)
        self.client.force_login(self.staff)
        r = self.client.get(reverse('users'))
        # zero-permission staff land on the access-denied page (403)
        self.assertEqual(r.status_code, 403)
        self.assertTemplateUsed(r, 'access_denied.html')

    def test_users_tab_hidden_for_staff(self):
        self.staff.permissions = {'dashboard': True}
        self.staff.save()
        self.client.force_login(self.staff)
        r = self.client.get('/')
        self.assertNotContains(r, 'href="/users/"')
        self.client.force_login(self.admin_user)
        r = self.client.get('/')
        self.assertContains(r, 'href="/users/"')

    # ---- create / roles ----
    def test_create_user_defaults_to_staff_no_access(self):
        self.client.force_login(self.admin_user)
        r = self.api({'action': 'create', 'username': 'newbie',
                      'password': 'N3w!Passw0rd'})
        self.assertTrue(r.json()['success'])
        u = User.objects.get(username='newbie')
        self.assertEqual(u.role, 'staff')
        self.assertFalse(u.is_superuser)
        # every permission key explicitly stored, all denied by default
        self.assertTrue(all(v is False for v in u.permissions.values()))
        self.assertFalse(u.permissions['dashboard'])
        self.assertFalse(u.permissions['orders_receive'])

    def test_create_user_requires_password_and_unique_username(self):
        self.client.force_login(self.admin_user)
        r = self.api({'action': 'create', 'username': 'stf',
                      'password': 'Whatever!123'})
        self.assertFalse(r.json()['success'])
        r = self.api({'action': 'create', 'username': 'weakpw',
                      'password': '123'})
        self.assertFalse(r.json()['success'])  # password validator rejects

    def test_staff_cannot_manage_users(self):
        self.client.force_login(self.staff)
        r = self.api({'action': 'create', 'username': 'x',
                      'password': 'Whatever!123'})
        self.assertEqual(r.status_code, 403)

    def test_role_change_to_admin_does_not_make_superuser(self):
        self.client.force_login(self.admin_user)
        r = self.api({'action': 'update', 'user_id': self.staff.id,
                      'role': 'admin'})
        self.assertTrue(r.json()['success'])
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.role, 'admin')
        self.assertFalse(self.staff.is_superuser)

    def test_admin_cannot_modify_self_or_superuser(self):
        self.client.force_login(self.admin_user)
        r = self.api({'action': 'update', 'user_id': self.admin_user.id,
                      'role': 'staff'})
        self.assertEqual(r.status_code, 400)
        r = self.api({'action': 'update', 'user_id': self.superuser.id,
                      'role': 'staff'})
        self.assertEqual(r.status_code, 400)

    def test_set_password(self):
        self.client.force_login(self.admin_user)
        r = self.api({'action': 'set_password', 'user_id': self.staff.id,
                      'password': 'Br4nd!NewPw'})
        self.assertTrue(r.json()['success'])
        self.assertTrue(self.client.login(username='stf',
                                          password='Br4nd!NewPw'))

    def test_deactivated_user_cannot_login(self):
        self.client.force_login(self.admin_user)
        r = self.api({'action': 'update', 'user_id': self.staff.id,
                      'role': 'staff', 'is_active': False})
        self.assertTrue(r.json()['success'])
        other = self.client.__class__()
        self.assertFalse(other.login(username='stf', password='St4ff!Pass'))

    # ---- permission enforcement ----
    def test_denied_tab_redirects_to_first_allowed(self):
        self.staff.permissions = {k: False for k in
                                  ['dashboard', 'products', 'cart',
                                   'quotations', 'orders', 'notifications',
                                   'reports']}
        self.staff.permissions['reports'] = True
        self.staff.save()
        self.client.force_login(self.staff)
        r = self.client.get('/')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, '/reports/')

    def test_denied_action_returns_403(self):
        # zero-permission staff (default) cannot mutate OR view
        self.client.force_login(self.staff)
        customer = Customer.objects.create(name='PermCust')
        r = self.post_json(reverse('cart_api', args=[customer.id]),
                           {'action': 'add_item', 'product_id': 1,
                            'quantity': 1})
        self.assertEqual(r.status_code, 403)
        r = self.post_json(reverse('customer_api'),
                           {'action': 'delete', 'id': customer.id})
        self.assertEqual(r.status_code, 403)
        # ... and cannot view the tab either
        self.assertEqual(self.client.get('/cart-management/').status_code, 403)

    def test_granted_action_allowed(self):
        self.staff.permissions = {'cart': True, 'cart_edit': True}
        self.staff.save()
        self.client.force_login(self.staff)
        customer = Customer.objects.create(name='PermCust2')
        product = Product.objects.create(parent_code='PP', child_code='PP-1')
        r = self.post_json(reverse('cart_api', args=[customer.id]),
                           {'action': 'add_item', 'product_id': product.id,
                            'quantity': 2})
        self.assertTrue(r.json()['success'])

    def test_sidebar_hides_denied_tabs(self):
        self.staff.permissions = {'dashboard': True, 'reports': True,
                                  'orders': False, 'quotations': False}
        self.staff.save()
        self.client.force_login(self.staff)
        r = self.client.get('/')
        self.assertNotContains(r, 'href="/orders/"')
        self.assertNotContains(r, 'href="/quotations/"')
        self.assertContains(r, 'href="/reports/"')

    def test_zero_perm_staff_denied_everywhere(self):
        """Staff with no granted permissions reach only the 403 page."""
        self.client.force_login(self.staff)  # default: all permissions False
        for url in ('/', '/products/', '/quotations/', '/orders/',
                    '/cart-management/', '/notifications/', '/reports/'):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 403, f'{url} should be denied')
        # APIs denied too (JSON 403)
        r = self.client.post('/api/orders/1/',
                             data=json.dumps({'action': 'update'}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 403)

    def test_single_product_requires_relevant_perm(self):
        Product.objects.create(parent_code='P', child_code='SKU-1')
        url = reverse('single_product')
        # zero-perm staff denied
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(url, {'code': 'SKU-1'}).status_code, 403)
        # receiving staff allowed
        self.staff.permissions = {'orders_receive': True}
        self.staff.save()
        r = self.client.get(url, {'code': 'SKU-1'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['child_code'], 'SKU-1')

    # ---- product API endpoints used by the Products page ----
    def test_product_api_returns_existing_products(self):
        Product.objects.create(parent_code='P', child_code='SKU-9')
        self.client.force_login(self.admin_user)
        r = self.client.get(reverse('product_api'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['results'][0]['child_code'], 'SKU-9')
        # the old relative call (page at /products/ + 'product_api?...')
        # resolved to this path and 404'd - it must stay a 404
        self.assertEqual(self.client.get('/products/product_api').status_code, 404)

    def test_individual_product_fetch_on_products_url(self):
        # get_single_data() must hit form_view's individual branch on
        # /products/, not '/' (which is the dashboard now)
        p = Product.objects.create(parent_code='PX', child_code='PX-1')
        self.client.force_login(self.admin_user)
        r = self.client.get('/products/',
                            {'pro_id': p.id, 'get_req_type': 'individual'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'].split(';')[0], 'application/json')
        self.assertEqual(r.json()['parent_code'], 'PX')

    # ---- UI hiding: unauthorized controls must not be rendered ----

    def test_products_page_hides_denied_panes_and_buttons(self):
        # products view-only staff: no add/import/price panes, no delete/export
        self.staff.permissions = {'products': True}
        self.staff.save()
        self.client.force_login(self.staff)
        r = self.client.get('/products/')
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'id="navpills-1"')
        self.assertNotContains(r, 'id="navpills-2"')
        self.assertNotContains(r, 'id="navpills-3"')
        self.assertNotContains(r, 'id="navpills-4"')
        self.assertNotContains(r, 'Delete Table')
        self.assertNotContains(r, 'id="delete_product"')
        self.assertNotContains(r, 'select-all')
        # PERMS injected for JS row gating
        self.assertContains(r, 'perms-data')
        # granted pane still renders
        self.staff.permissions = {'products': True, 'products_edit': True}
        self.staff.save()
        r = self.client.get('/products/')
        self.assertContains(r, 'id="navpills-1"')

    def test_cart_pages_hide_denied_controls(self):
        from Dashboard.models import Customer
        customer = Customer.objects.create(name='HideTest')
        self.staff.permissions = {'cart': True}
        self.staff.save()
        self.client.force_login(self.staff)
        # cart management: no add-customer card, no modals
        r = self.client.get('/cart-management/')
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'id="addCustomerBtn"')
        self.assertNotContains(r, 'id="editCustomerModal"')
        # customer cart: no import/quotation/add-to-cart/clear, readonly inputs
        r = self.client.get(f'/customer/{customer.id}/cart/')
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'id="importBtn"')
        self.assertNotContains(r, 'id="createQuotationBtn"')
        self.assertNotContains(r, 'id="addToCartBtn"')
        self.assertNotContains(r, 'id="clearCartBtn"')
        self.assertNotContains(r, 'id="saveCartDetailsBtn"')
        self.assertContains(r, 'readonly')
        # cart_edit restores the controls
        self.staff.permissions = {'cart': True, 'cart_edit': True}
        self.staff.save()
        r = self.client.get(f'/customer/{customer.id}/cart/')
        self.assertContains(r, 'id="importBtn"')
        self.assertContains(r, 'id="addToCartBtn"')
        self.assertNotContains(r, 'id="createQuotationBtn"')  # needs quotations_create

    def test_quotation_detail_hides_denied_controls(self):
        from Dashboard.models import Customer, Quotation
        customer = Customer.objects.create(name='QHide')
        q = Quotation.objects.create(customer=customer, status='draft',
                                     quotation_number='QT-HIDE-0001',
                                     created_by=self.admin_user)
        self.staff.permissions = {'quotations': True}
        self.staff.save()
        self.client.force_login(self.staff)
        r = self.client.get(f'/quotations/{q.id}/')
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'id="saveQuotationBtn"')
        self.assertNotContains(r, 'id="addItemBtn"')
        self.assertNotContains(r, 'id="uploadDocumentBtn"')
        self.assertNotContains(r, 'id="newFollowUpBtn"')
        self.assertNotContains(r, 'id="openPoBtn"')
        self.assertContains(r, 'readonly')  # info fields readable but not editable
        # customer-cart link hidden without the cart tab perm
        self.assertNotContains(r, f'/customer/{customer.id}/cart/')

    def test_order_detail_and_dashboard_sections_hidden(self):
        from Dashboard.models import Customer, Quotation, PurchaseOrder
        customer = Customer.objects.create(name='OH')
        q = Quotation.objects.create(customer=customer, status='accepted',
                                     quotation_number='QT-HIDE-0002',
                                     created_by=self.admin_user)
        po = PurchaseOrder.objects.create(
            po_number='PO-HIDE-0001', quotation=q,
            order_date='2020-01-01', expected_date='2030-01-01',
            status='ordered', created_by=self.admin_user)
        self.staff.permissions = {'orders': True}
        self.staff.save()
        self.client.force_login(self.staff)
        r = self.client.get(f'/orders/{po.id}/')
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'id="saveOrderBtn"')
        self.assertNotContains(r, 'id="receiveModal"')
        self.assertContains(r, 'readonly')
        # Related-quotation link hidden without quotations perm
        self.assertNotContains(r, '/quotations/')
        # dashboard-only staff: no stat sections
        self.staff.permissions = {'dashboard': True}
        self.staff.save()
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'Total Quotations')
        self.assertNotContains(r, 'Total Orders')
        self.assertNotContains(r, 'Needs Attention')

    def test_quotation_and_order_lists_hide_delete(self):
        self.staff.permissions = {'quotations': True, 'orders': True}
        self.staff.save()
        self.client.force_login(self.staff)
        r = self.client.get('/quotations/')
        self.assertNotContains(r, 'id="deleteQuotationModal"')
        r = self.client.get('/orders/')
        self.assertNotContains(r, 'id="deleteOrderModal"')

    def test_admin_sees_all_controls(self):
        self.client.force_login(self.admin_user)
        r = self.client.get('/products/')
        self.assertContains(r, 'id="navpills-1"')
        self.assertContains(r, 'Delete Table')
        r = self.client.get('/cart-management/')
        self.assertContains(r, 'addCustomerBtn')
        r = self.client.get('/')
        self.assertContains(r, 'Total Quotations')
