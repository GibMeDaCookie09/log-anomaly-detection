"""Pluggable embedding backends.

Three backends behind one interface, deliberately:

  TfidfEmbedder            - the honest baseline. No model download, no API key.
                             If this beats the neural embedder on your data, say
                             so in the README. That comparison is the point.
  SentenceTransformerEmbedder - local neural embeddings. Better on semantically
                             similar templates with different vocabulary.
  ApiEmbedder              - hosted embeddings. Best quality, costs money, adds
                             latency and a network dependency.

Swapping backends must not require touching detect.py or api.py. That is why
this file exists as an abstraction rather than a function call.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class Embedder(ABC):
    """Maps log templates to dense vectors."""

    name: str = "abstract"

    @abstractmethod
    def fit(self, templates: list[str]) -> Embedder: ...

    @abstractmethod
    def transform(self, templates: list[str]) -> np.ndarray: ...

    def fit_transform(self, templates: list[str]) -> np.ndarray:
        return self.fit(templates).transform(templates)


class TfidfEmbedder(Embedder):
    """Character n-gram TF-IDF, reduced with SVD.

    Character n-grams rather than words because log templates are full of
    identifiers, paths and camelCase that word tokenisers mangle.
    """

    name = "tfidf-svd"

    def __init__(self, n_components: int = 64, ngram_range: tuple[int, int] = (2, 4)):
        from sklearn.decomposition import TruncatedSVD
        from sklearn.feature_extraction.text import TfidfVectorizer

        self.n_components = n_components
        self._vec = TfidfVectorizer(analyzer="char_wb", ngram_range=ngram_range, min_df=1)
        self._svd_cls = TruncatedSVD
        self._svd = None

    def fit(self, templates: list[str]) -> TfidfEmbedder:
        X = self._vec.fit_transform(templates)
        # SVD needs n_components < n_features; clamp for small corpora.
        k = min(self.n_components, max(2, min(X.shape) - 1))
        self._svd = self._svd_cls(n_components=k, random_state=42)
        self._svd.fit(X)
        return self

    def transform(self, templates: list[str]) -> np.ndarray:
        if self._svd is None:
            raise RuntimeError("call fit() before transform()")
        X = self._vec.transform(templates)
        return np.asarray(self._svd.transform(X), dtype=np.float32)


class SentenceTransformerEmbedder(Embedder):
    """Local neural embeddings via sentence-transformers.

    Stateless: fit() is a no-op because the model is pretrained. Kept in the
    interface so backends stay interchangeable.
    """

    name = "sentence-transformers"

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model_name = model_name
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
        return self._model

    def fit(self, templates: list[str]) -> SentenceTransformerEmbedder:
        self._load()
        return self

    def transform(self, templates: list[str]) -> np.ndarray:
        model = self._load()
        return np.asarray(
            model.encode(templates, show_progress_bar=False, normalize_embeddings=True),
            dtype=np.float32,
        )


class ApiEmbedder(Embedder):
    """Hosted embeddings. Requires a provider SDK and an API key in the env."""

    name = "api"

    def __init__(self, model: str = "text-embedding-3-small", batch_size: int = 128):
        self.model = model
        self.batch_size = batch_size

    def fit(self, templates: list[str]) -> ApiEmbedder:
        return self

    def transform(self, templates: list[str]) -> np.ndarray:
        raise NotImplementedError(
            "Wire this to your embeddings provider. Batch in self.batch_size "
            "chunks, retry on 429, and cache by template hash - templates repeat "
            "constantly and you should never pay for the same one twice."
        )


def get_embedder(kind: str = "tfidf", **kwargs) -> Embedder:
    table = {
        "tfidf": TfidfEmbedder,
        "sentence-transformers": SentenceTransformerEmbedder,
        "api": ApiEmbedder,
    }
    if kind not in table:
        raise ValueError(f"unknown embedder {kind!r}; choose from {sorted(table)}")
    return table[kind](**kwargs)
