import csv
import io
import imaplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from invoice_mail import (amount_from_text, collect, eligible_for_delete,
                          import_manual_pdfs, load_settings, move_to_deleted,
                          parse_day, process_message, save_settings, search_window)


class InvoiceMailTests(unittest.TestCase):
    def test_amount_requires_explicit_total(self):
        self.assertEqual(amount_from_text('税率 6% 单价 99.00 价税合计（大写）叁拾伍元整（小写）¥35.00'), '35.00')
        self.assertEqual(amount_from_text('<TotalAmount>5.00</TotalAmount>'), '5.00')
        self.assertIsNone(amount_from_text('订单原价 99.00 优惠 10.00'))
        self.assertIsNone(amount_from_text('价税合计 10.00 价税合计 11.00'))
        self.assertEqual(amount_from_text('价 税 合 计 （ 小 写 ） ¥ 40 · 00'), '40.00')

    def test_date_range_searches_across_year_boundary(self):
        self.assertEqual(search_window(parse_day('2026-12-31'), parse_day('2027-01-02')),
                         ('30-Dec-2026', '04-Jan-2027'))
        with self.assertRaises(ValueError):
            search_window(parse_day('2026-09-02'), parse_day('2026-09-01'))

    def test_settings_remember_account_and_dates_without_authorization_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = Path(temporary) / 'settings.json'
            with patch('invoice_mail.SETTINGS_PATH', settings_path):
                save_settings('other@qq.com', parse_day('2027-01-01'),
                              parse_day('2027-01-31'), Path(temporary), False)
                settings = load_settings()
            self.assertEqual(settings['email'], 'other@qq.com')
            self.assertEqual(settings['start'], '2027-01-01')
            self.assertFalse(settings['delete_mail'])
            self.assertNotIn('auth', settings_path.read_text(encoding='utf-8').lower())

    def test_invoice_threshold_and_qr_separation(self):
        now = datetime(2026, 8, 10, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            mail = EmailMessage()
            mail['Subject'] = '电子发票'
            mail['From'] = 'billing@example.com'
            mail.set_content('附件是发票。')
            mail.add_attachment(b'%PDF-fake', maintype='application',
                                subtype='pdf', filename='invoice.pdf')
            with patch('invoice_mail.extract_invoice_text', return_value='价税合计¥5.01'):
                rows = process_message(mail.as_bytes(), now, 'INBOX', '1', output)
            self.assertEqual(rows[0]['status'], '已归档')
            self.assertTrue(Path(rows[0]['saved_file']).exists())
            self.assertTrue(eligible_for_delete(rows))

            qr = EmailMessage()
            qr['Subject'] = '开票二维码'
            qr['From'] = 'billing@example.com'
            qr.set_content('请扫码开票。')
            qr.add_attachment(b'fake-image', maintype='image', subtype='png', filename='code.png')
            qr_rows = process_message(qr.as_bytes(), now, 'INBOX', '2', output)
            self.assertEqual(qr_rows[0]['status'], '二维码待扫码')
            self.assertTrue((Path(qr_rows[0]['saved_file']) / '原始邮件.eml').exists())
            self.assertFalse(eligible_for_delete(qr_rows))

    def test_linked_zip_extracts_pdf_and_merchant_image_goes_to_qr_queue(self):
        message = EmailMessage()
        message['Subject'] = '电子发票文件'
        message['From'] = 'billing@example.com'
        message.set_content('电子发票已开具。')
        message.add_alternative(
            '<p>商家发票：<a href="https://example.com/merchant.png">查看电子发票文件</a></p>'
            '<p>平台发票：<a href="https://example.com/platform.zip">查看电子发票文件</a></p>',
            subtype='html')
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as file:
            file.writestr('subdir/invoice.pdf', b'%PDF-fake-test-content')
        def fetcher(url):
            return archive.getvalue() if url.endswith('.zip') else b'\x89PNG\r\n\x1a\nimage'
        now = datetime(2026, 8, 10, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as temporary:
            with patch('invoice_mail.extract_invoice_text', return_value='价税合计（小写）¥12.00'):
                rows = process_message(message.as_bytes(), now, 'INBOX', '3',
                                       Path(temporary), fetcher=fetcher)
            self.assertEqual({row['status'] for row in rows}, {'已归档', '二维码待扫码'})
            pdf_row = next(row for row in rows if row['status'] == '已归档')
            self.assertEqual(Path(pdf_row['saved_file']).suffix, '.pdf')
            self.assertTrue(Path(pdf_row['saved_file']).exists())
            self.assertFalse(eligible_for_delete(rows))

    def test_invoice_webpage_is_kept_for_manual_pdf_download(self):
        message = EmailMessage()
        message['Subject'] = '您的发票已成功开具'
        message.set_content('查看发票。')
        message.add_alternative(
            '<a href="https://nnfp.jss.com.cn/example">点击链接查看发票</a>',
            subtype='html')
        now = datetime(2026, 8, 10, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as temporary:
            rows = process_message(message.as_bytes(), now, 'INBOX', '4', Path(temporary))
            self.assertEqual([row['status'] for row in rows], ['网页下载待处理'])
            self.assertFalse(eligible_for_delete(rows))
            self.assertTrue((Path(rows[0]['saved_file']) / '打开链接.html').exists())

    def test_manually_downloaded_pdf_can_be_imported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            downloaded = root / 'downloaded.pdf'
            downloaded.write_bytes(b'%PDF-fake-test-content')
            with patch('invoice_mail.extract_invoice_text', return_value='价税合计（小写）￥290.64'):
                rows = import_manual_pdfs([downloaded], root / 'result')
            self.assertEqual(rows[0]['status'], '已归档')
            self.assertEqual(rows[0]['amount_cny'], '290.64')
            self.assertTrue(Path(rows[0]['saved_file']).is_file())

    def test_png_invoice_is_saved_for_review_not_qr_or_deletion(self):
        message = EmailMessage()
        message['Subject'] = '电子发票'
        message.set_content('请查收电子发票。')
        message.add_attachment(b'\x89PNG\r\n\x1a\nimage', maintype='image',
                               subtype='png', filename='invoice.png')
        now = datetime(2026, 8, 10, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as temporary:
            with patch('invoice_mail.windows_ocr_text', return_value=''):
                rows = process_message(message.as_bytes(), now, 'INBOX', '8', Path(temporary))
            self.assertEqual([row['status'] for row in rows], ['待核对'])
            self.assertTrue(Path(rows[0]['saved_file']).is_file())
            self.assertEqual(Path(rows[0]['saved_file']).suffix, '.png')
            self.assertFalse(eligible_for_delete(rows))

    def test_png_in_zip_and_direct_link_are_saved_for_review(self):
        message = EmailMessage()
        message['Subject'] = '发票文件'
        message.set_content('请查收。')
        message.add_alternative('<a href="https://example.com/invoice.png">下载发票图片</a>',
                                subtype='html')
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as file:
            file.writestr('invoice.png', b'\x89PNG\r\n\x1a\nzip-image')
        message.add_attachment(archive.getvalue(), maintype='application',
                               subtype='zip', filename='invoice.zip')
        now = datetime(2026, 8, 10, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as temporary:
            with patch('invoice_mail.windows_ocr_text', return_value=''):
                rows = process_message(message.as_bytes(), now, 'INBOX', '9', Path(temporary),
                                       fetcher=lambda _url: b'\x89PNG\r\n\x1a\nlinked-image')
            self.assertEqual([row['status'] for row in rows], ['待核对', '待核对'])
            self.assertTrue(all(Path(row['saved_file']).is_file() for row in rows))

    def test_pdf_xml_ofd_bundle_only_handles_pdf(self):
        message = EmailMessage()
        message['Subject'] = '电子发票'
        message.set_content('请查收附件。')
        message.add_attachment(b'%PDF-fake', maintype='application',
                               subtype='pdf', filename='发票.pdf')
        message.add_attachment(b'<Invoice/>', maintype='application',
                               subtype='xml', filename='发票.xml')
        message.add_attachment(b'fake-ofd', maintype='application',
                               subtype='octet-stream', filename='发票.ofd')
        now = datetime(2026, 8, 10, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as temporary:
            with patch('invoice_mail.extract_invoice_text',
                       side_effect=lambda name, _data: '价税合计（小写）¥12.00'
                       if name.endswith('.pdf') else self.fail('XML/OFD 不应解析')):
                rows = process_message(message.as_bytes(), now, 'INBOX', '10', Path(temporary))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['status'], '已归档')
            self.assertEqual(Path(rows[0]['saved_file']).suffix, '.pdf')
            self.assertTrue(eligible_for_delete(rows))

    def test_low_amount_mail_is_deletable_but_exactly_five_is_kept(self):
        message = EmailMessage()
        message['Subject'] = '电子发票'
        message.set_content('请查收。')
        message.add_attachment(b'%PDF-fake', maintype='application',
                               subtype='pdf', filename='invoice.pdf')
        now = datetime(2026, 8, 10, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as temporary:
            with patch('invoice_mail.extract_invoice_text', return_value='价税合计¥4.99'):
                low = process_message(message.as_bytes(), now, 'INBOX', '11', Path(temporary))
            self.assertEqual(low[0]['status'], '低于5元')
            self.assertEqual(low[0]['saved_file'], '')
            self.assertTrue(eligible_for_delete(low))
            with patch('invoice_mail.extract_invoice_text', return_value='价税合计¥5.00'):
                exact = process_message(message.as_bytes(), now, 'INBOX', '12', Path(temporary))
            self.assertEqual(exact[0]['status'], '等于5元')
            self.assertFalse(eligible_for_delete(exact))

    def test_xml_only_mail_is_kept_without_local_file(self):
        message = EmailMessage()
        message['Subject'] = '电子发票'
        message.set_content('请查收附件。')
        message.add_attachment(b'<Invoice/>', maintype='application',
                               subtype='xml', filename='发票.xml')
        now = datetime(2026, 8, 10, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as temporary:
            rows = process_message(message.as_bytes(), now, 'INBOX', '13', Path(temporary))
            self.assertEqual(rows[0]['status'], '仅XML/OFD，邮件已保留')
            self.assertEqual(rows[0]['saved_file'], '')
            self.assertFalse(eligible_for_delete(rows))

    def test_manually_downloaded_png_is_saved_for_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            downloaded = root / '发票.png'
            downloaded.write_bytes(b'\x89PNG\r\n\x1a\nimage')
            with patch('invoice_mail.windows_ocr_text', return_value=''):
                rows = import_manual_pdfs([downloaded], root / 'result')
            self.assertEqual(rows[0]['status'], '待核对')
            self.assertTrue(Path(rows[0]['saved_file']).is_file())

    def test_png_ocr_archives_full_invoice_with_clear_total(self):
        message = EmailMessage()
        message['Subject'] = '电子发票'
        message.set_content('请查收图片发票。')
        message.add_attachment(b'\x89PNG\r\n\x1a\nimage', maintype='image',
                               subtype='png', filename='发票.png')
        ocr_text = ('电 子 发 票 （ 普 通 发 票 ）\n'
                    '发 票 号 码 ： 99990000123456789012\n'
                    '购 买 方 ： 测 试 公 司\n销 售 方 ： 测 试 商 店\n'
                    '价 税 合 计 （ 小 写 ） ¥ 40 · 00')
        now = datetime(2026, 8, 10, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as temporary:
            with patch('invoice_mail.windows_ocr_text', return_value=ocr_text):
                rows = process_message(message.as_bytes(), now, 'INBOX', '14', Path(temporary))
            self.assertEqual(rows[0]['status'], '已归档')
            self.assertEqual(rows[0]['amount_cny'], '40.00')
            self.assertTrue(Path(rows[0]['saved_file']).is_file())
            self.assertTrue(eligible_for_delete(rows))

    def test_qr_image_amount_does_not_count_as_full_invoice(self):
        message = EmailMessage()
        message['Subject'] = '电子发票'
        message.set_content('请查收图片。')
        message.add_attachment(b'\x89PNG\r\n\x1a\nimage', maintype='image',
                               subtype='png', filename='发票.png')
        now = datetime(2026, 8, 10, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as temporary:
            with patch('invoice_mail.windows_ocr_text',
                       return_value='二维码\n订单编号 123456\n发票金额 ¥13.50'):
                rows = process_message(message.as_bytes(), now, 'INBOX', '15', Path(temporary))
            self.assertEqual(rows[0]['status'], '待核对')
            self.assertFalse(eligible_for_delete(rows))

    def test_uncertain_mail_is_never_deleted(self):
        self.assertFalse(eligible_for_delete([{'status': '待核对', 'saved_file': ''}]))
        self.assertFalse(eligible_for_delete([{'status': '等于5元', 'saved_file': ''}]))
        self.assertFalse(eligible_for_delete([{'status': '已归档', 'saved_file': 'missing'},
                                              {'status': '待核对', 'saved_file': ''}]))

    def test_delete_targets_only_selected_uid(self):
        class Mailbox:
            capabilities = (b'UIDPLUS',)

            def __init__(self):
                self.calls = []

            def uid(self, *args):
                self.calls.append(args)
                return 'OK', []

        connection = Mailbox()
        method = move_to_deleted(connection, b'42', b'"Deleted Messages"')
        self.assertEqual(method, 'UID EXPUNGE')
        self.assertEqual(connection.calls, [
            ('COPY', b'42', b'"Deleted Messages"'),
            ('STORE', b'42', '+FLAGS.SILENT', r'(\Deleted)'),
            ('EXPUNGE', b'42'),
        ])

    def test_delete_without_extensions_checks_deleted_flags_before_expunge(self):
        class Mailbox:
            capabilities = (b'IMAP4rev1',)

            def __init__(self):
                self.calls = []
                self.deleted = set()

            def uid(self, *args):
                self.calls.append(args)
                if args[0] == 'SEARCH':
                    return 'OK', [b' '.join(sorted(self.deleted))]
                if args[0] == 'STORE':
                    if args[2] == '+FLAGS.SILENT':
                        self.deleted.add(args[1])
                    else:
                        self.deleted.discard(args[1])
                return 'OK', []

            def expunge(self):
                self.calls.append(('EXPUNGE',))
                self.deleted.clear()
                return 'OK', []

        connection = Mailbox()
        method = move_to_deleted(connection, b'42', b'"Deleted Messages"')
        self.assertEqual(method, '兼容模式 EXPUNGE')
        self.assertEqual(connection.calls, [
            ('SEARCH', None, 'DELETED'),
            ('COPY', b'42', b'"Deleted Messages"'),
            ('STORE', b'42', '+FLAGS.SILENT', r'(\Deleted)'),
            ('SEARCH', None, 'DELETED'),
            ('EXPUNGE',),
        ])

    def test_delete_without_extensions_refuses_unrelated_deleted_mail(self):
        class Mailbox:
            capabilities = (b'IMAP4rev1',)

            def __init__(self):
                self.calls = []

            def uid(self, *args):
                self.calls.append(args)
                return 'OK', [b'99'] if args[0] == 'SEARCH' else []

            def expunge(self):
                raise AssertionError('must not expunge unrelated mail')

        connection = Mailbox()
        with self.assertRaisesRegex(RuntimeError, '其他已标记删除'):
            move_to_deleted(connection, b'42', b'"Deleted Messages"')
        self.assertEqual(connection.calls, [('SEARCH', None, 'DELETED')])

    def test_delete_without_extensions_restores_flag_when_guard_fails(self):
        class Mailbox:
            capabilities = (b'IMAP4rev1',)

            def __init__(self):
                self.calls = []
                self.search_count = 0

            def uid(self, *args):
                self.calls.append(args)
                if args[0] == 'SEARCH':
                    self.search_count += 1
                    return 'OK', [b'' if self.search_count == 1 else b'42 99']
                return 'OK', []

            def expunge(self):
                raise AssertionError('must not expunge unrelated mail')

        connection = Mailbox()
        with self.assertRaisesRegex(RuntimeError, '删除标记出现其他邮件'):
            move_to_deleted(connection, b'42', b'"Deleted Messages"')
        self.assertEqual(connection.calls[-1],
                         ('STORE', b'42', '-FLAGS.SILENT', r'(\Deleted)'))

    def test_delete_without_extensions_restores_flag_when_expunge_fails(self):
        class Mailbox:
            capabilities = (b'IMAP4rev1',)

            def __init__(self):
                self.calls = []
                self.deleted = set()

            def uid(self, *args):
                self.calls.append(args)
                if args[0] == 'SEARCH':
                    return 'OK', [b' '.join(self.deleted)]
                if args[0] == 'STORE':
                    if args[2] == '+FLAGS.SILENT':
                        self.deleted.add(args[1])
                    else:
                        self.deleted.discard(args[1])
                return 'OK', []

            def expunge(self):
                self.calls.append(('EXPUNGE',))
                return 'NO', []

        connection = Mailbox()
        with self.assertRaisesRegex(RuntimeError, '已撤销原邮件的删除标记'):
            move_to_deleted(connection, b'42', b'"Deleted Messages"')
        self.assertEqual(connection.deleted, set())
        self.assertEqual(connection.calls[-1],
                         ('STORE', b'42', '-FLAGS.SILENT', r'(\Deleted)'))

    def test_collect_saves_report_before_deleting_only_completed_invoice(self):
        invoice = EmailMessage()
        invoice['Subject'] = '电子发票'
        invoice['From'] = 'billing@example.com'
        invoice.set_content('请查收附件。')
        invoice.add_alternative(
            '<p>发票号码：99990000123456789012，扫描二维码查看发票。</p>'
            '<a href="https://a.51fapiao.cn/v/example">点击下载此发票</a>',
            subtype='html')
        invoice.add_attachment(b'%PDF-fake', maintype='application',
                               subtype='pdf', filename='99990000123456789012.pdf')
        invoice.add_attachment(b'<Invoice/>', maintype='application',
                               subtype='xml', filename='99990000123456789012.xml')
        invoice.add_attachment(b'fake-ofd', maintype='application',
                               subtype='octet-stream', filename='99990000123456789012.ofd')
        invoice.add_attachment(b'not-a-zip', maintype='application',
                               subtype='zip', filename='99990000123456789012.zip')
        low_invoice = EmailMessage()
        low_invoice['Subject'] = '电子发票'
        low_invoice['From'] = 'billing@example.com'
        low_invoice.set_content('请查收附件。')
        low_invoice.add_attachment(b'%PDF-low', maintype='application',
                                   subtype='pdf', filename='small-invoice.pdf')
        qr = EmailMessage()
        qr['Subject'] = '开票二维码'
        qr['From'] = 'billing@example.com'
        qr.set_content('请扫码开票。')

        class Mailbox:
            capabilities = (b'MOVE',)

            def __init__(self):
                self.calls = []
                self.messages = {b'1': invoice.as_bytes(), b'2': qr.as_bytes(),
                                 b'3': low_invoice.as_bytes()}

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def login(self, *_):
                return 'OK', []

            def list(self):
                return 'OK', [b'(\\HasNoChildren) "/" "INBOX"',
                              b'(\\HasNoChildren \\Trash) "/" "Deleted Messages"']

            def select(self, *_args, **_kwargs):
                return 'OK', []

            def uid(self, command, *args):
                self.calls.append((command, *args))
                if command == 'SEARCH':
                    return 'OK', [b'1 2 3']
                if command == 'FETCH':
                    uid = args[0]
                    return 'OK', [(b'1 (INTERNALDATE "10-Aug-2026 09:00:00 +0800" BODY[] {1}',
                                   self.messages[uid]), b')']
                if command == 'MOVE':
                    return 'OK', []
                raise AssertionError(command)

        mailbox = Mailbox()
        authenticated = []
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with patch('invoice_mail.imaplib.IMAP4_SSL', return_value=mailbox), \
                 patch('invoice_mail.extract_invoice_text',
                       side_effect=lambda _name, data: '价税合计¥4.00' if data == b'%PDF-low'
                       else '价税合计¥6.00'):
                rows = collect('test@qq.com', 'not-a-real-secret', output,
                               parse_day('2026-08-01'), parse_day('2026-09-30'),
                               on_authenticated=lambda: authenticated.append(True))
            self.assertEqual(authenticated, [True])
            self.assertEqual({row['status'] for row in rows},
                             {'已归档', '二维码待扫码', '低于5元'})
            self.assertIn(('SEARCH', None, 'SINCE', '31-Jul-2026',
                           'BEFORE', '02-Oct-2026'), mailbox.calls)
            self.assertEqual([call for call in mailbox.calls if call[0] == 'MOVE'],
                             [('MOVE', b'1', b'"Deleted Messages"'),
                              ('MOVE', b'3', b'"Deleted Messages"')])
            self.assertEqual([path.suffix for path in (output / '发票_大于5元').iterdir()],
                             ['.pdf'])
            with (output / '删除状态.csv').open(encoding='utf-8-sig', newline='') as file:
                deletion_rows = list(csv.DictReader(file))
            self.assertEqual(len(deletion_rows), 2)
            self.assertTrue(all(row['result'] == '已移到已删除' for row in deletion_rows))
            with (output / '处理清单.csv').open(encoding='utf-8-sig', newline='') as file:
                self.assertEqual(len(list(csv.DictReader(file))), 3)

    def test_authorization_code_is_not_saved_if_login_fails(self):
        class Mailbox:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def login(self, *_args):
                raise imaplib.IMAP4.error('login rejected')

        authenticated = []
        with tempfile.TemporaryDirectory() as temporary:
            with patch('invoice_mail.imaplib.IMAP4_SSL', return_value=Mailbox()):
                with self.assertRaises(imaplib.IMAP4.error):
                    collect('test@qq.com', 'invalid-code', Path(temporary),
                            parse_day('2026-08-01'), parse_day('2026-08-31'),
                            on_authenticated=lambda: authenticated.append(True))
        self.assertEqual(authenticated, [])


if __name__ == '__main__':
    unittest.main()
