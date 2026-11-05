"""LangChain retrieval + grounded answer chain."""
from __future__ import annotations

import asyncio
from pathlib import Path

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from ..settings import settings
from .store import vector_store

_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "你是医院就诊信息助手。只能根据给定的医院知识片段回答流程、服务和检查准备问题。"
     "不得给出诊断、分诊、用药、治疗或病情严重程度判断。资料未明确说明时，回答‘知识库暂未收录，请咨询医院工作人员’。"
     "不得把常识补充成医院规定。回答简明，并在适当处标注文档标题。\n\n知识片段：\n{context}"),
    ("human", "{question}"),
])


def _llm() -> ChatOpenAI:
    cfg = settings.llm
    base_url = str(cfg.get("base_url", "http://127.0.0.1:11434/v1"))
    return ChatOpenAI(
        model=str(cfg.get("model", "qwen2.5:7b")),
        base_url=base_url,
        api_key="ollama",
        temperature=0,
        timeout=float(settings.rag.get("timeout_seconds", 12)),
        max_tokens=700,
    )


async def answer_question(question: str) -> dict:
    if not settings.rag.get("enabled", False):
        raise RuntimeError("RAG 尚未启用；请在 config/app.json 中设置 rag.enabled=true")
    redline = ("吃什么药", "用什么药", "开药", "处方", "诊断", "我得了", "是不是癌",
               "能不能治", "严重吗", "症状", "胸口疼", "肚子疼")
    if any(term in question for term in redline):
        return {"answer": "我不能判断病情、诊断或提供用药建议。请联系医院工作人员或医生；如有紧急不适，请立即寻求现场医疗帮助。",
                "sources": [], "grounded": False, "reason": "MEDICAL_REDLINE"}
    try:
        store = vector_store()
        ranked = await asyncio.to_thread(
            store.similarity_search_with_relevance_scores,
            question,
            k=int(settings.rag.get("top_k", 4)),
        )
        cutoff = float(settings.rag.get("min_relevance", 0.25))
        docs = [doc for doc, score in ranked if score >= cutoff]
    except Exception as exc:  # index/model unavailable: fail closed; do not let LLM answer from memory
        raise RuntimeError("知识库检索暂不可用，请咨询医院工作人员") from exc

    if not docs:
        return {"answer": "知识库暂未收录，请咨询医院工作人员。", "sources": [], "grounded": False}

    context = "\n\n".join(f"[资料：{d.metadata.get('title', d.metadata.get('source', '医院资料'))}"
                           f" / {d.metadata.get('section', '')}]\n{d.page_content}" for d in docs)
    try:
        chain = _PROMPT | _llm() | StrOutputParser()
        answer = (await chain.ainvoke({"question": question, "context": context})).strip()
    except Exception as exc:
        raise RuntimeError("问答模型暂不可用，请稍后再试") from exc

    sources = []
    seen = set()
    for doc in docs:
        src = {"title": doc.metadata.get("title") or Path(doc.metadata.get("source", "资料")).stem,
               "section": doc.metadata.get("section"), "source": doc.metadata.get("source")}
        key = (src["source"], src["section"])
        if key not in seen:
            seen.add(key)
            sources.append(src)
    return {"answer": answer, "sources": sources, "grounded": True}
