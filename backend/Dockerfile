FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
COPY tests /app/tests
COPY pyproject.toml /app/pyproject.toml

EXPOSE 8000

CMD ["sh", "-c", "alembic -c backend/alembic.ini upgrade head && python -m backend.seed categories && uvicorn backend.main:app --host 0.0.0.0 --port 8000"]
