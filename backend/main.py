import asyncio
import hashlib
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend import agents, finance, rag
from backend.auth import create_token, get_admin_user, get_current_user, hash_password, verify_password
from backend.csv_import import clean_merchant, mask_pii
from backend.database import CHECKPOINT_DATABASE_URL, SessionLocal, get_session
from backend.models import (
    Budget,
    Category,
    ChatMessage,
    ChatSession,
    Import,
    KnowledgeArticle,
    MerchantRule,
    MonthlyReport,
    Transaction,
    User,
)
from backend.prompts import UNAVAILABLE_TEXT
from backend.schemas import (
    ArticleRequest,
    BudgetUpdate,
    CategoryCreate,
    CategoryUpdate,
    ChatMessageRequest,
    LoginRequest,
    ProfileUpdate,
    RegisterRequest,
    ReviewRequest,
    TokenResponse,
    TransactionCreate,
    TransactionUpdate,
    UserResponse,
)

logging.basicConfig(level=logging.INFO)

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
IMPORTS_PER_HOUR = 10
MESSAGES_PER_HOUR = 30


@asynccontextmanager
async def lifespan(app: FastAPI):
    agents.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    pool = AsyncConnectionPool(
        conninfo=CHECKPOINT_DATABASE_URL,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await pool.open()
    checkpointer = AsyncPostgresSaver(pool)
    await checkpointer.setup()
    app.state.import_graph = agents.build_import_graph(checkpointer)
    app.state.chat_graph = agents.build_chat_graph(checkpointer)
    yield
    await pool.close()


app = FastAPI(title="Personal Finance Explainer", lifespan=lifespan)


def one_hour_ago() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=1)


def month_or_422(month: str | None) -> date:
    try:
        return finance.parse_month(month)
    except ValueError:
        raise HTTPException(status_code=422, detail="month must look like 2026-09")


def category_to_dict(category: Category) -> dict:
    return {"id": category.id, "name": category.name, "kind": category.kind, "is_system": category.user_id is None}


def transaction_to_dict(transaction: Transaction, category_name: str | None) -> dict:
    return {
        "id": transaction.id,
        "date": transaction.date.isoformat(),
        "amount": str(transaction.amount),
        "type": transaction.type,
        "description": transaction.description,
        "merchant": transaction.merchant,
        "category_id": transaction.category_id,
        "category_name": category_name,
        "suggested_category_id": transaction.suggested_category_id,
        "category_source": transaction.category_source,
        "category_confidence": transaction.category_confidence,
        "was_corrected": transaction.was_corrected,
    }


def import_to_dict(record: Import) -> dict:
    return {
        "id": record.id,
        "filename": record.filename,
        "status": record.status,
        "current_step": record.current_step,
        "rows_total": record.rows_total,
        "rows_imported": record.rows_imported,
        "duplicates_skipped": record.duplicates_skipped,
        "needs_review_count": record.needs_review_count,
        "errors": record.errors,
        "error_message": record.error_message,
        "summary": record.summary,
        "created_at": record.created_at.isoformat(),
    }


async def get_visible_category(session: AsyncSession, user: User, category_id: int) -> Category:
    category = await session.get(Category, category_id)
    if category is None or category.user_id not in (None, user.id):
        raise HTTPException(status_code=404, detail="Category not found")
    return category


async def get_own_import(session: AsyncSession, user: User, import_id: int) -> Import:
    record = await session.get(Import, import_id)
    if record is None or record.user_id != user.id:
        raise HTTPException(status_code=404, detail="Import not found")
    return record


async def get_own_chat(session: AsyncSession, user: User, session_id: int) -> ChatSession:
    chat = await session.get(ChatSession, session_id)
    if chat is None or chat.user_id != user.id:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


def sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/auth/register", response_model=UserResponse, status_code=201)
async def register(body: RegisterRequest, session: AsyncSession = Depends(get_session)):
    email = body.email.lower()
    if await session.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=409, detail="Email is already registered")
    user = User(email=email, full_name=body.full_name, hashed_password=hash_password(body.password))
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@app.post("/auth/login", response_model=TokenResponse)
async def login(body: LoginRequest, session: AsyncSession = Depends(get_session)):
    user = await session.scalar(select(User).where(User.email == body.email.lower()))
    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Wrong email or password")
    return TokenResponse(access_token=create_token(user.id), user=UserResponse.model_validate(user))


@app.get("/auth/me", response_model=UserResponse)
async def me(user: User = Depends(get_current_user)):
    return user


