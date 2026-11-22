import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

import backend.models
from backend.database import ASYNC_DATABASE_URL, Base

target_metadata = Base.metadata


def include_name(name, type_, parent_names):
    return type_ != "table" or name in target_metadata.tables


def run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata, include_name=include_name)
    with context.begin_transaction():
        context.run_migrations()


async def main():
    engine = create_async_engine(ASYNC_DATABASE_URL)
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
    await engine.dispose()


asyncio.run(main())
