import os

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

load_dotenv()

POSTGRES_USER = os.getenv("POSTGRES_USER", "finance")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "finance")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5433")
POSTGRES_DB = os.getenv("POSTGRES_DB", "finance")

DATABASE_ADDRESS = f"{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
ASYNC_DATABASE_URL = f"postgresql+asyncpg://{DATABASE_ADDRESS}"
CHECKPOINT_DATABASE_URL = f"postgresql://{DATABASE_ADDRESS}"
VECTOR_DATABASE_URL = f"postgresql+psycopg://{DATABASE_ADDRESS}"

engine = create_async_engine(ASYNC_DATABASE_URL)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session():
    async with SessionLocal() as session:
        yield session
