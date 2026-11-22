# Personal Finance Explainer

Upload a bank statement (CSV). The app sorts the transactions into categories and explains your
spending in plain language. **Code calculates every number; the AI only explains them.**

Stack: FastAPI, LangGraph + LangChain, Gemini, PostgreSQL + pgvector, SQLAlchemy + Alembic,
JWT, Streamlit, Docker Compose, pytest.

## Run it

1. Start **Docker Desktop**.
2. Create `.env` from `.env.example` and set `JWT_SECRET` and `GOOGLE_API_KEY`.
3. Start the three containers (postgres, backend, frontend):

```bash
docker compose up -d --build
```

4. Load the finance knowledge base (once):

```bash
docker compose exec backend python -m backend.seed knowledge
```

5. Open http://localhost:8501, register, and upload the files from `sample_data/`.

Make an admin account:

```bash
docker compose exec backend python -m backend.seed admin admin@example.com your-password
```

Run the tests:

```bash
docker compose exec backend pytest
```

Stop everything (your data is kept):

```bash
docker compose down
```

## Files

| File | What it does |
|---|---|
| `backend/main.py` | All API endpoints |
| `backend/database.py` | Database connection settings |
| `backend/models.py` | Database tables |
| `backend/schemas.py` | Shapes of requests, responses and AI answers |
| `backend/auth.py` | Passwords, JWT tokens, current user, admin check |
| `backend/csv_import.py` | Reading the CSV, hiding card numbers and IBANs, cleaning merchant names |
| `backend/finance.py` | All calculations: totals, comparisons, budgets, subscriptions, unusual spending, savings |
| `backend/agents.py` | The two LangGraph graphs: importing statements and the chat assistant |
| `backend/prompts.py` | The instructions sent to the AI |
| `backend/rag.py` | Knowledge base search (pgvector) |
| `backend/seed.py` | System categories, knowledge articles, admin account |
| `frontend/app.py` | The Streamlit app |
| `frontend/api_client.py` | Every call from the frontend to the backend |
| `tests/` | Tests with a fake AI model |

## How it works

**Import graph:** `parse_csv` → `apply_rules` → `classify_unknown` (AI, only for unknown merchants)
→ `save_transactions` → `confirm_categories` (pauses and waits for the user when the AI is unsure)
→ `apply_review` → `finish_import`.

**Chat graph:** `understand_question` → `gather_facts` (code) or `retrieve_knowledge` (articles)
→ `write_explanation` (AI) → `verify_numbers` (code). If the AI writes a number that is not in the
facts, it gets one more try; after that, `fallback_template` writes the answer from the facts.
Questions about investments or crypto go to `polite_refusal`.

## Deploy (DigitalOcean + nginx)

nginx runs on the droplet itself and forwards port 80 to Streamlit. The three containers stay the
same as on your computer, and their ports are only reachable from inside the droplet.

1. Create an Ubuntu 24.04 droplet and add your SSH key to it.
2. On the droplet, as root:

```bash
curl -fsSL https://raw.githubusercontent.com/nikamokia777/personal-finance-explainer/main/deploy/setup_server.sh -o setup_server.sh
bash setup_server.sh https://github.com/nikamokia777/personal-finance-explainer.git "<deploy public key>"
```

3. Put your Gemini key into the server `.env`, then start the app as the `deploy` user:

```bash
su - deploy
cd finance-explainer
nano .env
docker compose up -d --build
docker compose exec backend python -m backend.seed knowledge
```

4. In GitHub → Settings → Secrets and variables → Actions add `DROPLET_IP` and `DEPLOY_SSH_KEY`.
   From then on every push to `main` runs the tests and, if they pass, redeploys the droplet.
