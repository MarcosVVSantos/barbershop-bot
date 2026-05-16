# Barbershop MCP — WhatsApp Assistant Bot

Assistente virtual para WhatsApp com agendamento inteligente, integração com Google Calendar e suporte multi-tenant. Desenvolvido com FastAPI, FastMCP e Gemini como LLM.

## Arquitetura

```
WhatsApp ↔ Evolution API (8082) → FastAPI Gateway (8000) → Gemini 2.5 Flash Lite
                                          ↕ MCP JSON-RPC
                                   MCP Server (8001)
                                          ↕
                              SQLite (data/barbershop.db)
                              Google Calendar API
```

| Componente | Arquivo | Responsabilidade |
|---|---|---|
| Gateway | `Gateway.py` | Recebe webhooks, gerencia histórico, chama Gemini, despacha ferramentas MCP |
| MCP Server | `Server.py` | 13 ferramentas MCP, SQLite, Google Calendar |
| Config | `config.json` | Perfil do negócio (troca de tema sem alterar código) |

## Funcionalidades

- Agendamento e cancelamento via WhatsApp com validação de disponibilidade em tempo real
- Integração bidirecional com Google Calendar (cria e deleta eventos)
- Identificação automática de clientes (novo vs. recorrente) com UUID persistido
- Pré-injeção de contexto: o LLM recebe cliente_id e IDs de agendamentos confirmados antes de responder — nunca inventa IDs
- Resolução automática de JIDs `@lid` via Evolution API com persistência em `data/lid_map.json`
- Sugestão de cortes, venda de produtos e registro de pedidos
- Audit log de cancelamentos no SQLite
- Multi-tenant: troque o perfil do negócio copiando um preset de `configs/`

## Pré-requisitos

- Docker e Docker Compose
- Conta Google Cloud com Calendar API habilitada e uma service account
- API Key do [Google AI Studio](https://aistudio.google.com) (Gemini) — tier pago recomendado
- Evolution API key

## Configuração rápida

```bash
git clone https://github.com/MarcosVVSantos/barbershop-bot.git
cd barbershop-bot

cp .env.example .env
# Edite o .env com suas credenciais

cp configs/barbearia.json config.json   # ou nutricionista.json, adega.json

# Coloque o JSON da service account do Google em:
mkdir -p credentials
cp /caminho/para/sua-service-account.json credentials/google-calendar.json
```

### Variáveis de ambiente (`.env`)

```env
GEMINI_API_KEY=sua_chave_aqui
GOOGLE_CALENDAR_ID=id_do_calendario@group.calendar.google.com
GOOGLE_CREDENTIALS_PATH=/app/credentials/google-calendar.json
EVOLUTION_API_KEY=sua_chave_evolution
EVOLUTION_INSTANCE=barbershop
```

## Rodando com Docker

```bash
docker compose up -d
```

Acesse `http://localhost:8082` para configurar a instância WhatsApp na Evolution API e conectar via QR Code.

## Rodando localmente (sem Docker)

```bash
pip install -r requirements.mcp.txt
pip install -r requirements.gateway.txt

# Terminal 1
python Server.py

# Terminal 2
uvicorn Gateway:app --reload --port 8000

# Teste sem WhatsApp
curl -X POST "http://localhost:8000/test-message?telefone=5511999999999&texto=Oi"
```

Saúde dos serviços:
```bash
curl http://localhost:8000/health
```

## Multi-tenant — Perfis de negócio

Troque o perfil copiando um preset para `config.json` e recriando os containers:

```bash
cp configs/nutricionista.json config.json
docker compose up -d --build
```

Presets disponíveis em `configs/`:

| Arquivo | Negócio |
|---|---|
| `barbearia.json` | Barbearia (padrão) |
| `nutricionista.json` | Consultório de nutrição |
| `adega.json` | Adega / loja de vinhos |

## Banco de dados

SQLite em `data/barbershop.db` com WAL mode. Tabelas principais:

| Tabela | Conteúdo |
|---|---|
| `clientes` | Cadastro de clientes |
| `agendamentos` | Agendamentos com ID do evento do Google Calendar |
| `preferencias_cliente` | Preferências salvas pelo assistente |
| `produtos` / `pedidos_produto` | Catálogo e pedidos |
| `historico_sessao` | Histórico de conversa por telefone |
| `audit_log` | Log imutável de cancelamentos e ações críticas |

Para resetar o banco:
```bash
docker exec barbershop_mcp python data/clean_db.py
# ou
rm data/barbershop.db
```

## Ferramentas MCP disponíveis

`identificar_cliente` · `listar_horarios_disponiveis` · `agendar_corte` · `cancelar_agendamento` · `historico_cliente` · `atualizar_preferencias_cliente` · `sugerir_corte` · `listar_produtos` · `fazer_pedido_produto` · `buscar_sessao` · `salvar_mensagem_sessao`

## Estrutura do projeto

```
.
├── Gateway.py              # FastAPI + loop de agente Gemini
├── Server.py               # FastMCP com 13 ferramentas
├── config.json             # Perfil ativo do negócio
├── configs/                # Presets de negócio
│   ├── barbearia.json
│   ├── nutricionista.json
│   └── adega.json
├── data/
│   ├── barbershop.db       # SQLite (gerado automaticamente)
│   └── lid_map.json        # Mapa LID → telefone (gerado automaticamente)
├── credentials/
│   └── google-calendar.json  # Service account (não commitado)
├── docker/
│   ├── Dockerfile.gateway
│   └── Dockerfile.mcp
├── docker-compose.yml
├── requirements.gateway.txt
└── requirements.mcp.txt
```
