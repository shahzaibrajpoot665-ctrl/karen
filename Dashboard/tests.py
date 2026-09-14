from django.test import TestCase

# Create your tests here.

import io
import os
import tempfile
from unittest.mock import patch, MagicMock

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from openpyxl import Workbook
from PIL import Image as PillowImage

from .models import Image, Product, ProductImageLink, User, GoogleSheetConfig
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
        self.client.force_login(User.objects.create_user(username='importer'))
        for urls, expected in [('https://images.example/a.png,https://images.example/b.png', 2),
                               ('https://images.example/b.png', 1), ('', 0)]:
            upload = SimpleUploadedFile('products.xlsx', self.workbook(urls))
            response = self.client.post(reverse('form'), {'type': 'bulk-create', 'xlsx_file': upload})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.product.images.count(), expected)

    def test_google_entry_dispatches_shared_worker_with_origin(self):
        self.client.force_login(User.objects.create_user(username='sheet-importer'))
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
