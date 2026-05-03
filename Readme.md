# 💈 Barbershop AI — WhatsApp + Claude + MCP

Sistema completo de atendimento inteligente para barbearia via WhatsApp, usando Claude como IA conversacional com MCP (Model Context Protocol) para gerenciar agendamentos, clientes, produtos e sugestões personalizadas de corte.

---

## 🏗️ Arquitetura

```
Cliente WhatsApp
      │
      ▼
Evolution API  ←── webhook ──►  FastAPI Gateway  ──►  Claude API (claude-sonnet)
(bridge WA)                          │                      │
                                     │                      │ MCP tools
                                     ▼                      ▼
                               SQLite Database  ◄──  MCP Server (FastMCP)
                               ├── clientes
                               ├── agendamentos
                               ├── produtos
                               ├── pedidos
                               ├── preferências
                               ├── sessões
                               └── catálogo de cortes (RAG)
```

### Componentes

| Serviço | Porta | Função |
|---|---|---|
| **Evolution API** | 8080 | Bridge WhatsApp ↔ REST API |
| **FastAPI Gateway** | 8000 | Orquestra Claude + MCP, recebe webhooks |
| **MCP Server** | 8001 | Ferramentas para Claude (banco de dados) |

---

## ✨ Funcionalidades

### Para o Cliente (via WhatsApp)
- 🗓️ **Agendamento** — marcar, consultar e cancelar horários
- ✂️ **Sugestão de cortes** — personalizada pelo histórico e preferências
- 🧴 **Catálogo de produtos** — ver, pedir e retirar no dia do corte
- 👤 **Perfil inteligente** — o bot lembra preferências entre sessões
- 💬 **Conversa natural** — linguagem casual, como falar com o barbeiro

### Para o Barbeiro (admin)
- 📊 **Relatório de período** — agendamentos, receita e produtos vendidos
- 🧴 **Gestão de produtos** — cadastrar, editar estoque e preços
- 📅 **Visão de agenda** — horários livres e ocupados por data
- 👥 **Top clientes** — quem mais frequenta a barbearia

---

## 🚀 Como Subir (Docker)

### 1. Clone e configure
```bash
git clone <repo>
cd barbershop-mcp
cp .env.example .env
# Edite .env com suas credenciais
```

### 2. Suba os containers
```bash
docker compose up -d
```

### 3. Configure a instância do WhatsApp
```bash
# Crie a instância
curl -X POST http://localhost:8080/instance/create \
  -H "apikey: $EVOLUTION_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"instanceName": "barbershop", "qrcode": true}'

# Escaneie o QR Code
curl http://localhost:8080/instance/connect/barbershop \
  -H "apikey: $EVOLUTION_API_KEY"
# Abra a URL retornada e escaneie com o WhatsApp
```

### 4. Configure o webhook
```bash
curl -X POST http://localhost:8080/webhook/set/barbershop \
  -H "apikey: $EVOLUTION_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://SEU_IP_OU_DOMINIO:8000/webhook",
    "webhook_by_events": false,
    "webhook_base64": false,
    "events": ["MESSAGES_UPSERT"]
  }'
```

