"""Collect QQ Mail invoices for a selected inclusive date range."""

from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import html
from html.parser import HTMLParser
import imaplib
import io
import json
import os
import ipaddress
import socket
import subprocess
import tempfile
import unicodedata
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
import re
import sys
import zipfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from decimal import Decimal
from typing import Callable
from xml.etree import ElementTree

_vendor = Path(__file__).parent / 'vendor'
if _vendor.is_dir():
    sys.path.insert(0, str(_vendor))

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None


HONG_KONG = timezone(timedelta(hours=8))
SETTINGS_PATH = Path(__file__).with_name('settings.json')
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
          'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
INTERNAL_DATE = re.compile(rb'INTERNALDATE "([^"]+)"')
AMOUNT = re.compile(r'(?<!\d)(?:[¥￥]\s*)?(\d{1,3}(?:,\d{3})*|\d+)\.([0-9]{2})(?!\d)')
QR_WORDS = re.compile(r'开票二维码|发票二维码|扫码开票|扫码申请开票|开票码|二维码开票')
INVOICE_WORDS = re.compile(r'发票|电子票|数电票|开票|invoice', re.I)
URL = re.compile(r'https?://[^\s<>"\']+', re.I)
DIRECT_EXTENSIONS = {'.pdf', '.zip', '.png', '.jpg', '.jpeg'}
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024
MAX_ZIP_MEMBERS = 60
PROXY_DNS_RANGE = ipaddress.ip_network('198.18.0.0/15')
CSV_FIELDS = ['status', 'received_hk', 'folder', 'uid', 'subject', 'sender',
              'attachment', 'amount_cny', 'saved_file', 'note']
DELETE_FIELDS = ['received_hk', 'folder', 'uid', 'subject', 'result', 'note']


class TextHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text: list[str] = []
        self.links: list[str] = []
        self.link_details: list[tuple[str, str, str]] = []
        self._active_href: str | None = None
        self._active_text: list[str] = []
        self._active_context = ''

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._active_href:
            self._active_text.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == 'a':
            href = dict(attrs).get('href')
            if href and href.startswith(('https://', 'http://')):
                self.links.append(href)
                self._active_href = html.unescape(href)
                self._active_text = []
                self._active_context = ' '.join(self.text[-5:])[-180:]

    def handle_endtag(self, tag: str) -> None:
        if tag == 'a' and self._active_href:
            self.link_details.append((self._active_href,
                                      ' '.join(self._active_text).strip(),
                                      self._active_context.strip()))
            self._active_href = None


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')
    return name[:120] or 'attachment'


def amount_from_text(raw: str) -> str | None:
    """Accept explicit invoice total labels only; an arbitrary price is unsafe."""
    text = normalize_amount_text(raw)
    candidates: set[str] = set()
    for match in re.finditer(r'价税合计|税价合计|合计金额|小写金额|发票金额', text):
        segment = text[match.end():match.end() + 100]
        # A second label or table row must not leak into this total.
        segment = re.split(r'价税合计|税价合计|合计金额|小写金额|发票金额|备注|销售方', segment)[0]
        small = re.search(r'小写[）):：]*', segment)
        if small:
            segment = segment[small.end():]
        found = AMOUNT.search(segment)
        if found:
            candidates.add(f'{int(found.group(1).replace(",", ""))}.{found.group(2)}')
    for match in re.finditer(r'<(?:TotalAmount|AmountWithTax|TaxInclusiveAmount)>\s*', text, re.I):
        found = AMOUNT.match(text, match.end())
        if found:
            candidates.add(f'{int(found.group(1).replace(",", ""))}.{found.group(2)}')
    return next(iter(candidates)) if len(candidates) == 1 else None


def normalize_amount_text(raw: str) -> str:
    text = unicodedata.normalize('NFKC', html.unescape(raw))
    text = re.sub(r'\s+', '', text)
    # Windows OCR can render the decimal point as a centered dot.
    return re.sub(r'(?<=\d)[·•∙⋅。](?=\d{2}(?!\d))', '.', text)


def image_amount_from_text(raw: str) -> str | None:
    """Require an OCR total and amount on the same or immediately following line."""
    lines = [normalize_amount_text(line) for line in raw.splitlines()]
    candidates: set[str] = set()
    for index, line in enumerate(lines):
        label = re.search(r'价税合计|税价合计|合计金额|小写金额', line)
        if not label:
            continue
        tail = line[label.end():]
        small = re.search(r'小写[）):：]*', tail)
        if small:
            tail = tail[small.end():]
        found = AMOUNT.search(tail)
        if not found and index + 1 < len(lines):
            following = lines[index + 1]
            if re.fullmatch(r'[¥￥]?\d[\d,]*\.\d{2}', following):
                found = AMOUNT.search(following)
        if found:
            candidates.add(f'{int(found.group(1).replace(",", ""))}.{found.group(2)}')
    return next(iter(candidates)) if len(candidates) == 1 else None


