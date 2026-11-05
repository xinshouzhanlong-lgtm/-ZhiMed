"""Shared construction of the local Chroma index and its embedding model."""
from __future__ import annotations

from functools import lru_cache

from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings

from ..settings import ROOT, settings


def _path(key: str) -> str:
    value = str(settings.rag.get(key, ""))
    path = ROOT / value
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


@lru_cache(maxsize=1)
def embeddings() -> HuggingFaceEmbeddings:
    return HuggingFaceEmbeddings(
        model_name=str(settings.rag.get("embedding_model", "BAAI/bge-m3")),
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )


def vector_store() -> Chroma:
    return Chroma(
        collection_name="hospital_approved_knowledge",
        embedding_function=embeddings(),
        persist_directory=_path("index_dir"),
    )
