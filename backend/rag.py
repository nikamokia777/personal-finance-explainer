import os
from functools import lru_cache

from langchain_core.documents import Document
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_postgres import PGVector
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sqlalchemy import text

from backend.database import VECTOR_DATABASE_URL

COLLECTION_NAME = "finance_knowledge"
MIN_SCORE = 0.65

splitter = RecursiveCharacterTextSplitter(chunk_size=700, chunk_overlap=90)


def get_embeddings():
    return GoogleGenerativeAIEmbeddings(model=os.getenv("GEMINI_EMBEDDING_MODEL", "models/gemini-embedding-001"))


@lru_cache
def get_store() -> PGVector:
    return PGVector(
        embeddings=get_embeddings(),
        collection_name=COLLECTION_NAME,
        connection=VECTOR_DATABASE_URL,
        use_jsonb=True,
        async_mode=True,
    )


async def delete_article_chunks(session, article_id: int) -> None:
    table_exists = await session.scalar(text("SELECT to_regclass('langchain_pg_embedding') IS NOT NULL"))
    if table_exists:
        await session.execute(
            text("DELETE FROM langchain_pg_embedding WHERE cmetadata->>'article_id' = :article_id"),
            {"article_id": str(article_id)},
        )
        await session.commit()


async def index_article(session, article) -> int:
    await delete_article_chunks(session, article.id)
    if not article.is_published:
        return 0
    chunks = splitter.split_text(f"{article.title}\n\n{article.content}")
    documents = [
        Document(page_content=chunk, metadata={"article_id": article.id, "title": article.title}) for chunk in chunks
    ]
    await get_store().aadd_documents(documents)
    return len(documents)


async def search_knowledge(question: str) -> list[dict]:
    results = await get_store().asimilarity_search_with_relevance_scores(question, k=3)
    return [
        {"title": document.metadata["title"], "content": document.page_content, "score": round(score, 3)}
        for document, score in results
        if score >= MIN_SCORE
    ]