def extract_invoice_text(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {'.png', '.jpg', '.jpeg'}:
        return windows_ocr_text(suffix, data)
    if suffix == '.pdf':
        if PdfReader is None:
            raise RuntimeError('缺少 pypdf，请运行 python -m pip install -r requirements.txt')
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ValueError('PDF 已加密')
        return '\n'.join(page.extract_text(extraction_mode='layout') or ''
                         for page in reader.pages)
    if suffix in ('.xml', '.txt'):
        if suffix == '.xml':
            return xml_text(data)
        for encoding in ('utf-8-sig', 'gb18030'):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                pass
        return ''
    if suffix == '.ofd':
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            pieces = []
            for member in archive.infolist():
                if member.filename.lower().endswith('.xml') and member.file_size < 5_000_000:
                    pieces.append(xml_text(archive.read(member)))
            return '\n'.join(pieces)
    return ''


def windows_ocr_text(suffix: str, data: bytes) -> str:
    """Run the built-in Chinese Windows OCR outside the mail process."""
    if sys.platform != 'win32':
        raise RuntimeError('图片 OCR 需要 Windows')
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError('图片超过 40 MB')
    script = Path(__file__).with_name('ocr_windows.ps1')
    if not script.is_file():
        raise RuntimeError('缺少 ocr_windows.ps1')
    with tempfile.TemporaryDirectory(prefix='qq-invoice-ocr-') as temporary:
        image_path = Path(temporary) / f'invoice{suffix}'
        image_path.write_bytes(data)
        try:
            result = subprocess.run(
                ['powershell.exe', '-NoProfile', '-NonInteractive',
                 '-ExecutionPolicy', 'Bypass', '-File', str(script),
                 '-ImagePath', str(image_path)],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=40, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                check=False)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('图片 OCR 超时') from exc
    if result.returncode != 0:
        raise RuntimeError('Windows 图片 OCR 失败：' + result.stderr.strip()[-300:])
    try:
        lines = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError('Windows 图片 OCR 结果无法读取') from exc
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        raise RuntimeError('Windows 图片 OCR 结果格式错误')
    return '\n'.join(lines)


def is_full_invoice_image(text: str) -> bool:
    normalized = re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))
    has_number = bool(re.search(r'发票号[码吗]|发票代码', normalized))
    has_total = bool(re.search(r'价税合计|合计金额|小写金额', normalized))
    has_document = ('电子发票' in normalized or
                    ('购买方' in normalized and '销售方' in normalized))
    return has_number and has_total and has_document


def xml_text(data: bytes) -> str:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        for encoding in ('utf-8-sig', 'gb18030'):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                pass
        return ''
    pieces = []
    for node in root.iter():
        if node.text and node.text.strip():
            tag = node.tag.rsplit('}', 1)[-1]
            value = node.text.strip()
            pieces.append(f'<{tag}>{value}</{tag}> {value}')
    return '\n'.join(pieces)


def body_and_links(message: Message) -> tuple[str, list[str]]:
    chunks: list[str] = []
    links: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() != 'text' or part.get_filename():
            continue
        try:
            content = part.get_content()
        except (UnicodeError, LookupError):
            continue
        if not isinstance(content, str):
            continue
        if part.get_content_subtype() == 'html':
            parser = TextHTML()
            parser.feed(content)
            chunks.append(' '.join(parser.text))
            links.extend(parser.links)
        else:
            chunks.append(content)
        links.extend(URL.findall(content))
    return '\n'.join(chunks), sorted({html.unescape(link) for link in links})


def detailed_links(message: Message) -> list[tuple[str, str, str]]:
    """Return URL, visible label, and nearby text for invoice links."""
    details: list[tuple[str, str, str]] = []
    for part in message.walk():
        if part.get_content_type() != 'text/html' or part.get_filename():
            continue
        try:
            content = part.get_content()
        except (UnicodeError, LookupError):
            continue
        if isinstance(content, str):
            parser = TextHTML()
            parser.feed(content)
            details.extend(parser.link_details)
    if details:
        return list(dict.fromkeys(details))
    body, links = body_and_links(message)
    return [(url, '', body[max(0, body.find(url) - 100):body.find(url)])
            for url in links]


