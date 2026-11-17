import json
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    input: Path = ROOT / 'data/raw/期刊链接.xlsx'
    output: Path = ROOT / 'outputs/current'
    sheet: str = '期刊链接'
    workers: int = 2
    timeout: float = 20
    retries: int = 2
    delay: float = 1
    max_pages: int = 30
    max_depth: int = 3
    max_bytes: int = 20 * 1024 * 1024
    min_text_length: int = 40
    browser: str = 'auto'
    browser_wait_ms: int = 1500
    discover_official: bool = True
    http_fallback: bool = False
    semantic: bool = False
    embedding_model: str = 'BAAI/bge-small-zh-v1.5'
    embedding_revision: str = 'main'
    semantic_threshold: float = 0.8
    semantic_content_threshold: float = 0.8
    semantic_top_k: int = 3
    semantic_candidate_limit: int = 200
    semantic_queries: list[str] = field(default_factory=lambda: [
        '论文稿件撰写排版和投稿要求', '期刊作者服务与投稿资料下载',
        '论文排版示例和投稿模板', '参考文献图表及修改稿要求'])
    semantic_content_queries: list[str] = field(default_factory=lambda: [
        '论文正文的字体字号行距页边距和排版要求', '投稿论文的撰写要求和作者提交材料说明',
        '参考文献引文图表及修改稿的具体编写要求'])
    rules: dict = field(default_factory=dict)

    def validate(self):
        for key in ('workers', 'max_pages', 'max_bytes', 'min_text_length',
                    'semantic_top_k', 'semantic_candidate_limit'):
            value = getattr(self, key)
            if type(value) is not int or value <= 0:
                raise ValueError(f'{key} 必须为正整数')
        for key in ('retries', 'max_depth', 'browser_wait_ms'):
            value = getattr(self, key)
            if type(value) is not int or value < 0:
                raise ValueError(f'{key} 必须为非负整数')
        import math
        if (not math.isfinite(self.timeout) or not math.isfinite(self.delay)
                or self.timeout <= 0 or self.delay < 0):
            raise ValueError('timeout 必须大于 0，delay 必须不小于 0，且都必须是有限数值')
        if self.browser not in ('auto', 'off', 'always', 'chrome', 'auto-chrome'):
            raise ValueError('browser 必须是 auto/off/always/chrome/auto-chrome')
        for key in ('http_fallback', 'semantic', 'discover_official'):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f'{key} 必须是布尔值')
        for key in ('semantic_threshold', 'semantic_content_threshold'):
            value = getattr(self, key)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f'{key} 必须是 0 到 1 之间的有限数值')
        for key in ('embedding_model', 'embedding_revision'):
            if not isinstance(getattr(self, key), str) or not getattr(self, key).strip():
                raise ValueError(f'{key} 必须是非空字符串')
        for key in ('semantic_queries', 'semantic_content_queries'):
            values = getattr(self, key)
            if not isinstance(values, list) or not values or not all(isinstance(v, str) and v.strip() for v in values):
                raise ValueError(f'{key} 必须是非空字符串列表')
        return self


def load_settings(path: Path | None = None) -> Settings:
    path = Path(path).resolve() if path else ROOT / 'config/pipeline.toml'
    data = tomllib.loads(path.read_text(encoding='utf-8'))
    options = data.get('pipeline', {})
    allowed = {f.name for f in fields(Settings)} - {'rules'}
    unknown = options.keys() - allowed
    if unknown:
        raise ValueError(f'未知配置字段: {sorted(unknown)}')
    # 配置中的相对路径相对配置文件；CLI 参数相对执行目录。
    for key in ('input', 'output'):
        if key in options:
            options[key] = (path.parent / options[key]).resolve()
    rule_path = (path.parent / data.get('rules_file', 'rules.json')).resolve()
    rules = json.loads(rule_path.read_text(encoding='utf-8'))
    for key in ('strong', 'content', 'weak', 'exclude_markers', 'cnki_markers',
                'pro_platform_markers', 'download_exts'):
        if not isinstance(rules.get(key), list) or not all(isinstance(x, str) and x for x in rules[key]):
            raise ValueError(f'规则 {key} 必须是非空字符串构成的列表')
    if 'resource_hints' in rules and (not isinstance(rules['resource_hints'], list)
                                    or not all(isinstance(v, str) and v.strip() for v in rules['resource_hints'])):
        raise ValueError('规则 resource_hints 必须是非空字符串构成的列表')
    return Settings(**options, rules=rules).validate()
