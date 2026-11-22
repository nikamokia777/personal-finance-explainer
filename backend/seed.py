import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

from backend import rag
from backend.auth import hash_password
from backend.database import SessionLocal, engine
from backend.models import Category, KnowledgeArticle, User

SYSTEM_CATEGORIES = [
    ("სურსათი", "expense"),
    ("რესტორნები", "expense"),
    ("ტრანსპორტი", "expense"),
    ("კომუნალური", "expense"),
    ("ჯანმრთელობა", "expense"),
    ("გართობა", "expense"),
    ("შოპინგი", "expense"),
    ("გამოწერები", "expense"),
    ("სხვა", "expense"),
    ("ხელფასი", "income"),
]

KNOWLEDGE_FOLDER = Path(__file__).parent / "knowledge"


async def seed_categories() -> None:
    async with SessionLocal() as session:
        existing = set(await session.scalars(select(Category.name).where(Category.user_id.is_(None))))
        for name, kind in SYSTEM_CATEGORIES:
            if name not in existing:
                session.add(Category(name=name, kind=kind))
        await session.commit()


async def seed_knowledge() -> None:
    async with SessionLocal() as session:
        for path in sorted(KNOWLEDGE_FOLDER.glob("*.md")):
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            title = lines[0].lstrip("# ").strip()
            content = "\n".join(lines[1:]).strip()
            if await session.scalar(select(KnowledgeArticle).where(KnowledgeArticle.title == title)):
                continue
            article = KnowledgeArticle(title=title, content=content)
            session.add(article)
            await session.commit()
            chunks = await rag.index_article(session, article)
            print(f"{title}: {chunks} chunks")


async def create_admin(email: str, password: str) -> None:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.email == email.lower()))
        if user is None:
            user = User(email=email.lower(), full_name="Admin", hashed_password=hash_password(password))
            session.add(user)
        user.role = "admin"
        await session.commit()
        print(f"{email} is now an admin")


async def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "categories"
    if command == "categories":
        await seed_categories()
    elif command == "knowledge":
        await seed_knowledge()
    elif command == "admin":
        await create_admin(sys.argv[2], sys.argv[3])
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
