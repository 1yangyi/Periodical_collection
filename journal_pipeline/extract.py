import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from bs4 import BeautifulSoup, Comment

from .fetch import BlockedError


_TEXT_BLOCKS = ('address', 'article', 'blockquote', 'dd', 'div', 'dl', 'dt', 'form', 'h1', 'h2',
                'h3', 'h4', 'h5', 'h6', 'li', 'main', 'p', 'section', 'table', 'td', 'th', 'tr')


def _block_text(body):
    for node in body.find_all(string=True):
        if not isinstance(node, Comment):
            node.replace_with(re.sub(r'\s+', ' ', str(node)))
    for br in body.find_all('br'):
        br.replace_with('\n')
    for block in body.find_all(_TEXT_BLOCKS):
        block.insert_before('\n')
        block.append('\n')
    return '\n'.join(line.strip() for line in body.get_text().splitlines() if line.strip())


def page_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    raw_text = soup.get_text(' ', strip=True)
    if (any(k in title for k in ('安全验证', '访问验证', '人机验证', '验证码', 'Access Denied'))
            or (len(raw_text) < 600 and any(k in raw_text for k in ('请输入验证码', '完成验证后', '访问过于频繁')))):
        raise BlockedError('页面为验证码或访问验证页，请人工处理')
    for tag in soup(['script', 'style', 'noscript', 'nav', 'header', 'footer',
                     'input', 'button', 'select', 'textarea']):
        tag.decompose()
    body = soup.find('main') or soup.find('article') or soup.body or soup
    # Anchor menus are useful for discovery, but are not guideline正文 evidence.
    evidence_soup = BeautifulSoup(str(body), 'html.parser')
    for tag in evidence_soup.find_all('a'):
        tag.decompose()
    return _block_text(body), _block_text(evidence_soup)


def extract_document(path: Path) -> dict:
    suffix = path.suffix.lower()
    try:
        if suffix == '.docx':
            from docx import Document
            document = Document(path)
            text = '\n'.join([p.text for p in document.paragraphs] +
                             ['\t'.join(cell.text for cell in row.cells)
                              for table in document.tables for row in table.rows])
        elif suffix == '.pdf':
            from pypdf import PdfReader
            text = '\n'.join(page.extract_text() or '' for page in PdfReader(path).pages)
        elif suffix in ('.doc', '.wps'):
            converter = shutil.which('soffice') or shutil.which('libreoffice')
            if not converter:
                return {'status': 'unavailable', 'reason': '未找到 LibreOffice/soffice，已保留原文件'}
            with tempfile.TemporaryDirectory() as directory:
                work = Path(directory)
                profile = (work / 'profile').as_uri()
                result = subprocess.run([converter, f'-env:UserInstallation={profile}', '--headless',
                                         '--convert-to', 'txt:Text', '--outdir', directory, str(path.resolve())],
                                        capture_output=True, timeout=60, check=False)
                converted = work / (path.stem + '.txt')
                if result.returncode != 0 or not converted.exists():
                    raise RuntimeError('LibreOffice 未生成文本文件')
                text = converted.read_text(encoding='utf-8-sig', errors='replace')
        else:
            return {'status': 'unsupported', 'reason': '仅保留附件，不自动解压或转换此格式'}
        if not text.strip():
            return {'status': 'empty', 'reason': '未提取到文本（可能为扫描 PDF），需人工检查/OCR'}
        target = path.with_suffix(path.suffix + '.txt')
        target.write_text(text.strip() + '\n', encoding='utf-8')
        return {'status': 'ok', 'path': target.name}
    except Exception as error:
        return {'status': 'failed', 'reason': f'{type(error).__name__}: {error}'[:500]}
