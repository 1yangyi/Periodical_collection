import re


_CATEGORIES = {
    '字体字号': r'字体|字号|[宋黑楷仿]体|仿宋|[初一二三四五六七八]号字|\bfont\b|typeface',
    '标题与层级': r'题名|标题|小标题|各级标题|heading|\btitle\b',
    '页面与段落': r'行距|页边距|缩进|单双栏|双栏|段间距|对齐|纸张|line spacing|margins?|indent|columns?',
    '摘要与关键词': r'摘\s*要|关键词|关键字|abstract|keywords?',
    '注释与引证': r'脚注|尾注|注释|引注|引证|footnotes?|endnotes?|citation',
    '参考文献': r'参考文献|著录|文献表|bibliography|references?',
    '图表与公式': r'图表|表格|插图|图题|表题|图注|公式|分辨率|图像|figures?|tables?|equations?|\bdpi\b',
    '篇幅与稿件文件': r'字数|篇幅|稿件格式|稿件长度|文件格式|\.docx?\b|word (?:limit|count|document)|file format|manuscript length',
}
_SIGNAL = re.compile(
    r'应当|应[为使用采标按在有将由置写列另包符避不小大]|应采用|必须|须|不得|不宜|不超过|不少于|'
    r'不低于|不高于|不多于|为宜|限于|仅限|统一|一般为|顺序为|格式为|字体为|字号为|'
    r'设为|居中|左对齐|右对齐|[宋黑楷仿]体|仿宋|小[一二三四五六]|\d\s*(?:pt|dpi)|'
    r'\b(?:must|should|shall|required|maximum|minimum|no more than|at least)\b|'
    r'^(?:use|avoid|provide|include)\b|'
    r'(?:题名|标题|正文|稿件|文稿|行距|参考文献|引证|图表|摘要).{0,18}(?:使用|采用|要求[为：:])', re.I)
_MANUSCRIPT_LENGTH = re.compile(
    r'(?:稿件|论文|全文|文章|正文)(?:的)?(?:总)?(?:字数|篇幅)\D{0,8}'
    r'(\d+(?:\.\d+)?)(万)?\s*(?:字\s*)?(?:到|至|[-–—~～])\s*(\d+(?:\.\d+)?)(万)?\s*字')
_FORMAT_ENTRY = re.compile(r'模板|模版|体例|著录|作图|排版|格式|样例|template|formatting|reference style', re.I)
_TEMPLATE = re.compile(r'模板|模版|template|insert your|enter your|type your|此处填写|在此处输入', re.I)
_BROWSER_TEXT = re.compile(r'浏览器|浏览[此本]页面|屏幕分辨率|\bIE\s*\d|browser compatibility', re.I)


def find_requirements(text, *, skip_lines=0):
    for line_number, line in enumerate(text.splitlines()[skip_lines:], skip_lines + 1):
        for sentence in re.split(r'(?<=[。！？；])', line):
            quote = sentence.strip()
            if len(quote) < 4 or _BROWSER_TEXT.search(quote) or not _SIGNAL.search(quote):
                continue
            categories = [key for key, pattern in _CATEGORIES.items() if re.search(pattern, quote, re.I)]
            if categories:
                yield line_number, quote, categories


def _template_styles(path):
    from docx import Document
    from docx.oxml.ns import qn

    def properties(font):
        fonts = font._element.xpath('./w:rPr/w:rFonts')
        east_asia = fonts[0].get(qn('w:eastAsia')) if fonts else None
        return {'中文字体': east_asia, '字体': font.name,
                '字号': f'{font.size.pt:g} pt' if font.size is not None else None}

    def style_properties(style):
        inherited = {}
        visited = set()
        while style is not None and style.style_id not in visited:
            visited.add(style.style_id)
            for key, value in properties(style.font).items():
                if value is not None:
                    inherited.setdefault(key, value)
            style = style.base_style
        return inherited

    document = Document(path)
    for number, paragraph in enumerate(document.paragraphs, 1):
        if not paragraph.text.strip():
            continue
        style = paragraph.style
        if not re.fullmatch(r'Title|Heading [1-9]', style.name if style else '', re.I):
            continue
        inherited = style_properties(style)
        observed = {}
        for run in paragraph.runs:
            if not run.text.strip():
                continue
            character = style_properties(run.style)
            for key, value in properties(run.font).items():
                value = value or character.get(key) or inherited.get(key)
                if value is not None:
                    observed.setdefault(key, set()).add(value)
        if observed:
            value = '，'.join(f'{key} {" / ".join(sorted(values))}' for key, values in observed.items())
            yield number, f'{paragraph.style.name} 样式，{value}'


