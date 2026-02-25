"""Vector service that combines Chroma persistence with Doubao embedding API."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .config import get_settings

try:  # pragma: no cover - optional dependency
    import chromadb
except Exception:  # pylint: disable=broad-except
    chromadb = None  # type: ignore


class DoubaoEmbeddingFunction:
    """Embedding function adapter compatible with Chroma."""

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.model = model
        self.endpoint = base_url.rstrip("/") + "/embeddings"

    def _fetch_embeddings(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        payload = {
            "model": self.model,
            "input": texts,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        response = requests.post(self.endpoint, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        embeddings = data.get("data") or []
        return [item.get("embedding") for item in embeddings]

    def __call__(self, input: List[str]) -> List[List[float]]:  # pylint: disable=redefined-builtin
        return self.embed_documents(input)

    def embed_documents(self, texts: List[str], **_) -> List[List[float]]:
        return self._fetch_embeddings(texts)

    def embed_query(self, text: str | None = None, **kwargs) -> List[List[float]]:
        # Chroma>=0.5 passes query texts as list/str; normalize to list of strings
        inputs: List[str] = []
        if text is not None:
            inputs = [text]
        else:
            payload = kwargs.get("input")
            if isinstance(payload, list):
                inputs = [str(item) for item in payload if item is not None]
            elif isinstance(payload, str):
                inputs = [payload]
        if not inputs:
            inputs = [""]
        return self._fetch_embeddings(inputs)


class VectorService:
    """Wrapper around a Chroma collection backed by Doubao embeddings."""

    def __init__(
        self,
        *,
        persist_directory: Optional[Path] = None,
        collection_name: str = "evaluation_docs",
    ):
        settings = get_settings()
        self.persist_directory = persist_directory or (
            Path(__file__).resolve().parent.parent / "storage" / "chroma"
        )
        self.collection_name = collection_name
        self._collection = None
        self._api_available = all(
            [settings.embedding_api_key, settings.embedding_base_url, settings.embedding_model]
        )

        if chromadb is not None and self._api_available:
            self.persist_directory.mkdir(parents=True, exist_ok=True)
            embed_fn = DoubaoEmbeddingFunction(
                api_key=settings.embedding_api_key,
                base_url=settings.embedding_base_url,
                model=settings.embedding_model,
            )
            client = chromadb.PersistentClient(path=str(self.persist_directory))
            self._collection = client.get_or_create_collection(
                name=self.collection_name,
                embedding_function=embed_fn,
            )

    @property
    def available(self) -> bool:
        return self._collection is not None

    async def add_document(
        self,
        *,
        request_id: str,
        step_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not content.strip() or not self.available:
            return

        def _upsert():
            assert self._collection is not None
            doc_id = f"{request_id}_{step_id}_{uuid.uuid4().hex[:8]}"
            meta = {"request_id": request_id, "step_id": step_id}
            if metadata:
                meta.update(metadata)
            self._collection.upsert(documents=[content], ids=[doc_id], metadatas=[meta])

        await asyncio.to_thread(_upsert)

    async def search(
        self,
        *,
        query: str,
        request_id: Optional[str] = None,
        limit: int = 3,
    ) -> List[str]:
        if not query.strip() or not self.available:
            return []

        def _query():
            assert self._collection is not None
            where = {"request_id": request_id} if request_id else None
            result = self._collection.query(
                query_texts=[query],
                n_results=limit,
                where=where,
            )
            documents = result.get("documents") or [[]]
            metadatas = result.get("metadatas") or [[]]
            hits = []
            for doc, meta in zip(documents[0], metadatas[0]):
                title = meta.get("step_title") if isinstance(meta, dict) else None
                hits.append(f"{title or '相关内容'}：{doc[:200]}")
            return hits

        return await asyncio.to_thread(_query)
