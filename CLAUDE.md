# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

Three-service system for a WhatsApp barbershop assistant:

```
WhatsApp ↔ Evolution API (port 8082) → FastAPI Gateway (port 8000) → Groq LLM
                                                ↕ MCP JSON-RPC
                                         MCP Server (port 8001)
                                                ↕
                                   SQLite (data/barbershop.db)
                                   Google Calendar API
```

- **`Gateway.py`** — FastAPI app. Receives Evolution API webhooks, manages conversation history via MCP session, calls Groq (`llama-3.3-70b-versatile`) with tool definitions, dispatches MCP tool calls, sends WhatsApp replies.
- **`Server.py`** — FastMCP server. Implements all 13 MCP tools as async functions. Owns the SQLite schema and Google Calendar integration. Runs on port 8001 with `streamable-http` transport.
- **`data/barbershop.db`** — SQLite with WAL mode. Tables: `clientes`, `preferencias_cliente`, `agendamentos`, `produtos`, `pedidos_produto`, `historico_sessao`, `catalogo_cortes`.

The Gateway holds a single persistent MCP session ID (`_mcp_session_id`) reused across requests. Tool calls are wrapped as `{"params": <args>}` when sent via JSON-RPC.

## Running Locally (no Docker)

```bash
# Install dependencies
pip install -r requirements.mcp.txt
pip install -r requirements.gateway.txt

# Terminal 1 — MCP Server (port 8001)
python Server.py

# Terminal 2 — Gateway (port 8000)
uvicorn Gateway:app --reload --port 8000

# Test without WhatsApp
curl -X POST "http://localhost:8000/test-message?telefone=5511999999999&texto=Oi"
```

Check service health: `curl http://localhost:8000/health`

## Running with Docker

```bash
cp .env.example .env   # fill in credentials
docker compose up -d
```

After containers start, create and connect the WhatsApp instance via Evolution API on port 8082.

## Key Environment Variables

| Variable | Purpose |
|---|---|
| `GROQ_API_KEY` | LLM provider (Groq, not Anthropic) |
| `GOOGLE_CALENDAR_ID` | Calendar for availability checks |
| `GOOGLE_CREDENTIALS_PATH` | Path to service account JSON |
| `EVOLUTION_API_KEY` | WhatsApp bridge authentication |
| `EVOLUTION_INSTANCE` | WhatsApp instance name (default: `barbershop`) |
| `MCP_SERVER_URL` | MCP endpoint (default: `http://localhost:8001/mcp`) |

Google credentials go in `credentials/google-calendar.json` (service account JSON). Share the calendar with the service account email with edit permission.

## MCP Tool Conventions

- All tools accept a single Pydantic model parameter named `params` (configured with `extra='forbid'`).
- Tools annotate `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` for MCP capability hints.
- All tools return JSON strings (`json.dumps(..., ensure_ascii=False)`).
- Each tool opens and closes its own SQLite connection in a `try/finally` block.
- `agendar_corte` creates a Google Calendar event first, then mirrors with the event ID as the SQLite primary key.
- `listar_horarios_disponiveis` generates 30-minute slots (09:00–19:00 SP timezone) and filters against Calendar events.

## Service Duration Map

Defined in `Server.py` as `DURACAO_SERVICO`:
- `corte`: 30 min / `barba`: 30 min / `corte+barba`: 60 min / `progressiva`: 60 min

## Gateway Tool Call Flow

`processar_mensagem` runs an agentic loop (max 10 iterations):
1. Fetches last 20 session messages via `buscar_sessao`
2. Saves incoming message via `salvar_mensagem_sessao`
3. Calls Groq with `parallel_tool_calls=False`
4. Dispatches each tool call to MCP and appends results
5. Loops until no more tool calls, then sends final text to WhatsApp

The `LID_MAP` in `Gateway.py` maps WhatsApp LID identifiers to phone numbers — add entries there for contacts that appear as `@lid` JIDs.

## Database

Schema is initialized on startup via `init_db()` which also seeds sample products and a haircut catalog if tables are empty. To reset: delete `data/barbershop.db` or run `python data/clean_db.py`.
