import os

os.environ["POSTGRES_DB"] = "finance_test"
os.environ["JWT_SECRET"] = "test-secret-that-is-long-enough-for-hs256"

import asyncio
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select, text

from backend import agents, rag
from backend.auth import create_token, hash_password
from backend.database import Base, SessionLocal, engine
from backend.main import app
from backend.models import Category, Transaction, User
from backend.seed import seed_categories
from tests.fakes import FakeEmbeddings, FakeLLM


@pytest.fixture(scope="session", autouse=True)
async def database():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    agents.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    app.state.import_graph = agents.build_import_graph(InMemorySaver())
    app.state.chat_graph = agents.build_chat_graph(InMemorySaver())
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_tables():
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    await seed_categories()


@pytest.fixture
def fake_llm(monkeypatch):
    llm = FakeLLM()
    monkeypatch.setattr(agents, "get_llm", lambda: llm)
    return llm


@pytest.fixture
async def fake_embeddings(monkeypatch):
    monkeypatch.setattr(rag, "get_embeddings", lambda: FakeEmbeddings())
    monkeypatch.setattr(rag, "MIN_SCORE", 0.2)
    rag.get_store.cache_clear()
    async with engine.begin() as connection:
        if await connection.scalar(text("SELECT to_regclass('langchain_pg_embedding') IS NOT NULL")):
            await connection.execute(text("DELETE FROM langchain_pg_embedding"))
    yield
    rag.get_store.cache_clear()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client


async def create_user(email="user@example.com", role="user"):
    async with SessionLocal() as session:
        user = User(email=email, full_name="Test", hashed_password=hash_password("password123"), role=role)
        session.add(user)
        await session.commit()
        return user.id, {"Authorization": f"Bearer {create_token(user.id)}"}


async def add_transaction(user_id, day, amount, merchant, category=None, kind="expense"):
    async with SessionLocal() as session:
        category_id = None
        if category:
            category_id = await session.scalar(select(Category.id).where(Category.name == category))
        session.add(Transaction(
            user_id=user_id,
            date=date.fromisoformat(day),
            amount=Decimal(amount),
            type=kind,
            description=merchant,
            merchant=merchant,
            category_id=category_id,
            fingerprint=f"{merchant}-{day}-{amount}-{category}",
        ))
        await session.commit()


async def wait_for_import(client, headers, import_id):
    for _ in range(100):
        record = (await client.get(f"/imports/{import_id}", headers=headers)).json()
        if record["status"] in ("completed", "failed", "awaiting_review"):
            return record
        await asyncio.sleep(0.05)
    return record