def build_format_report(report, directory):
    requirements, review, ranges = {}, [], {}

    def add(basis, quote, categories, source):
        entry = requirements.setdefault((basis, quote), {
            'basis': basis, 'quote': quote, 'categories': categories, 'sources': []})
        if source not in entry['sources']:
            entry['sources'].append(source)
        return entry

    for artifact in report['artifacts']:
        extraction = artifact.get('extraction', {})
        text_path = artifact['path'] if artifact['kind'] == 'html' else extraction.get('path')
        source = {'url': artifact['final_url'], 'artifact': artifact['path']}
        if artifact.get('retained_for_review'):
            review.append(dict(source, status='相关性待核对', reason='附件正文未通过语义验证'))
            continue
        if not text_path:
            review.append(dict(source, status='原件已保存，文字未提取', reason=extraction.get('reason')))
            continue
        text = (directory / text_path).read_text(encoding='utf-8')
        for line, quote, categories in find_requirements(text, skip_lines=4 if artifact['kind'] == 'html' else 0):
            entry = add('explicit_text', quote, categories, dict(source, path=text_path, line=line))
            match = _MANUSCRIPT_LENGTH.search(quote)
            if match:
                low, low_unit, high, high_unit = match.groups()
                key = (float(low) * (10000 if low_unit or high_unit else 1),
                       float(high) * (10000 if high_unit else 1))
                ranges.setdefault(key, entry)
        if artifact['kind'] == 'attachment' and artifact['path'].lower().endswith('.docx'):
            context = ' '.join((artifact['label'], artifact['path'], artifact['selection'].get('parent_label', ''), text[:3000]))
            if _TEMPLATE.search(context):
                try:
                    for paragraph, quote in _template_styles(directory / artifact['path']):
                        add('template_style', quote, ['字体字号', '标题与层级'], dict(source, paragraph=paragraph))
                except (KeyError, ValueError, OSError, AttributeError) as error:
                    review.append(dict(source, status='模板样式待核对', reason=str(error)))
    parents_with_templates = {artifact['selection'].get('source_url') for artifact in report['artifacts']
                              if artifact['kind'] == 'attachment' and _TEMPLATE.search(artifact['label'])}
    visited = {url for visit in report['visits'] for url in (visit['url'], visit.get('final_url'))}
    for visit in report['visits']:
        if (visit['status'] in {'no_content', 'blocked', 'fetch_failed'} and _FORMAT_ENTRY.search(visit['label'])
                and visit.get('final_url') not in parents_with_templates):
            review.append({'url': visit['url'], 'label': visit['label'], 'status': '格式入口待核对',
                           'reason': visit.get('content_validation', {}).get('reason') or visit['status']})
    pending_formats = {}
    for candidate in report['candidates']:
        url = candidate['candidate_url']
        if (candidate['queue_status'] in {'queued', 'max_depth'} and url not in visited
                and _FORMAT_ENTRY.search(candidate['label'])):
            pending_formats.setdefault(url, {'url': url, 'label': candidate['label'],
                                             'status': '格式入口未访问', 'reason': '达到遍历上限'})
    review.extend(pending_formats.values())

    conflicts = [{'field': '稿件字数', 'requirements': list(ranges.values())}] if len(ranges) > 1 else []
    categories = {key: sum(key in entry['categories'] for entry in requirements.values()) for key in _CATEGORIES}
    return {'journal': report['journal']['name'], 'crawl_status': report['status'],
            'categories': categories, 'requirements': list(requirements.values()),
            'possible_conflicts': conflicts, 'review': review}


def format_markdown(result):
    lines = [f"# {result['journal']}投稿格式", '', f"采集状态为 `{result['crawl_status']}`。", '',
             '条目来自本次取得的资料，未列出的要求需另行核查。']
    unverified = [category for category, count in result['categories'].items() if not count]
    if unverified:
        lines.append(f'本次未取得可核对条目的类别包括{ "、".join(unverified)}。')
    for category, count in result['categories'].items():
        if not count:
            continue
        lines.extend(['', f'## {category}', ''])
        for item in result['requirements']:
            if category not in item['categories']:
                continue
            basis = '原文' if item['basis'] == 'explicit_text' else '模板样式观察'
            lines.append(f"- **{basis}** {item['quote']}")
            for source in item['sources']:
                where = (f"{source['path']} 第 {source['line']} 行" if 'line' in source else
                         f"{source['artifact']} 第 {source['paragraph']} 个段落")
                lines.append(f"  来源位置为 {where}。[来源页面]({source['url']})")
    if result['possible_conflicts'] or result['review']:
        lines.extend(['', '## 待核对', ''])
        for conflict in result['possible_conflicts']:
            lines.append(f"- {conflict['field']}存在不同表述，请核对适用范围。")
            lines.extend(f"  - {item['quote']}" for item in conflict['requirements'])
        for entry in result['review']:
            label = entry.get('label') or entry.get('artifact')
            lines.append(f"- {label}。{entry['status']}。{entry['reason'] or ''} [来源]({entry['url']})")
    return '\n'.join(lines) + '\n'
