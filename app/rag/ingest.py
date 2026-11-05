"""Build the persistent index from markdown files in the approved knowledge folder.

Run with: python -m app.rag.ingest
Only reviewed, approved materials belong in this directory.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from ..settings import ROOT, settings
from .store import vector_store


def build_index() -> int:
    source = ROOT / str(settings.rag.get("knowledge_dir", "data/knowledge/approved"))
    source.mkdir(parents=True, exist_ok=True)
    files = sorted(source.rglob("*.md"))
    if not files:
        raise RuntimeError(f"知识目录没有 Markdown 文件：{source}")

    loader = DirectoryLoader(str(source), glob="**/*.md", loader_cls=TextLoader,
                             loader_kwargs={"encoding": "utf-8"}, show_progress=False)
    raw_docs = loader.load()
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("#", "title"), ("##", "section"), ("###", "subsection")],
        strip_headers=False,
    )
    docs = []
    for doc in raw_docs:
        relative = Path(doc.metadata["source"]).resolve().relative_to(source.resolve()).as_posix()
        for part in header_splitter.split_text(doc.page_content):
            part.metadata.update({"source": relative, "review_status": "approved"})
            docs.append(part)

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=int(settings.rag.get("chunk_size", 700)),
        chunk_overlap=int(settings.rag.get("chunk_overlap", 100)),
        add_start_index=True,
    )
    chunks = splitter.split_documents(docs)
    # Stable IDs make rebuilding idempotent and ensure changed documents replace old chunks.
    ids = [hashlib.sha256((d.metadata["source"] + "\0" + d.page_content).encode()).hexdigest()
           for d in chunks]
    store = vector_store()
    store.reset_collection()
    store.add_documents(chunks, ids=ids)
    return len(chunks)


if __name__ == "__main__":
    count = build_index()
    print(f"已建立索引，共 {count} 个知识片段")