def _public_https(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        raise ValueError('只允许公开的 HTTPS 下载链接')
    if parts.port not in (None, 443):
        raise ValueError('不支持非标准端口')
    if parts.hostname.lower() == 'localhost' or parts.hostname.lower().endswith(('.local', '.internal')):
        raise ValueError('不允许访问本机或内部网络链接')
    try:
        literal = ipaddress.ip_address(parts.hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ValueError('不允许访问内部网络地址')
    addresses = socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not (ipaddress.ip_address(address[4][0]).is_global or
                             (literal is None and ipaddress.ip_address(address[4][0]) in PROXY_DNS_RANGE))
                            for address in addresses):
        raise ValueError('链接没有解析到公开网络地址')


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        _public_https(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def download_direct_link(url: str) -> bytes:
    _public_https(url)
    opener = build_opener(_SafeRedirect())
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0 InvoiceCollector/1.0'})
    with opener.open(request, timeout=20) as response:
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError('文件超过 40 MB，已留待手动处理')
    return data


def direct_kind(url: str) -> str | None:
    suffix = Path(urlsplit(url).path).suffix.lower()
    return suffix if suffix in DIRECT_EXTENSIONS else None


def zip_invoice_files(data: bytes) -> list[tuple[str, bytes]]:
    if not data.startswith(b'PK'):
        raise ValueError('链接返回的不是 ZIP 文件')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members) > MAX_ZIP_MEMBERS:
            raise ValueError('ZIP 中的文件过多')
        result: list[tuple[str, bytes]] = []
        total = 0
        for member in members:
            suffix = Path(member.filename).suffix.lower()
            if member.is_dir() or suffix not in {'.pdf', '.png', '.jpg', '.jpeg'}:
                continue
            total += member.file_size
            if total > MAX_DOWNLOAD_BYTES or member.file_size > MAX_DOWNLOAD_BYTES:
                raise ValueError('ZIP 中的发票文件过大')
            content = archive.read(member)
            if (suffix == '.pdf' and not content.startswith(b'%PDF-') or
                    suffix == '.png' and not content.startswith(b'\x89PNG\r\n\x1a\n') or
                    suffix in {'.jpg', '.jpeg'} and not content.startswith(b'\xff\xd8')):
                raise ValueError('ZIP 中发现文件内容与扩展名不符')
            result.append((safe_name(Path(member.filename).name), content))
        if not result:
            raise ValueError('ZIP 中没有 PDF 或图片发票')
        return result


def attachments(message: Message) -> list[tuple[str, bytes, str, str | None]]:
    result = []
    index = 0
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        mime = part.get_content_type()
        disposition = part.get_content_disposition()
        if not filename and disposition != 'attachment' and not mime.startswith('image/'):
            continue
        data = part.get_payload(decode=True)
        if not data:
            continue
        index += 1
        extension = {'application/pdf': '.pdf', 'image/png': '.png',
                     'image/jpeg': '.jpg', 'text/xml': '.xml',
                     'application/zip': '.zip',
                     'application/x-zip-compressed': '.zip'}.get(mime, '.bin')
        result.append((safe_name(filename or f'image_{index}{extension}'), data,
                       mime, disposition))
    return result


def save_unique(directory: Path, filename: str, data: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    name = safe_name(Path(filename).stem)
    extension = Path(filename).suffix.lower()
    target = directory / f'{name}_{digest}{extension}'
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise OSError(f'同名文件内容不符：{target}')
    if not target.exists():
        durable_write(target, data)
    return target


def durable_write(path: Path, data: bytes) -> None:
    with path.open('wb') as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())


def invoice_row(filename: str, data: bytes, subject: str,
                common: dict[str, str], output: Path,
                source_note: str = '') -> dict[str, str] | None:
    try:
        content = extract_invoice_text(filename, data)
    except Exception as exc:
        content, error = '', str(exc)
    else:
        error = ''
    if not INVOICE_WORDS.search(subject + filename + content[:2000]):
        return None
    image_file = Path(filename).suffix.lower() in {'.png', '.jpg', '.jpeg'}
    amount = image_amount_from_text(content) if image_file else amount_from_text(content)
    if image_file and not is_full_invoice_image(content):
        status = '待核对'
        note = error or '图片 OCR 未确认是完整发票，需人工核对'
    elif amount is None:
        status = '待核对'
        note = (error or ('图片 OCR 未可靠识别价税合计，请人工核对'
                          if image_file
                          else '未可靠识别价税合计；可能是扫描件或特殊版式'))
    elif Decimal(amount) > 5:
        status, note = '已归档', source_note
    elif Decimal(amount) < 5:
        status, note = '低于5元', source_note
    else:
        status, note = '等于5元', source_note
    saved = ''
    if status in {'已归档', '待核对'}:
        destination = output / ('发票_大于5元' if status == '已归档' else '待核对')
        saved = str(save_unique(destination, filename, data).resolve())
    return dict(common, status=status, attachment=filename,
                amount_cny=amount or '', saved_file=saved, note=note)


def save_mail_reference(directory: Path, raw: bytes, received: datetime,
                        subject: str, sender: str, links: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    durable_write(directory / '原始邮件.eml', raw)
    (directory / '邮件信息.txt').write_text(
        f'收件时间：{received.isoformat()}\n主题：{subject}\n发件人：{sender}\n'
        f'链接：\n' + '\n'.join(links) + '\n', encoding='utf-8')
    if links:
        items = ''.join(f'<li><a href="{html.escape(link, quote=True)}" '
                        f'target="_blank" rel="noopener noreferrer">'
                        f'{html.escape(link)}</a></li>' for link in links)
        (directory / '打开链接.html').write_text(
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<title>发票邮件链接</title><body style="font:16px sans-serif;max-width:800px;'
            'margin:40px auto;line-height:1.6;overflow-wrap:anywhere">'
            f'<h1>发票邮件链接</h1><p>{html.escape(subject)}</p><ol>{items}</ol>'
            '<p>下载 PDF 或图片后，可回到发票归集工具点击“导入手动下载的 PDF/图片”。</p>'
            '</body></html>', encoding='utf-8')


def process_message(raw: bytes, received: datetime, folder: str, uid: str,
                    output: Path,
                    fetcher: Callable[[str], bytes] = download_direct_link) -> list[dict[str, str]]:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    subject = str(message.get('Subject', ''))
    sender = str(message.get('From', ''))
    body, all_links = body_and_links(message)
    link_items = detailed_links(message)
    parts = attachments(message)
    reference = safe_name(f'{folder}-{uid}')
    common = dict(received_hk=received.isoformat(), folder=folder, uid=uid,
                  subject=subject, sender=sender)
    rows: list[dict[str, str]] = []
    errors: list[str] = []
    page_links: list[str] = []
    qr_images: list[tuple[str, bytes]] = []

    qr_explicit = bool(QR_WORDS.search(subject + '\n' + body))
    has_structured_attachment = any(Path(name).suffix.lower() in {'.pdf', '.zip', '.xml', '.ofd'}
                                    for name, _data, _mime, _disposition in parts)
    image_parts = [(name, data) for name, data, mime, _disposition in parts
                   if mime.startswith('image/')]
    pdf_parts = [(name, data) for name, data, _mime, _disposition in parts
                 if Path(name).suffix.lower() == '.pdf']
    pdf_stem = Path(pdf_parts[0][0]).stem.lower() if len(pdf_parts) == 1 else ''
    companion_parts = [name for name, _data, _mime, _disposition in parts
                       if Path(name).suffix.lower() in {'.xml', '.ofd', '.zip'}]
    same_invoice_companions = bool(pdf_stem and companion_parts and
                                   all(Path(name).stem.lower() == pdf_stem
                                       for name in companion_parts))

    for filename, data in pdf_parts:
        row = invoice_row(filename, data, subject, common, output)
        if row:
            rows.append(row)

    verified_pdf = (len(pdf_parts) == 1 and len(rows) == 1 and
                    rows[0]['status'] in {'已归档', '低于5元', '等于5元'})
    # A numbered PDF matching the number in the message or its companion files
    # represents the same invoice as the mail's QR/web download entry.
    pdf_covers_mail = bool(verified_pdf and re.fullmatch(r'\d{15,25}', pdf_stem) and
                           (pdf_stem in body or same_invoice_companions) and
                           not any(disposition == 'attachment' and mime.startswith('image/')
                                   for _name, _data, mime, disposition in parts))
    if pdf_covers_mail:
        rows[0]['note'] = 'PDF 已覆盖同封邮件的网页/二维码入口'

    for filename, data, _mime, disposition in parts:
        suffix = Path(filename).suffix.lower()
        if suffix == '.pdf':
            continue
        if suffix == '.zip' and pdf_stem and Path(filename).stem.lower() == pdf_stem:
            continue  # Companion archive is an alternate format of the attached PDF.
        if suffix == '.zip' and INVOICE_WORDS.search(subject + filename):
            try:
                for item_name, item_data in zip_invoice_files(data):
                    if qr_explicit and Path(item_name).suffix.lower() in {'.png', '.jpg', '.jpeg'}:
                        qr_images.append((item_name, item_data))
                        continue
                    row = invoice_row(item_name, item_data, subject, common, output,
                                      '从邮件 ZIP 附件提取')
                    if row:
                        rows.append(row)
            except Exception as exc:
                errors.append(f'ZIP 附件 {filename}：{exc}')
        elif suffix in ('.png', '.jpg', '.jpeg') and not qr_explicit:
            # Ignore ordinary inline logos when a structured invoice is present.
            if (disposition == 'attachment' or INVOICE_WORDS.search(filename)
                    or not has_structured_attachment):
                row = invoice_row(filename, data, subject, common, output)
                if row:
                    rows.append(row)

    for url, label, context in link_items:
        kind = direct_kind(url)
        link_text = label + ' ' + context
        if pdf_covers_mail and (kind is None or
                                kind in {'.png', '.jpg', '.jpeg'} and
                                re.search(r'商家发票|二维码|扫码开票', link_text) or
                                kind in {'.pdf', '.zip'} and
                                Path(urlsplit(url).path).stem.lower() == pdf_stem):
            continue
        if kind in ('.pdf', '.zip') and INVOICE_WORDS.search(subject + link_text):
            try:
                data = fetcher(url)
                if kind == '.pdf':
                    if not data.startswith(b'%PDF-'):
                        raise ValueError('链接返回的不是 PDF 文件')
                    files = [(safe_name(Path(urlsplit(url).path).name), data)]
                else:
                    files = zip_invoice_files(data)
                for filename, file_data in files:
                    if qr_explicit and Path(filename).suffix.lower() in {'.png', '.jpg', '.jpeg'}:
                        qr_images.append((filename, file_data))
                        continue
                    row = invoice_row(filename, file_data, subject, common, output,
                                      '从邮件链接下载' if kind == '.pdf' else '从链接 ZIP 提取')
                    if row:
                        rows.append(row)
            except Exception as exc:
                errors.append(f'{kind.upper()} 链接下载失败：{exc}')
        elif kind in ('.png', '.jpg', '.jpeg') and INVOICE_WORDS.search(subject + link_text):
            try:
                data = fetcher(url)
                if not (data.startswith(b'\x89PNG\r\n\x1a\n') or data.startswith(b'\xff\xd8')):
                    raise ValueError('链接返回的不是图片')
                filename = safe_name(Path(urlsplit(url).path).name)
                if re.search(r'商家发票|二维码|扫码开票', link_text) or qr_explicit:
                    qr_images.append((filename, data))
                    page_links.append(url)  # Keep its source even if the image expires.
                else:
                    row = invoice_row(filename, data, subject, common, output,
                                      '从邮件图片链接下载')
                    if row:
                        rows.append(row)
            except Exception as exc:
                errors.append(f'图片链接下载失败：{exc}')
        elif kind is None and (re.search(r'查看.{0,4}发票|下载.{0,4}发票|点击链接查看发票', link_text)
                               or urlsplit(url).hostname == 'nnfp.jss.com.cn'):
            page_links.append(url)

    qr_expected = (not pdf_covers_mail and (qr_explicit or bool(qr_images)
                   or any(direct_kind(url) in ('.png', '.jpg', '.jpeg')
                          and re.search(r'商家发票|二维码|扫码开票', label + ' ' + context)
                          for url, label, context in link_items)))
    if qr_expected:
        qr_dir = output / '开票二维码_待扫码' / reference
        save_mail_reference(qr_dir, raw, received, subject, sender, all_links)
        for filename, data in image_parts + qr_images:
            save_unique(qr_dir, filename, data)
        rows.append(dict(common, status='二维码待扫码', attachment='', amount_cny='',
                         saved_file=str(qr_dir.resolve()), note='请核查图片或邮件链接'))

    web_links = [url for url in page_links if direct_kind(url) is None]
    if web_links:
        web_dir = output / '网页下载_待处理' / reference
        save_mail_reference(web_dir, raw, received, subject, sender, web_links)
        rows.append(dict(common, status='网页下载待处理', attachment='网页链接',
                         amount_cny='', saved_file=str(web_dir.resolve()),
                         note='打开邮件信息.txt中的链接，手动下载 PDF 或图片'))

    if errors:
        review_dir = output / '待核对' / reference
        save_mail_reference(review_dir, raw, received, subject, sender, all_links)
        rows.append(dict(common, status='待核对', attachment='邮件链接或 ZIP',
                         amount_cny='', saved_file=str(review_dir.resolve()),
                         note='；'.join(errors)))
    elif (not rows and parts and all(Path(name).suffix.lower() in {'.xml', '.ofd'}
                                     for name, _data, _mime, _disposition in parts)
          and INVOICE_WORDS.search(subject + body + ' '.join(name for name, *_ in parts))):
        rows.append(dict(common, status='仅XML/OFD，邮件已保留', attachment='',
                         amount_cny='', saved_file='', note='按设置不下载 XML/OFD'))
    elif not rows and INVOICE_WORDS.search(subject + body):
        review_dir = output / '待核对' / reference
        save_mail_reference(review_dir, raw, received, subject, sender, all_links)
        rows.append(dict(common, status='待核对', attachment='', amount_cny='',
                         saved_file=str(review_dir.resolve()), note='无可自动确认金额的发票附件'))
    return rows


def mail_folders(connection: imaplib.IMAP4_SSL) -> list[tuple[bytes, str]]:
    result, lines = connection.list()
    if result != 'OK':
        raise RuntimeError('无法读取邮箱文件夹')
    folders = []
    for line in lines:
        if not line:
            continue
        flags_match = re.match(rb'\(([^)]*)\)', line)
        flags = flags_match.group(1).lower() if flags_match else b''
        if b'\\noselect' in flags:
            continue
        # IMAP LIST sends the mailbox name last. QQ's non-ASCII names use modified UTF-7.
        match = re.search(rb'("(?:[^"\\]|\\.)*"|[^ ]+)$', line)
        if match:
            mailbox = match.group(1)
            folders.append((mailbox, mailbox.decode('ascii', errors='replace').strip('"')))
    if not folders:
        raise RuntimeError('没有可读取的邮箱文件夹')
    return folders


def is_nonincoming_folder(label: str) -> bool:
    return label.casefold() in {
        'sent messages', 'sent', 'drafts', 'deleted messages', 'deleted',
        'trash', '已发送', '草稿箱', '已删除',
    }


def deleted_folder(folders: list[tuple[bytes, str]]) -> bytes | None:
    for mailbox, label in folders:
        if label.casefold() in {'deleted messages', 'deleted', 'trash', '已删除'}:
            return mailbox
    return None


def move_to_deleted(connection: imaplib.IMAP4_SSL, uid: bytes,
                    target: bytes) -> str:
    """Move one UID, guarding a base-IMAP fallback against other deleted mail."""
    capabilities = {cap.upper() for cap in connection.capabilities}
    if b'MOVE' in capabilities:
        status, _ = connection.uid('MOVE', uid, target)
        if status != 'OK':
            raise RuntimeError('IMAP MOVE 失败')
        return 'IMAP MOVE'

    def deleted_uids() -> set[bytes]:
        status, data = connection.uid('SEARCH', None, 'DELETED')
        if status != 'OK' or not data or data[0] is None:
            raise RuntimeError('无法核查已标记删除的邮件，原邮件已保留')
        return set(data[0].split())

    # Plain EXPUNGE removes every \Deleted message in the selected folder.
    # Refuse that fallback if another message is already marked for deletion.
    if b'UIDPLUS' not in capabilities and deleted_uids():
        raise RuntimeError('收件箱有其他已标记删除的邮件，已跳过兼容删除以免误删')
    status, _ = connection.uid('COPY', uid, target)
    if status != 'OK':
        raise RuntimeError('复制到已删除文件夹失败；原邮件已保留')
    status, _ = connection.uid('STORE', uid, '+FLAGS.SILENT', r'(\Deleted)')
    if status != 'OK':
        raise RuntimeError('复制成功，但原邮件标记删除失败；可能出现重复邮件')
    try:
        if b'UIDPLUS' in capabilities:
            status, _ = connection.uid('EXPUNGE', uid)
        else:
            if deleted_uids() != {uid}:
                raise RuntimeError('删除标记出现其他邮件，已取消兼容删除')
            status, _ = connection.expunge()
        if status != 'OK':
            raise RuntimeError('邮件清除命令失败')
    except Exception as exc:
        try:
            restore_status, _ = connection.uid('STORE', uid, '-FLAGS.SILENT', r'(\Deleted)')
        except Exception as restore_exc:
            raise RuntimeError(f'{exc}；恢复原邮件删除标记也失败：{restore_exc}') from exc
        if restore_status != 'OK':
            raise RuntimeError(f'{exc}；恢复原邮件删除标记也失败') from exc
        raise RuntimeError(f'{exc}；已撤销原邮件的删除标记，已删除文件夹可能有副本') from exc
    return 'UID EXPUNGE' if b'UIDPLUS' in capabilities else '兼容模式 EXPUNGE'


def eligible_for_delete(rows: list[dict[str, str]]) -> bool:
    return bool(rows) and all(
        (row['status'] == '已归档' and bool(row['saved_file'])
         and Path(row['saved_file']).is_file()) or
        (row['status'] == '低于5元' and bool(row['amount_cny'])
         and Decimal(row['amount_cny']) < 5)
        for row in rows)


def persist_rows(writer: csv.DictWriter, file: io.TextIOWrapper,
                 rows: list[dict[str, str]]) -> None:
    writer.writerows(rows)
    file.flush()
    os.fsync(file.fileno())


def write_summary(report_path: Path, output: Path) -> None:
    with report_path.open('r', newline='', encoding='utf-8-sig') as report:
        counts: dict[str, int] = {}
        for row in csv.DictReader(report):
            status = row['status']
            counts[status] = counts.get(status, 0) + 1
    (output / '处理汇总.json').write_text(
        json.dumps(counts, ensure_ascii=False, indent=2), encoding='utf-8')


def import_manual_pdfs(files: list[Path], output: Path) -> list[dict[str, str]]:
    """Classify PDFs or images downloaded by hand from invoice landing pages."""
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / '处理清单.csv'
    needs_header = not report_path.exists() or report_path.stat().st_size == 0
    rows: list[dict[str, str]] = []
    with report_path.open('a', newline='', encoding='utf-8-sig' if needs_header else 'utf-8') as report:
        writer = csv.DictWriter(report, fieldnames=CSV_FIELDS)
        if needs_header:
            writer.writeheader()
        for path in files:
            data = path.read_bytes()
            suffix = path.suffix.lower()
            valid = (suffix == '.pdf' and data.startswith(b'%PDF-') or
                     suffix == '.png' and data.startswith(b'\x89PNG\r\n\x1a\n') or
                     suffix in {'.jpg', '.jpeg'} and data.startswith(b'\xff\xd8'))
            if not valid:
                raise ValueError(f'不是有效的 PDF 或图片发票：{path.name}')
            common = dict(received_hk=datetime.now(HONG_KONG).isoformat(),
                          folder='手动导入', uid='', subject='手动下载的发票', sender='')
            row = invoice_row(path.name, data, '手动下载的发票', common, output,
                              '从网页手动下载并导入')
            if row:
                persist_rows(writer, report, [row])
                rows.append(row)
    write_summary(report_path, output)
    return rows


def received_date(fetch_data: list[object]) -> datetime:
    metadata = b' '.join(item[0] if isinstance(item, tuple) else item
                         for item in fetch_data if isinstance(item, (tuple, bytes)))
    found = INTERNAL_DATE.search(metadata)
    if not found:
        raise ValueError('邮件缺少 INTERNALDATE')
    return datetime.strptime(found.group(1).decode('ascii'), '%d-%b-%Y %H:%M:%S %z').astimezone(HONG_KONG)


def parse_day(value: str) -> date:
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('日期必须使用 YYYY-MM-DD 格式')
    return date.fromisoformat(value)


def imap_day(value: date) -> str:
    return f'{value.day:02d}-{MONTHS[value.month - 1]}-{value.year:04d}'


def search_window(start_day: date, end_day: date) -> tuple[str, str]:
    """IMAP searches server dates; pad both ends, then filter in HK time."""
    if end_day < start_day:
        raise ValueError('结束日期不能早于开始日期')
    return imap_day(start_day - timedelta(days=1)), imap_day(end_day + timedelta(days=2))


def collect(address: str, password: str, output: Path, start_day: date,
            end_day: date, delete_mail: bool = True,
            progress: Callable[[str], None] | None = None,
            stop_requested: Callable[[], bool] | None = None,
            on_authenticated: Callable[[], None] | None = None) -> list[dict[str, str]]:
    since, before = search_window(start_day, end_day)
    start = datetime.combine(start_day, datetime.min.time(), tzinfo=HONG_KONG)
    end = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=HONG_KONG)
    emit = progress or print
    output.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    report_path = output / '处理清单.csv'
    deletion_path = output / '删除状态.csv'
    report_needs_header = not report_path.exists() or report_path.stat().st_size == 0
    deletion_needs_header = not deletion_path.exists() or deletion_path.stat().st_size == 0
    with report_path.open('a', newline='', encoding='utf-8-sig' if report_needs_header else 'utf-8') as report, \
         deletion_path.open('a', newline='', encoding='utf-8-sig' if deletion_needs_header else 'utf-8') as deletion_report:
        writer = csv.DictWriter(report, fieldnames=CSV_FIELDS)
        deletion_writer = csv.DictWriter(deletion_report, fieldnames=DELETE_FIELDS)
        if report_needs_header:
            writer.writeheader()
        if deletion_needs_header:
            deletion_writer.writeheader()
        with imaplib.IMAP4_SSL('imap.qq.com', 993) as connection:
            login_status, _ = connection.login(address, password)
            if login_status != 'OK':
                raise imaplib.IMAP4.error('IMAP 登录未成功')
            if on_authenticated:
                on_authenticated()
            folders = mail_folders(connection)
            trash = deleted_folder(folders)
            if trash is None:
                emit('未找到“已删除”文件夹；仍会下载，但不会删除邮件。')
            for mailbox, label in folders:
                if stop_requested and stop_requested():
                    emit('收到停止请求；已保存当前处理结果。')
                    break
                if is_nonincoming_folder(label):
                    continue
                status, _ = connection.select(mailbox, readonly=not delete_mail)
                if status != 'OK':
                    emit(f'跳过无法读取的文件夹：{label}')
                    continue
                status, data = connection.uid('SEARCH', None, 'SINCE', since,
                                              'BEFORE', before)
                if status != 'OK':
                    emit(f'搜索失败：{label}')
                    continue
                uids = data[0].split() if data and data[0] else []
                emit(f'{label}: {len(uids)} 封候选邮件')
                for uid in uids:
                    if stop_requested and stop_requested():
                        emit('收到停止请求；已保存当前处理结果。')
                        break
                    status, fetched = connection.uid('FETCH', uid, '(BODY.PEEK[] INTERNALDATE)')
                    if status != 'OK' or not fetched:
                        emit(f'读取失败：{label}/{uid.decode()}')
                        continue
                    try:
                        received = received_date(fetched)
                        if not start <= received < end:
                            continue
                        raw = next(item[1] for item in fetched if isinstance(item, tuple))
                        message = BytesParser(policy=policy.default).parsebytes(raw)
                        mail_rows = process_message(raw, received, label, uid.decode(), output)
                        persist_rows(writer, report, mail_rows)
                        rows.extend(mail_rows)
                        if mail_rows:
                            statuses = '、'.join(sorted({row['status'] for row in mail_rows}))
                            emit(f'{label}/{uid.decode()}：{statuses}')
                        if eligible_for_delete(mail_rows):
                            try:
                                if not delete_mail:
                                    result, note = '已保留', '本次选择保留原邮件'
                                elif trash is None:
                                    raise RuntimeError('未找到“已删除”文件夹')
                                else:
                                    method = move_to_deleted(connection, uid, trash)
                                    result, note = '已移到已删除', method
                                    emit(f'已移到已删除：{label}/{uid.decode()}（{method}）')
                            except Exception as exc:
                                result, note = '删除失败', str(exc)
                                emit(f'删除失败：{label}/{uid.decode()}：{exc}')
                            deletion_writer.writerow(dict(received_hk=received.isoformat(),
                                                           folder=label, uid=uid.decode(),
                                                           subject=str(message.get('Subject', '')),
                                                           result=result, note=note))
                            deletion_report.flush()
                            os.fsync(deletion_report.fileno())
                    except Exception as exc:
                        emit(f'处理失败：{label}/{uid.decode()}：{exc}')
                # SELECT on the next folder deselects this one without expunging
                # unrelated messages that another client may have marked deleted.
    write_summary(report_path, output)
    return rows


def load_settings() -> dict[str, str | bool]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    settings: dict[str, str | bool] = {
        key: value for key, value in data.items()
        if key in {'email', 'start', 'end', 'output_root'} and isinstance(value, str)
    }
    if isinstance(data.get('delete_mail'), bool):
        settings['delete_mail'] = data['delete_mail']
    return settings


def save_settings(address: str, start_day: date, end_day: date,
                  output_root: Path, delete_mail: bool = True) -> None:
    data = {'email': address, 'start': start_day.isoformat(),
            'end': end_day.isoformat(), 'output_root': str(output_root.resolve()),
            'delete_mail': delete_mail}
    temporary = SETTINGS_PATH.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(SETTINGS_PATH)


def prompt_value(label: str, default: str) -> str:
    answer = input(f'{label} [{default}]：').strip()
    return answer or default


def main() -> None:
    from credential_store import load_credential, save_credential

    parser = argparse.ArgumentParser(description='按日期归集 QQ 邮箱发票')
    parser.add_argument('--email', help='QQ 邮箱地址；不填则交互选择')
    parser.add_argument('--start', help='开始日期 YYYY-MM-DD，含当日')
    parser.add_argument('--end', help='结束日期 YYYY-MM-DD，含当日')
    parser.add_argument('--output-root', type=Path, help='归档结果的根目录')
    parser.add_argument('--output', type=Path, help='直接指定本次输出文件夹')
    parser.add_argument('--keep-mail', action='store_true', help='本次仅下载，不移动原邮件')
    parser.add_argument('--replace-auth', action='store_true',
                        help='重新输入此邮箱的授权码；登录成功后替换本机保存的旧码')
    args = parser.parse_args()
    if PdfReader is None:
        parser.error('缺少 pypdf，请先运行 python -m pip install -r requirements.txt')
    settings = load_settings()
    address = args.email or prompt_value('QQ 邮箱', settings.get('email', ''))
    if not re.fullmatch(r'[^\s@]+@qq\.com', address, re.I):
        parser.error('请输入有效的 @qq.com 邮箱地址')
    start_text = args.start or prompt_value('开始日期（含）', settings.get('start', ''))
    end_text = args.end or prompt_value('结束日期（含）', settings.get('end', ''))
    try:
        start_day, end_day = parse_day(start_text), parse_day(end_text)
        search_window(start_day, end_day)
    except (ValueError, OverflowError) as exc:
        parser.error(str(exc))
    root_default = settings.get('output_root', str(Path(__file__).parent / '归档结果'))
    output_root = args.output_root or Path(prompt_value('保存根目录', root_default))
    output = args.output or output_root / safe_name(address.lower()) / f'{start_day}_至_{end_day}'
    print(f'本次输出：{output.resolve()}')
    if args.keep_mail:
        delete_mail = False
    elif sys.stdin.isatty():
        delete_mail = prompt_value('归档成功后移到“已删除”？输入 y 或 n', 'y').lower() in {'y', 'yes', '是'}
    else:
        delete_mail = True
    try:
        stored_code = None if args.replace_auth else load_credential(address)
    except OSError as exc:
        print(f'无法读取本机保存的授权码：{exc}')
        stored_code = None
    if stored_code:
        print(f'使用 {address.lower()} 在本机保存的 IMAP 授权码。')
    entered_code = None if stored_code else getpass.getpass('请输入 QQ 邮箱 IMAP 授权码（输入不显示）：')
    password = stored_code or entered_code
    if not password:
        parser.error('授权码不能为空')

    def remember_after_login() -> None:
        if entered_code:
            try:
                save_credential(address, entered_code)
            except OSError as exc:
                print(f'邮箱登录成功，但授权码未能保存到 Windows 凭据管理器：{exc}')
            else:
                print('邮箱登录成功，授权码已保存在 Windows 凭据管理器。')

    try:
        rows = collect(address, password, output, start_day, end_day, delete_mail,
                       on_authenticated=remember_after_login)
    except imaplib.IMAP4.error as exc:
        parser.exit(1, f'邮箱连接失败：{exc}\n请确认已开启 IMAP 服务并使用授权码。\n')
    save_settings(address, start_day, end_day, output_root, delete_mail)
    print(f'完成：{len(rows)} 条记录。清单：{(output / "处理清单.csv").resolve()}')


if __name__ == '__main__':
    main()