> **Nota:** Em desenvolvimento local, use [ngrok](https://ngrok.com) para expor a porta 8000:
> ```bash
> ngrok http 8000
> # Use a URL gerada no webhook acima
> ```

---

## 🔧 Desenvolvimento Local (sem Docker)

```bash
# Instale dependências
pip install -r requirements.mcp.txt
pip install -r requirements.gateway.txt

# Terminal 1 — MCP Server
cd mcp_server
python server.py

# Terminal 2 — Gateway
cd gateway
uvicorn gateway:app --reload --port 8000

# Teste sem WhatsApp
curl -X POST "http://localhost:8000/test-message?telefone=5511999999999&texto=Oi, quero agendar um corte"
```

---

## 🛠️ Ferramentas MCP Disponíveis

| Ferramenta | Descrição |
|---|---|
| `identificar_cliente` | Cadastra ou recupera cliente pelo telefone |
| `listar_horarios_disponiveis` | Slots livres em uma data |
| `agendar_corte` | Cria agendamento confirmado |
| `cancelar_agendamento` | Cancela com motivo |
| `atualizar_preferencias_cliente` | Salva preferências (corte, horário, etc) |
| `sugerir_corte` | RAG: recomenda cortes pelo perfil e histórico |
| `listar_produtos` | Catálogo de produtos com filtro |
| `fazer_pedido_produto` | Pedido com retirada no corte ou na loja |
| `historico_cliente` | Agendamentos, pedidos e preferências |
| `salvar_mensagem_sessao` | Persiste mensagens para contexto |
| `buscar_sessao` | Recupera histórico de conversa |
| `relatorio_barbeiro` | Receita, agendamentos e top clientes |
| `gerenciar_produto` | CRUD de produtos (admin) |

---

## 📱 Exemplos de Conversa

```
Cliente: Oi, quero marcar um corte
Bot: Olá! 💈 Seja bem-vindo! Não te conheço ainda — qual é o seu nome?

Cliente: João
Bot: Oi João! ✂️ Que dia você quer vir?

Cliente: Quinta-feira
Bot: Na quinta (08/05), tenho esses horários disponíveis:
     09:00 • 09:30 • 10:00 • 14:00 • 14:30 • 15:00
     Qual prefere?

Cliente: 14:30
Bot: Perfeito! ✅ Agendado:
     📅 Quinta, 08/05 às 14:30
     ✂️ Serviço: Corte
     
     Aproveitando — você quer algum produto pra levar no dia?
     Temos pomadas, óleos de barba e muito mais 🧴

Cliente: Que pomadas vocês têm?
Bot: Temos duas opções ótimas:
     • *Pomada Matte Black* — R$ 45 | Fixação forte, acabamento fosco
     • *Pomada Brilhosa Clássica* — R$ 40 | Fixação média, brilho intenso
     
     Quer reservar uma pra retirar no dia do seu corte? 😊
```

---

## 📂 Estrutura do Projeto

```
barbershop-mcp/
├── mcp_server/
│   └── server.py          # MCP Server com todas as ferramentas
├── gateway/
│   └── gateway.py         # FastAPI webhook + orquestração Claude
├── docker/
│   ├── Dockerfile.mcp
│   └── Dockerfile.gateway
├── data/                  # SQLite (criado automaticamente)
├── docker-compose.yml
├── requirements.mcp.txt
├── requirements.gateway.txt
├── .env.example
└── README.md
```

---

## 🔐 Variáveis de Ambiente

| Variável | Descrição |
|---|---|
| `ANTHROPIC_API_KEY` | Chave da API Anthropic |
| `EVOLUTION_API_KEY` | Chave da Evolution API |
| `EVOLUTION_INSTANCE` | Nome da instância WhatsApp |
| `EVOLUTION_API_URL` | URL da Evolution API |
| `MCP_SERVER_URL` | URL do MCP Server |

---

## 🗺️ Roadmap / Próximos Passos

- [ ] **Notificações automáticas** — lembrete 1h antes do agendamento
- [ ] **Fila de espera** — avisa quando abrir horário cancelado
- [ ] **Pagamento via Pix** — integração com gateway de pagamento
- [ ] **Fotos de cortes** — enviar imagens de referência
- [ ] **Multi-barbeiro** — agenda separada por profissional
- [ ] **Programa de fidelidade** — pontos por visita, desconto na 10ª
- [ ] **Dashboard web** — painel admin para o barbeiro
- [ ] **PostgreSQL** — migração do SQLite para produção escalável
- [ ] **Redis** — cache de sessões para alta concorrência

---

## 📄 Licença

MIT — Use à vontade para o seu negócio! ✂️