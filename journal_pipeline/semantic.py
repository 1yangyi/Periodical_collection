"""Optional local embedding ranking. No hosted inference or automatic downloads."""
import math
import threading
from collections import OrderedDict
from functools import lru_cache


class SemanticError(ValueError):
    pass


@lru_cache(maxsize=2)
def load_embedding_model(model_name: str, revision: str):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as error:
        raise SemanticError('语义模式需要安装 requirements-semantic.txt，并提前下载模型；详见 README') from error
    try:
        return SentenceTransformer(model_name, revision=revision, device='cpu',
                                   local_files_only=True, trust_remote_code=False)
    except Exception as error:
        raise SemanticError(f'无法加载本地模型 {model_name}：请按 README 提前下载，或设置本地模型目录。{error}') from error


_ENCODE_LOCK = threading.Lock()


class SemanticMatcher:
    def __init__(self, settings):
        self.settings = settings
        self.model = load_embedding_model(settings.embedding_model, settings.embedding_revision)
        self.cache = OrderedDict()
        self.lock = threading.RLock()

    def _encode(self, texts):
        with self.lock:
            missing = list(dict.fromkeys(text for text in texts if text not in self.cache))
            if missing:
                try:
                    with _ENCODE_LOCK:
                        encoded = self.model.encode(missing, normalize_embeddings=True,
                                                    convert_to_numpy=True, show_progress_bar=False,
                                                    batch_size=32)
                    if len(encoded) != len(missing):
                        raise ValueError('模型返回的向量数量与文本数量不一致')
                    for text, raw in zip(missing, encoded):
                        vector = [float(value) for value in raw]
                        if not vector or not all(math.isfinite(v) for v in vector):
                            raise ValueError('模型返回了空向量或非有限数值')
                        norm = math.sqrt(sum(v * v for v in vector))
                        self.cache[text] = [v / norm for v in vector] if norm else vector
                except Exception as error:
                    raise SemanticError(f'本地 embedding 编码失败：{error}') from error
            result = [self.cache[text] for text in texts]
            for text in texts:
                self.cache.move_to_end(text)
            while len(self.cache) > 2048:
                self.cache.popitem(last=False)
            return result

    def _score(self, texts, queries):
        if not texts:
            return []
        query_vectors = self._encode(['为这个句子生成表示以用于检索相关文章：' + q for q in queries])
        vectors = self._encode(texts)
        decisions = []
        for vector in vectors:
            if any(len(vector) != len(query) for query in query_vectors):
                raise SemanticError('模型返回的向量维度不一致')
            scores = [sum(a * b for a, b in zip(vector, query)) for query in query_vectors]
            index = max(range(len(scores)), key=scores.__getitem__)
            decisions.append({'method': 'semantic', 'score': max(-1.0, min(1.0, scores[index])),
                              'query': queries[index], 'model': self.settings.embedding_model,
                              'revision': self.settings.embedding_revision})
        return decisions

    def select_links(self, candidates):
        candidates = candidates[:self.settings.semantic_candidate_limit]
        scores = self._score([c['context'] for c in candidates], self.settings.semantic_queries)
        accepted = [dict(candidate, **decision) for candidate, decision in zip(candidates, scores)
                    if decision['score'] >= self.settings.semantic_threshold]
        return sorted(accepted, key=lambda item: item['score'], reverse=True)[:self.settings.semantic_top_k]

    def validate_content(self, text):
        # 400-character chunks stay below the typical Chinese embedding context window.
        text = text[:8000]
        chunks = [text[start:start + 400] for start in range(0, len(text), 400)]
        if not chunks:
            return {'method': 'semantic', 'score': 0.0, 'accepted': False}
        decisions = self._score(chunks, self.settings.semantic_content_queries)
        best_index = max(range(len(decisions)), key=lambda i: decisions[i]['score'])
        best = decisions[best_index]
        return dict(best, accepted=best['score'] >= self.settings.semantic_content_threshold,
                    chunk_index=best_index, evaluated_chars=len(text))
