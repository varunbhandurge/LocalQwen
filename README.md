# BA-Account Swarm

A fully local, privacy-first multi-agent orchestrator powered by **FastAPI**, local **Ollama** (Qwen), and **SQLite**.

## Features

- **Local-first**: No cloud services. All data stays on disk via SQLite.
- **Swarm handoff**: Sequential agent pipeline with parallel-group classification.
- **Human-in-the-Loop (HITL)**: Approval checkpoints for sensitive operations.
- **Tool-calling agents**: Zoho CRM, JIRA, Gmail, Microsoft Graph, WhatsApp, SQLite queries.
- **OpenAI-compatible**: Works with Ollama (`qwen3.6` / `qwen2.5-coder`) or any OpenAI-compatible API.

## Prerequisites

- Python 3.11+
- [Ollama](https://ollama.com) installed & running on `http://localhost:11434`

```bash
# Pull the model
ollama pull qwen3.6    # or: ollama pull qwen2.5-coder
```

## Quick Start (Linux / macOS / WSL)

```bash
bash run_local.sh
```

## Quick Start (Windows)

```powershell
.\run_local.bat
```

**Or manually:**

```bash
# 1. Create venv & install deps
python -m venv venv
# Windows:  venv\Scripts\activate
# Linux/macOS: source venv/bin/activate
pip install -r requirements.txt

# 2. Configure credentials (optional)
cp .env.example .env
# Edit .env with your service credentials

# 3. Start the server
uvicorn app.main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000) in your browser for the Mission Control dashboard.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible API endpoint |
| `LLM_API_KEY` | `ollama` | API key for local model service |
| `LLM_MODEL` | `qwen3.6` | Full-resolution model name |
| `FAST_LLM_MODEL` | `qwen3.6` | Model used for intent classification |
| `ZOHO_CLIENT_ID` | — | Zoho CRM OAuth2 client ID |
| `ZOHO_CLIENT_SECRET` | — | Zoho CRM secret |
| `ZOHO_REFRESH_TOKEN` | — | Refresh token for CRM access |
| `JIRA_API_TOKEN` | — | Atlassian personal access token |
| `JIRA_EMAIL` | — | Account email / username |
| `GMAIL_CLIENT_EMAIL` | — | Gmail API service account |
| `GMAIL_PRIVATE_KEY_PATH` | — | Path to P12 key file |
| `MICROSOFT_GRAPH_*` | — | OAuth2 credentials for Graph API |
| `WHATSAPP_*` | — | Meta Cloud API credentials |

## Architecture

```
ba-account-swarm/
├── app/                  # FastAPI application layer
│   ├── __init__.py
│   ├── db.py             # SQLite persistence (built-in sqlite3)
│   ├── main.py           # Routes, WebSocket /chat
│   └── ...
├── swarm/                # Multi-agent engine
│   ├── __init__.py
│   ├── engine.py         # Orchestration loop + tool dispatch
│   ├── tools.py          # Tool schemas & async call wrappers
│   ├── prompts.py        # System guardrails + agent prompts
│   └── ...
├── ui/                   # Mission Control dashboard
│   ├── index.html
│   ├── main.css
│   └── app.js
├── tests/
│   └── test_swarm.py     # pytest suite (mocked)
├── requirements.txt
├── .env.example
├── run_local.sh / run_local.bat
└── README.md
```

### Swarm Flow

1. **ROUTE** receives the user query and classifies intent.
2. Intent classifier determines if parallel groups exist.
3. Agents execute sequentially or in parallel, calling tools as needed.
4. Each agent appends `next_agent: <name>` to signal handoff.
5. When no further handoff is needed (`next_agent: END`), the final result is returned.

### Agent Pipeline

| Agent | Role |
|-------|------|
| **ROUTE** | Initial intent classification & routing |
| **ZOHO_SWARM** | CRM contact search / record CRUD |
| **JIRA_SWARM** | Issue creation & JQL queries |
| **GMAIL_SWARM** | Email composition & send |
| **WORK_SWARM** | Calendar, profile, WhatsApp, SQLite ops |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Mission Control dashboard |
| `GET` | `/health` | Liveness probe |
| `POST` | `/swarm/run` | Kick off a swarm session (JSON body: `{query, caller}`) |
| `WS` | `/chat` | WebSocket live chat (JSON protocol) |
| `GET` | `/hitl/pending` | Pending HITL approvals |
| `POST` | `/hitl/action` | Approve/deny a HITL request |
| `POST` | `/context/save` | Save caller context |
| `GET` | `/context/get?caller=&key=` | Load caller context |
| `GET` | `/audit/{session_id}` | Full swarm execution trace |

## Testing

Requires `pytest`. Run from the repo root:

```bash
pip install pytest pytest-asyncio
cd ba-account-swarm   # or run python -m pytest from this directory
python -m pytest tests/ -v
```

The test suite uses **mock HTTP** and a **temporary SQLite database**. No external services are contacted.

## License

MIT