@app.put("/auth/me", response_model=UserResponse)
async def update_me(body: ProfileUpdate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    if body.full_name is not None:
        user.full_name = body.full_name
    if body.currency is not None:
        user.currency = body.currency
    await session.commit()
    await session.refresh(user)
    return user


@app.get("/categories")
async def list_categories(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    categories = await agents.user_categories(session, user.id)
    categories.sort(key=lambda category: (category.user_id is not None, category.name))
    return [category_to_dict(category) for category in categories]


@app.post("/categories", status_code=201)
async def create_category(body: CategoryCreate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    if body.system and user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can create system categories")
    names = [category.name.lower() for category in await agents.user_categories(session, user.id)]
    if body.name.lower() in names:
        raise HTTPException(status_code=409, detail="A category with this name already exists")
    category = Category(user_id=None if body.system else user.id, name=body.name, kind=body.kind)
    session.add(category)
    await session.commit()
    return category_to_dict(category)


@app.put("/categories/{category_id}")
async def update_category(category_id: int, body: CategoryUpdate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    category = await get_visible_category(session, user, category_id)
    if category.user_id is None and user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can change system categories")
    category.name = body.name
    await session.commit()
    return category_to_dict(category)


@app.delete("/categories/{category_id}", status_code=204)
async def delete_category(category_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    category = await get_visible_category(session, user, category_id)
    if category.user_id is None and user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can change system categories")
    used = await session.scalar(select(func.count()).select_from(Transaction).where(Transaction.category_id == category.id))
    if used:
        raise HTTPException(status_code=409, detail="This category has transactions")
    await session.delete(category)
    await session.commit()


@app.get("/transactions")
async def list_transactions(
    date_from: date | None = None,
    date_to: date | None = None,
    category_id: int | None = None,
    type: str | None = None,
    search: str | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    uncategorized: bool = False,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    query = (
        select(Transaction, Category.name)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(Transaction.user_id == user.id)
    )
    if date_from:
        query = query.where(Transaction.date >= date_from)
    if date_to:
        query = query.where(Transaction.date <= date_to)
    if category_id:
        query = query.where(Transaction.category_id == category_id)
    if type:
        query = query.where(Transaction.type == type)
    if search:
        query = query.where(or_(Transaction.description.ilike(f"%{search}%"), Transaction.merchant.ilike(f"%{search}%")))
    if min_amount is not None:
        query = query.where(Transaction.amount >= min_amount)
    if max_amount is not None:
        query = query.where(Transaction.amount <= max_amount)
    if uncategorized:
        query = query.where(Transaction.category_id.is_(None))

    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    query = query.order_by(Transaction.date.desc(), Transaction.id.desc()).offset((page - 1) * size).limit(size)
    rows = await session.execute(query)
    return {"items": [transaction_to_dict(t, name) for t, name in rows], "total": total, "page": page, "size": size}


@app.post("/transactions", status_code=201)
async def create_transaction(body: TransactionCreate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    description = mask_pii(body.description)
    merchant = clean_merchant(description)
    transaction = Transaction(
        user_id=user.id,
        date=body.date,
        amount=body.amount,
        type=body.type,
        description=description,
        merchant=merchant,
        fingerprint=hashlib.sha256(f"manual-{uuid.uuid4()}".encode()).hexdigest(),
    )
    if body.category_id is not None:
        category = await get_visible_category(session, user, body.category_id)
        if category.kind != body.type:
            raise HTTPException(status_code=422, detail="Category type does not match the transaction type")
        transaction.category_id = category.id
        transaction.category_source = "user"
    else:
        rule = await session.scalar(
            select(MerchantRule).where(MerchantRule.user_id == user.id, MerchantRule.pattern == merchant)
        )
        if rule is not None:
            transaction.category_id = rule.category_id
            transaction.category_source = "rule"
            transaction.category_confidence = 1.0
    session.add(transaction)
    await session.commit()
    return transaction_to_dict(transaction, None)


@app.patch("/transactions/{transaction_id}")
async def update_transaction(transaction_id: int, body: TransactionUpdate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    transaction = await session.get(Transaction, transaction_id)
    if transaction is None or transaction.user_id != user.id:
        raise HTTPException(status_code=404, detail="Transaction not found")
    category = await get_visible_category(session, user, body.category_id)
    if category.kind != transaction.type:
        raise HTTPException(status_code=422, detail="Category type does not match the transaction type")

    ai_guess = transaction.suggested_category_id
    if transaction.category_source == "ai":
        ai_guess = transaction.category_id
    transaction.was_corrected = ai_guess is not None and ai_guess != category.id
    transaction.category_id = category.id
    transaction.category_source = "user"
    transaction.category_confidence = None
    if body.remember:
        await agents.save_merchant_rule(session, user.id, transaction.merchant, category.id)
    await session.commit()
    return transaction_to_dict(transaction, category.name)


@app.delete("/transactions/{transaction_id}", status_code=204)
async def delete_transaction(transaction_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    transaction = await session.get(Transaction, transaction_id)
    if transaction is None or transaction.user_id != user.id:
        raise HTTPException(status_code=404, detail="Transaction not found")
    await session.delete(transaction)
    await session.commit()


@app.get("/budgets")
async def list_budgets(month: str | None = None, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await finance.budget_status(session, user.id, month_or_422(month))


@app.put("/budgets/{category_id}")
async def set_budget(category_id: int, body: BudgetUpdate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    category = await get_visible_category(session, user, category_id)
    if category.kind != "expense":
        raise HTTPException(status_code=422, detail="Budgets can only be set for expense categories")
    budget = await session.scalar(select(Budget).where(Budget.user_id == user.id, Budget.category_id == category.id))
    if budget is None:
        session.add(Budget(user_id=user.id, category_id=category.id, monthly_limit=body.monthly_limit))
    else:
        budget.monthly_limit = body.monthly_limit
    await session.commit()
    return {"category_id": category.id, "category": category.name, "monthly_limit": str(body.monthly_limit)}


@app.delete("/budgets/{category_id}", status_code=204)
async def delete_budget(category_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    budget = await session.scalar(select(Budget).where(Budget.user_id == user.id, Budget.category_id == category_id))
    if budget is None:
        raise HTTPException(status_code=404, detail="Budget not found")
    await session.delete(budget)
    await session.commit()


@app.post("/imports", status_code=202)
async def upload_statement(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=422, detail="Only .csv files can be uploaded")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="The file is larger than 2 MB")
    if not content.strip():
        raise HTTPException(status_code=422, detail="The file is empty")
    recent = await session.scalar(
        select(func.count()).select_from(Import).where(Import.user_id == user.id, Import.created_at >= one_hour_ago())
    )
    if recent >= IMPORTS_PER_HOUR:
        raise HTTPException(status_code=429, detail="Too many imports, try again later")

    record = Import(user_id=user.id, filename=file.filename[:255])
    session.add(record)
    await session.commit()
    (agents.UPLOAD_DIR / f"import-{record.id}.csv").write_bytes(content)
    background_tasks.add_task(agents.run_import, request.app.state.import_graph, record.id, user.id)
    return {"id": record.id, "status": record.status}


@app.get("/imports")
async def list_imports(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    records = await session.scalars(select(Import).where(Import.user_id == user.id).order_by(Import.id.desc()))
    return [import_to_dict(record) for record in records]


@app.get("/imports/{import_id}")
async def get_import(import_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return import_to_dict(await get_own_import(session, user, import_id))


@app.get("/imports/{import_id}/stream")
async def stream_import(import_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await get_own_import(session, user, import_id)

    async def events():
        last_seen = None
        for _ in range(600):
            async with SessionLocal() as stream_session:
                record = await stream_session.get(Import, import_id)
            if (record.status, record.current_step) != last_seen:
                last_seen = (record.status, record.current_step)
                yield sse({"type": "progress", "status": record.status, "step": record.current_step})
            if record.status in ("completed", "failed", "awaiting_review"):
                yield sse({"type": "done", "import": import_to_dict(record)})
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/imports/{import_id}/review")
async def get_review(import_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await get_own_import(session, user, import_id)
    query = (
        select(Transaction, Category.name)
        .outerjoin(Category, Category.id == Transaction.suggested_category_id)
        .where(Transaction.import_id == import_id, Transaction.user_id == user.id, Transaction.category_id.is_(None))
        .order_by(Transaction.merchant)
    )
    rows = await session.execute(query)
    return [
        {
            "transaction_id": t.id,
            "date": t.date.isoformat(),
            "amount": str(t.amount),
            "type": t.type,
            "description": t.description,
            "merchant": t.merchant,
            "suggested_category_id": t.suggested_category_id,
            "suggested_category_name": suggested_name,
            "confidence": t.category_confidence,
        }
        for t, suggested_name in rows
    ]


@app.post("/imports/{import_id}/review")
async def submit_review(import_id: int, body: ReviewRequest, request: Request, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    record = await get_own_import(session, user, import_id)
    if record.status != "awaiting_review":
        raise HTTPException(status_code=409, detail="This import is not waiting for review")

    for decision in body.decisions:
        transaction = await session.get(Transaction, decision.transaction_id)
        if transaction is None or transaction.import_id != import_id or transaction.category_id is not None:
            raise HTTPException(status_code=422, detail=f"Transaction {decision.transaction_id} is not waiting for review")
        category = await get_visible_category(session, user, decision.category_id)
        if category.kind != transaction.type:
            raise HTTPException(status_code=422, detail="Category type does not match the transaction type")

    record.status = "classifying"
    record.current_step = "apply_review"
    await session.commit()
    await agents.resume_import(request.app.state.import_graph, import_id, [d.model_dump() for d in body.decisions])
    await session.refresh(record)
    return import_to_dict(record)


@app.get("/summary")
async def get_summary(month: str | None = None, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    start = month_or_422(month)
    end = finance.month_end(start)
    return {
        "spending": await finance.spending_summary(session, user.id, start, end),
        "comparison": await finance.compare_periods(session, user.id, start, end),
    }


@app.get("/summary/recurring")
async def get_recurring(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await finance.find_subscriptions(session, user.id, date.today())


@app.get("/summary/anomalies")
async def get_anomalies(month: str | None = None, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    start = month_or_422(month)
    return await finance.find_anomalies(session, user.id, start, finance.month_end(start))


def report_to_dict(report: MonthlyReport) -> dict:
    return {
        "month": report.month.strftime("%Y-%m"),
        "explanation": report.explanation,
        "facts": report.facts,
        "created_at": report.created_at.isoformat(),
    }


@app.post("/reports/{month}")
async def create_report(month: str, request: Request, force: bool = False, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    start = month_or_422(month)
    report = await session.scalar(select(MonthlyReport).where(MonthlyReport.user_id == user.id, MonthlyReport.month == start))
    if report is not None and not force:
        return report_to_dict(report)

    config = {"configurable": {"thread_id": f"report-{user.id}-{month}"}, "tags": ["report"]}
    state = await request.app.state.chat_graph.ainvoke(
        {
            "messages": [HumanMessage(f"თვიური ანგარიში {month}")],
            "user_id": user.id,
            "today": date.today().isoformat(),
            "report_month": month,
            "intent": "monthly_report",
            "analysis": {"period": "custom", "start_date": start.isoformat(), "end_date": finance.month_end(start).isoformat()},
            "knowledge_chunks": [],
            "number_issues": [],
            "attempts": 0,
            "answer": "",
        },
        config,
    )
    explanation = {"text": state["answer"], "draft": state.get("draft")}
    if report is None:
        report = MonthlyReport(user_id=user.id, month=start, facts=state["facts"], explanation=explanation)
        session.add(report)
    else:
        report.facts = state["facts"]
        report.explanation = explanation
    await session.commit()
    await session.refresh(report)
    return report_to_dict(report)


@app.get("/reports")
async def list_reports(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    reports = await session.scalars(select(MonthlyReport).where(MonthlyReport.user_id == user.id).order_by(MonthlyReport.month.desc()))
    return [{"month": r.month.strftime("%Y-%m"), "created_at": r.created_at.isoformat()} for r in reports]


@app.get("/reports/{month}")
async def get_report(month: str, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    start = month_or_422(month)
    report = await session.scalar(select(MonthlyReport).where(MonthlyReport.user_id == user.id, MonthlyReport.month == start))
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return report_to_dict(report)


@app.post("/chat/sessions", status_code=201)
async def create_chat(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    chat = ChatSession(user_id=user.id)
    session.add(chat)
    await session.commit()
    return {"id": chat.id, "title": chat.title}


@app.get("/chat/sessions")
async def list_chats(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    chats = await session.scalars(select(ChatSession).where(ChatSession.user_id == user.id).order_by(ChatSession.id.desc()))
    return [{"id": chat.id, "title": chat.title} for chat in chats]


@app.get("/chat/sessions/{session_id}/messages")
async def list_messages(session_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await get_own_chat(session, user, session_id)
    messages = await session.scalars(select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id))
    return [{"role": message.role, "content": message.content} for message in messages]


@app.post("/chat/sessions/{session_id}/messages")
async def send_message(session_id: int, body: ChatMessageRequest, request: Request, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    chat = await get_own_chat(session, user, session_id)
    sent = await session.scalar(
        select(func.count())
        .select_from(ChatMessage)
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .where(ChatSession.user_id == user.id, ChatMessage.role == "user", ChatMessage.created_at >= one_hour_ago())
    )
    if sent >= MESSAGES_PER_HOUR:
        raise HTTPException(status_code=429, detail="Too many messages, try again later")

    session.add(ChatMessage(session_id=chat.id, role="user", content=body.content))
    if chat.title == "New chat":
        chat.title = body.content[:60]
    await session.commit()

    graph = request.app.state.chat_graph
    config = {"configurable": {"thread_id": f"chat-{session_id}"}, "tags": ["chat"], "metadata": {"session_id": session_id}}
    inputs = {"messages": [HumanMessage(body.content)], "user_id": user.id, "today": date.today().isoformat()}

    async def events():
        try:
            async for update in graph.astream(inputs, config, stream_mode="updates"):
                for step in update:
                    yield sse({"type": "step", "name": step})
            state = await graph.aget_state(config)
            answer = state.values.get("answer") or UNAVAILABLE_TEXT
        except Exception as error:
            logging.error("Chat %s failed (%s)", session_id, type(error).__name__)
            answer = UNAVAILABLE_TEXT
        async with SessionLocal() as answer_session:
            answer_session.add(ChatMessage(session_id=session_id, role="assistant", content=answer))
            await answer_session.commit()
        yield sse({"type": "answer", "text": answer})

    return StreamingResponse(events(), media_type="text/event-stream")


@app.delete("/chat/sessions/{session_id}", status_code=204)
async def delete_chat(session_id: int, request: Request, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    chat = await get_own_chat(session, user, session_id)
    await session.delete(chat)
    await session.commit()
    await request.app.state.chat_graph.checkpointer.adelete_thread(f"chat-{session_id}")


@app.get("/knowledge/articles")
async def list_articles(admin: User = Depends(get_admin_user), session: AsyncSession = Depends(get_session)):
    articles = await session.scalars(select(KnowledgeArticle).order_by(KnowledgeArticle.id))
    return [{"id": a.id, "title": a.title, "content": a.content, "is_published": a.is_published} for a in articles]


@app.post("/knowledge/articles", status_code=201)
async def create_article(body: ArticleRequest, admin: User = Depends(get_admin_user), session: AsyncSession = Depends(get_session)):
    article = KnowledgeArticle(title=body.title, content=body.content, is_published=body.is_published)
    session.add(article)
    await session.commit()
    try:
        chunks = await rag.index_article(session, article)
    except Exception:
        await session.delete(article)
        await session.commit()
        raise HTTPException(status_code=503, detail="Could not index the article")
    return {"id": article.id, "title": article.title, "chunks": chunks}


@app.put("/knowledge/articles/{article_id}")
async def update_article(article_id: int, body: ArticleRequest, admin: User = Depends(get_admin_user), session: AsyncSession = Depends(get_session)):
    article = await session.get(KnowledgeArticle, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    article.title = body.title
    article.content = body.content
    article.is_published = body.is_published
    await session.commit()
    try:
        chunks = await rag.index_article(session, article)
    except Exception:
        raise HTTPException(status_code=503, detail="Could not index the article")
    return {"id": article.id, "title": article.title, "chunks": chunks}


@app.delete("/knowledge/articles/{article_id}", status_code=204)
async def delete_article(article_id: int, admin: User = Depends(get_admin_user), session: AsyncSession = Depends(get_session)):
    article = await session.get(KnowledgeArticle, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    await rag.delete_article_chunks(session, article.id)
    await session.delete(article)
    await session.commit()


@app.get("/analytics/overview")
async def analytics_overview(admin: User = Depends(get_admin_user), session: AsyncSession = Depends(get_session)):
    return {
        "users": await session.scalar(select(func.count()).select_from(User)),
        "imports": await session.scalar(select(func.count()).select_from(Import)),
        "transactions": await session.scalar(select(func.count()).select_from(Transaction)),
    }


@app.get("/analytics/categorization-sources")
async def analytics_sources(admin: User = Depends(get_admin_user), session: AsyncSession = Depends(get_session)):
    rows = await session.execute(select(Transaction.category_source, func.count()).group_by(Transaction.category_source))
    return {source or "uncategorized": count for source, count in rows}


@app.get("/analytics/ai-accuracy")
async def analytics_accuracy(admin: User = Depends(get_admin_user), session: AsyncSession = Depends(get_session)):
    ai_guessed = or_(Transaction.category_source == "ai", Transaction.suggested_category_id.is_not(None))
    guessed = await session.scalar(select(func.count()).select_from(Transaction).where(ai_guessed))
    corrected = await session.scalar(select(func.count()).select_from(Transaction).where(Transaction.was_corrected.is_(True)))
    return {"ai_guesses": guessed, "corrected_by_users": corrected, "accepted": guessed - corrected}
