"""
BarberShop MCP Server
Ferramentas para Claude gerenciar agendamentos, clientes, produtos e RAG de cortes.
"""

import os
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP, Context
from pydantic import BaseModel, Field, ConfigDict
from google.oauth2 import service_account
from googleapiclient.discovery import build as gcal_build

# ─── Constantes ───────────────────────────────────────────────────────────────
DB_PATH = Path(__file__).parent.parent / "data" / "barbershop.db"
HORARIO_ABERTURA = 9
HORARIO_FECHAMENTO = 19
DURACAO_CORTE_MIN = 30
SP_TZ = ZoneInfo("America/Sao_Paulo")

CALENDAR_ID       = os.getenv("GOOGLE_CALENDAR_ID", "primary")
CREDENTIALS_PATH  = Path(os.getenv("GOOGLE_CREDENTIALS_PATH", "/app/credentials/google-calendar.json"))

DURACAO_SERVICO = {
    "corte+barba": 60,
    "progressiva": 60,
    "corte": 30,
    "barba": 30,
}

_calendar_svc = None

def get_calendar_service():
    global _calendar_svc
    if _calendar_svc is None:
        creds = service_account.Credentials.from_service_account_file(
            str(CREDENTIALS_PATH),
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
        _calendar_svc = gcal_build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _calendar_svc

# ─── Lifespan ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def app_lifespan(server: FastMCP):
    db = get_db()
    init_db(db)
    db.close()
    yield {}

mcp = FastMCP("barbershop_mcp", lifespan=app_lifespan, host="0.0.0.0", port=8001)

# ─── Database helpers ─────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS clientes (
        id TEXT PRIMARY KEY,
        telefone TEXT UNIQUE NOT NULL,
        nome TEXT NOT NULL,
        criado_em TEXT NOT NULL,
        ultima_visita TEXT,
        total_visitas INTEGER DEFAULT 0,
        notas TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS preferencias_cliente (
        id TEXT PRIMARY KEY,
        cliente_id TEXT NOT NULL,
        tipo TEXT NOT NULL,       -- 'corte', 'produto', 'horario', 'barbeiro'
        valor TEXT NOT NULL,
        atualizado_em TEXT NOT NULL,
        FOREIGN KEY (cliente_id) REFERENCES clientes(id)
    );

    CREATE TABLE IF NOT EXISTS agendamentos (
        id TEXT PRIMARY KEY,
        cliente_id TEXT NOT NULL,
        data_hora TEXT NOT NULL,
        servico TEXT NOT NULL,    -- 'corte', 'barba', 'corte+barba', 'progressiva'
        status TEXT DEFAULT 'pendente',  -- pendente, confirmado, cancelado, concluido
        observacoes TEXT DEFAULT '',
        criado_em TEXT NOT NULL,
        FOREIGN KEY (cliente_id) REFERENCES clientes(id)
    );

    CREATE TABLE IF NOT EXISTS produtos (
        id TEXT PRIMARY KEY,
        nome TEXT NOT NULL,
        descricao TEXT NOT NULL,
        preco REAL NOT NULL,
        categoria TEXT NOT NULL,  -- 'pomada', 'shampoo', 'condicionador', 'balm', 'oleo'
        estoque INTEGER DEFAULT 0,
        ativo INTEGER DEFAULT 1,
        imagem_url TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS pedidos_produto (
        id TEXT PRIMARY KEY,
        cliente_id TEXT NOT NULL,
        agendamento_id TEXT,
        produto_id TEXT NOT NULL,
        quantidade INTEGER DEFAULT 1,
        retirada TEXT DEFAULT 'no_corte',  -- 'no_corte', 'entrega', 'loja'
        status TEXT DEFAULT 'pendente',    -- pendente, confirmado, entregue, cancelado
        criado_em TEXT NOT NULL,
        FOREIGN KEY (cliente_id) REFERENCES clientes(id),
        FOREIGN KEY (produto_id) REFERENCES produtos(id)
    );

    CREATE TABLE IF NOT EXISTS historico_sessao (
        id TEXT PRIMARY KEY,
        telefone TEXT NOT NULL,
        mensagem TEXT NOT NULL,
        papel TEXT NOT NULL,  -- 'user', 'assistant'
        criado_em TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS catalogo_cortes (
        id TEXT PRIMARY KEY,
        nome TEXT NOT NULL,
        descricao TEXT NOT NULL,
        tags TEXT NOT NULL,        -- JSON array: ['degradê', 'moderno', 'cabelo curto']
        tempo_execucao INTEGER,    -- minutos
        preco REAL,
        imagem_url TEXT DEFAULT ''
    );
    """)
    conn.commit()
    _seed_produtos(conn)
    _seed_cortes(conn)

def _seed_produtos(conn: sqlite3.Connection):
    """Insere produtos de exemplo se o catálogo estiver vazio."""
    count = conn.execute("SELECT COUNT(*) FROM produtos").fetchone()[0]
    if count > 0:
        return
    produtos = [
        (str(uuid.uuid4()), "Pomada Matte Black", "Fixação forte, acabamento fosco. Ideal para cabelos médios e crespos.", 45.00, "pomada", 20, 1, ""),
        (str(uuid.uuid4()), "Pomada Brilhosa Clássica", "Fixação média, brilho intenso. Estilo retrô e clássico.", 40.00, "pomada", 15, 1, ""),
        (str(uuid.uuid4()), "Shampoo Antiqueda", "Com biotina e queratina. Fortalece os fios e reduz a queda.", 35.00, "shampoo", 25, 1, ""),
        (str(uuid.uuid4()), "Balm para Barba", "Hidrata, amacia e perfuma a barba. Aroma amadeirado.", 55.00, "balm", 12, 1, ""),
        (str(uuid.uuid4()), "Óleo de Barba Premium", "Óleo de argan + jojoba. Domina o frizz e dá brilho natural.", 65.00, "oleo", 10, 1, ""),
        (str(uuid.uuid4()), "Condicionador Masculino", "Com manteiga de karité. Desembaraça e hidrata sem pesar.", 30.00, "condicionador", 20, 1, ""),
    ]
    conn.executemany(
        "INSERT INTO produtos (id, nome, descricao, preco, categoria, estoque, ativo, imagem_url) VALUES (?,?,?,?,?,?,?,?)",
        produtos
    )
    conn.commit()

def _seed_cortes(conn: sqlite3.Connection):
    """Insere catálogo de cortes para RAG."""
    count = conn.execute("SELECT COUNT(*) FROM catalogo_cortes").fetchone()[0]
    if count > 0:
        return
    cortes = [
        (str(uuid.uuid4()), "Degradê Americano", "Fade suave nas laterais com volume no topo. Moderno e versátil.", '["degradê","moderno","curto","social","esportivo"]', 30, 35.0),
        (str(uuid.uuid4()), "Degradê com Risco", "Fade nas laterais com risco definido. Estilo e precisão.", '["degradê","risco","social","moderno"]', 35, 40.0),
        (str(uuid.uuid4()), "Corte Clássico", "Lateral curta e topo com volume leve. Ideal para ambientes formais.", '["clássico","social","comprido","formal","tradicional"]', 30, 35.0),
        (str(uuid.uuid4()), "Undercut", "Laterais completamente raspadas, topo longo. Estilo underground e moderno.", '["undercut","moderno","alternativo","comprido","estiloso"]', 40, 45.0),
        (str(uuid.uuid4()), "Corte Cacheado", "Valoriza os cachos com forma e leveza. Sem perder o volume natural.", '["cacheado","crespo","volume","natural","leve"]', 30, 35.0),
        (str(uuid.uuid4()), "Militar / Máquina", "Cabelo rente com máquina em número uniforme. Prático e limpo.", '["militar","máquina","curto","prático","clássico"]', 20, 25.0),
        (str(uuid.uuid4()), "Long Top Fade", "Topo longo com fade nas laterais. Muito popular entre jovens.", '["moderno","longo","degradê","jovem","estiloso"]', 40, 50.0),
        (str(uuid.uuid4()), "Corte + Barba", "Combinação de corte degradê com design e acabamento de barba.", '["corte","barba","combo","completo","cuidado"]', 60, 65.0),
        (str(uuid.uuid4()), "Barba Desenhada", "Design preciso no contorno da barba. Linhas retas e definidas.", '["barba","design","contorno","preciso","cuidado"]', 30, 35.0),
        (str(uuid.uuid4()), "Progressiva Masculina", "Alinhamento e redução do volume com escova + produto específico.", '["progressiva","liso","volume","comprido","tratamento"]', 90, 120.0),
    ]
    conn.executemany(
        "INSERT INTO catalogo_cortes (id, nome, descricao, tags, tempo_execucao, preco) VALUES (?,?,?,?,?,?)",
        cortes
    )
    conn.commit()

def row_to_dict(row) -> dict:
    return dict(row) if row else {}

def rows_to_list(rows) -> list:
    return [dict(r) for r in rows]

# ─── Modelos Pydantic ──────────────────────────────────────────────────────────

class IdentificarClienteInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    telefone: str = Field(..., description="Número do WhatsApp no formato internacional, ex: '5511999999999'", min_length=10)
    nome: Optional[str] = Field(None, description="Nome do cliente (informar apenas se for novo cadastro)")

class AgendarCorteInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    cliente_id: str = Field(..., description="ID do cliente retornado por identificar_cliente")
    data: str = Field(..., description="Data desejada no formato 'YYYY-MM-DD', ex: '2025-05-10'")
    hora: str = Field(..., description="Hora desejada no formato 'HH:MM', ex: '14:30'")
    servico: str = Field(..., description="Tipo de serviço: 'corte', 'barba', 'corte+barba', 'progressiva'")
    observacoes: Optional[str] = Field("", description="Observações extras do cliente")

class ListarHorariosInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    data: str = Field(..., description="Data para verificar disponibilidade no formato 'YYYY-MM-DD'")

class AtualizarPreferenciasInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    cliente_id: str = Field(..., description="ID do cliente")
    tipo: str = Field(..., description="Tipo de preferência: 'corte', 'produto', 'horario', 'barbeiro'")
    valor: str = Field(..., description="Valor da preferência, ex: 'degradê americano', 'manhã', 'produtos naturais'")

class SugerirCorteInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    cliente_id: Optional[str] = Field(None, description="ID do cliente para buscar histórico de preferências")
    descricao: str = Field(..., description="Descrição do que o cliente quer ou perfil dele, ex: 'cabelo crespo, gosta de estilo moderno, trabalha em escritório'")

class ListarProdutosInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    categoria: Optional[str] = Field(None, description="Filtrar por categoria: 'pomada', 'shampoo', 'condicionador', 'balm', 'oleo'")
    apenas_disponiveis: bool = Field(True, description="Se True, retorna apenas produtos com estoque > 0")

class FazerPedidoInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    cliente_id: str = Field(..., description="ID do cliente")
    produto_id: str = Field(..., description="ID do produto")
    quantidade: int = Field(1, description="Quantidade desejada", ge=1, le=10)
    retirada: str = Field("no_corte", description="Forma de retirada: 'no_corte' (no dia do agendamento), 'loja' (passar na loja)")
    agendamento_id: Optional[str] = Field(None, description="ID do agendamento vinculado, se retirada for 'no_corte'")

class CancelarAgendamentoInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    agendamento_id: str = Field(..., description="ID do agendamento a cancelar")
    motivo: Optional[str] = Field("", description="Motivo do cancelamento (opcional)")

class HistoricoClienteInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    cliente_id: str = Field(..., description="ID do cliente")
    limite: int = Field(10, description="Número máximo de registros a retornar", ge=1, le=50)

class RelatorioBarbeiroInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    data_inicio: str = Field(..., description="Data de início no formato 'YYYY-MM-DD'")
    data_fim: str = Field(..., description="Data de fim no formato 'YYYY-MM-DD'")

class SalvarSessaoInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    telefone: str = Field(..., description="Telefone do usuário")
    mensagem: str = Field(..., description="Conteúdo da mensagem")
    papel: str = Field(..., description="Quem enviou: 'user' ou 'assistant'")

class BuscarSessaoInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    telefone: str = Field(..., description="Telefone do usuário")
    limite: int = Field(20, description="Últimas N mensagens da sessão", ge=1, le=50)

class GerenciarProdutoInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    produto_id: Optional[str] = Field(None, description="ID do produto (para edição). Omitir para criação.")
    nome: Optional[str] = Field(None, description="Nome do produto")
    descricao: Optional[str] = Field(None, description="Descrição do produto")
    preco: Optional[float] = Field(None, description="Preço em reais", ge=0)
    categoria: Optional[str] = Field(None, description="Categoria: 'pomada', 'shampoo', 'condicionador', 'balm', 'oleo'")
    estoque: Optional[int] = Field(None, description="Quantidade em estoque", ge=0)
    ativo: Optional[bool] = Field(None, description="Se o produto está ativo/disponível")

# ─── Ferramentas MCP ───────────────────────────────────────────────────────────

@mcp.tool(
    name="identificar_cliente",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
)
async def identificar_cliente(params: IdentificarClienteInput) -> str:
    """Identifica ou cadastra um cliente pelo número de WhatsApp.

    Se o cliente já existir, retorna seus dados e preferências.
    Se for novo e o nome foi informado, cria o cadastro automaticamente.
    Se for novo e o nome NÃO foi informado, retorna status='novo' para solicitar o nome.

    Args:
        params: IdentificarClienteInput com telefone e nome opcional.

    Returns:
        JSON com: status ('encontrado'|'novo'|'cadastrado'), cliente (dict com id, nome, telefone,
        total_visitas, ultima_visita, notas), preferencias (list de dicts tipo/valor).
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM clientes WHERE telefone = ?", (params.telefone,)
        ).fetchone()

        if row:
            cliente = row_to_dict(row)
            prefs = rows_to_list(conn.execute(
                "SELECT tipo, valor, atualizado_em FROM preferencias_cliente WHERE cliente_id = ?",
                (cliente["id"],)
            ).fetchall())
            return json.dumps({"status": "encontrado", "cliente": cliente, "preferencias": prefs}, ensure_ascii=False)

        if not params.nome:
            return json.dumps({"status": "novo", "mensagem": "Cliente não encontrado. Por favor, informe o nome para o cadastro."}, ensure_ascii=False)

        novo_id = str(uuid.uuid4())
        agora = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO clientes (id, telefone, nome, criado_em) VALUES (?, ?, ?, ?)",
            (novo_id, params.telefone, params.nome, agora)
        )
        conn.commit()
        cliente = row_to_dict(conn.execute("SELECT * FROM clientes WHERE id = ?", (novo_id,)).fetchone())
        return json.dumps({"status": "cadastrado", "cliente": cliente, "preferencias": []}, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool(
    name="listar_horarios_disponiveis",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def listar_horarios_disponiveis(params: ListarHorariosInput) -> str:
    """Retorna os horários disponíveis para agendamento em uma data específica consultando o Google Calendar.

    Args:
        params: ListarHorariosInput com data no formato YYYY-MM-DD.

    Returns:
        JSON com: data, horarios_disponiveis (list HH:MM), horarios_ocupados (list dicts).
    """
    try:
        year, month, day = int(params.data[:4]), int(params.data[5:7]), int(params.data[8:10])
        day_start = datetime(year, month, day, HORARIO_ABERTURA, 0, 0, tzinfo=SP_TZ)
        day_end   = datetime(year, month, day, HORARIO_FECHAMENTO, 0, 0, tzinfo=SP_TZ)

        svc = get_calendar_service()
        events = svc.events().list(
            calendarId=CALENDAR_ID,
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute().get("items", [])

        # Gera todos os slots de 30 min
        slots, t = [], day_start
        while t < day_end:
            slots.append(t.strftime("%H:%M"))
            t += timedelta(minutes=DURACAO_CORTE_MIN)

        # Marca slots ocupados por overlap com qualquer evento
        ocupados = []
        for ev in events:
            ev_start = datetime.fromisoformat(ev["start"].get("dateTime", ev["start"].get("date"))).astimezone(SP_TZ)
            ev_end   = datetime.fromisoformat(ev["end"].get("dateTime", ev["end"].get("date"))).astimezone(SP_TZ)
            t = day_start
            while t < day_end:
                slot_end = t + timedelta(minutes=DURACAO_CORTE_MIN)
                if t < ev_end and slot_end > ev_start:
                    hora_str = t.strftime("%H:%M")
                    if hora_str not in {o["hora"] for o in ocupados}:
                        ocupados.append({"hora": hora_str, "servico": ev.get("summary", "ocupado")})
                t += timedelta(minutes=DURACAO_CORTE_MIN)

        ocupados_horas = {o["hora"] for o in ocupados}
        disponiveis = [s for s in slots if s not in ocupados_horas]

        return json.dumps({
            "data": params.data,
            "horarios_disponiveis": disponiveis,
            "horarios_ocupados": ocupados,
            "total_disponivel": len(disponiveis),
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"erro": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="agendar_corte",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
)
async def agendar_corte(params: AgendarCorteInput) -> str:
    """Cria um agendamento no Google Calendar e espelha no SQLite.

    Args:
        params: AgendarCorteInput com cliente_id, data (YYYY-MM-DD), hora (HH:MM),
                servico e observacoes opcionais.

    Returns:
        JSON com: sucesso (bool), agendamento (dict com id, data_hora, servico, status, calendar_link).
    """
    conn = get_db()
    try:
        cliente = conn.execute("SELECT nome, telefone FROM clientes WHERE id = ?", (params.cliente_id,)).fetchone()
        if not cliente:
            return json.dumps({"sucesso": False, "erro": "Cliente não encontrado."}, ensure_ascii=False)

        year, month, day = int(params.data[:4]), int(params.data[5:7]), int(params.data[8:10])
        h, m = int(params.hora[:2]), int(params.hora[3:5])
        ev_start = datetime(year, month, day, h, m, 0, tzinfo=SP_TZ)
        duracao  = DURACAO_SERVICO.get(params.servico.lower(), DURACAO_CORTE_MIN)
        ev_end   = ev_start + timedelta(minutes=duracao)

        svc = get_calendar_service()
        event_body = {
            "summary": f"{params.servico.capitalize()} — {cliente['nome']}",
            "description": (
                f"Serviço: {params.servico}\n"
                f"Telefone: {cliente['telefone']}\n"
                f"Obs: {params.observacoes or ''}"
            ),
            "start": {"dateTime": ev_start.isoformat(), "timeZone": "America/Sao_Paulo"},
            "end":   {"dateTime": ev_end.isoformat(),   "timeZone": "America/Sao_Paulo"},
        }
        created = svc.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()
        event_id = created["id"]
        data_hora = f"{params.data} {params.hora}:00"
        agora = datetime.now().isoformat()

        conn.execute(
            "INSERT INTO agendamentos (id, cliente_id, data_hora, servico, status, observacoes, criado_em) VALUES (?,?,?,?,?,?,?)",
            (event_id, params.cliente_id, data_hora, params.servico, "confirmado", params.observacoes or "", agora)
        )
        conn.execute(
            "UPDATE clientes SET ultima_visita = ?, total_visitas = total_visitas + 1 WHERE id = ?",
            (data_hora, params.cliente_id)
        )
        conn.commit()

        return json.dumps({
            "sucesso": True,
            "agendamento": {
                "id": event_id,
                "cliente": cliente["nome"],
                "data_hora": data_hora,
                "servico": params.servico,
                "status": "confirmado",
                "calendar_link": created.get("htmlLink", ""),
            }
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"sucesso": False, "erro": str(e)}, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool(
    name="cancelar_agendamento",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
)
async def cancelar_agendamento(params: CancelarAgendamentoInput) -> str:
    """Cancela um agendamento existente.

    Args:
        params: CancelarAgendamentoInput com agendamento_id e motivo opcional.

    Returns:
        JSON com: sucesso (bool), mensagem (str).
    """
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM agendamentos WHERE id = ?", (params.agendamento_id,)).fetchone()
        if not row:
            return json.dumps({"sucesso": False, "erro": "Agendamento não encontrado."}, ensure_ascii=False)
        if row["status"] == "cancelado":
            return json.dumps({"sucesso": False, "erro": "Agendamento já estava cancelado."}, ensure_ascii=False)

        obs_nova = f"{row['observacoes']} | Cancelado: {params.motivo or 'sem motivo'}".strip(" |")
        conn.execute(
            "UPDATE agendamentos SET status = 'cancelado', observacoes = ? WHERE id = ?",
            (obs_nova, params.agendamento_id)
        )
        conn.commit()
        return json.dumps({"sucesso": True, "mensagem": f"Agendamento de {row['data_hora']} cancelado com sucesso."}, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool(
    name="atualizar_preferencias_cliente",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def atualizar_preferencias_cliente(params: AtualizarPreferenciasInput) -> str:
    """Salva ou atualiza uma preferência do cliente (corte favorito, horário preferido, etc).

    Se já existir uma preferência do mesmo tipo, substitui. Caso contrário, cria nova.

    Args:
        params: AtualizarPreferenciasInput com cliente_id, tipo e valor.

    Returns:
        JSON com: sucesso (bool), preferencia (dict).
    """
    conn = get_db()
    try:
        agora = datetime.now().isoformat()
        existente = conn.execute(
            "SELECT id FROM preferencias_cliente WHERE cliente_id = ? AND tipo = ?",
            (params.cliente_id, params.tipo)
        ).fetchone()

        if existente:
            conn.execute(
                "UPDATE preferencias_cliente SET valor = ?, atualizado_em = ? WHERE id = ?",
                (params.valor, agora, existente["id"])
            )
        else:
            conn.execute(
                "INSERT INTO preferencias_cliente (id, cliente_id, tipo, valor, atualizado_em) VALUES (?,?,?,?,?)",
                (str(uuid.uuid4()), params.cliente_id, params.tipo, params.valor, agora)
            )
        conn.commit()
        return json.dumps({"sucesso": True, "preferencia": {"tipo": params.tipo, "valor": params.valor}}, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool(
    name="sugerir_corte",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def sugerir_corte(params: SugerirCorteInput) -> str:
    """Sugere cortes adequados ao perfil do cliente usando RAG simples sobre o catálogo.

    Busca no catálogo de cortes por palavras-chave extraídas da descrição do cliente
    e das preferências salvas. Retorna as melhores sugestões ordenadas por relevância.

    Args:
        params: SugerirCorteInput com cliente_id opcional e descricao do perfil/desejo.

    Returns:
        JSON com: sugestoes (list de dicts com nome, descricao, preco, tags, relevancia).
    """
    conn = get_db()
    try:
        # Coleta palavras-chave da descrição
        descricao_lower = params.descricao.lower()
        keywords = set(descricao_lower.split())

        # Se tiver cliente_id, adiciona preferências como palavras-chave
        if params.cliente_id:
            prefs = rows_to_list(conn.execute(
                "SELECT tipo, valor FROM preferencias_cliente WHERE cliente_id = ? AND tipo = 'corte'",
                (params.cliente_id,)
            ).fetchall())
            for p in prefs:
                keywords.update(p["valor"].lower().split())

        todos_cortes = rows_to_list(conn.execute("SELECT * FROM catalogo_cortes").fetchall())

        scored = []
        for corte in todos_cortes:
            tags = json.loads(corte["tags"])
            score = 0
            for kw in keywords:
                for tag in tags:
                    if kw in tag.lower() or tag.lower() in kw:
                        score += 2
                if kw in corte["nome"].lower():
                    score += 3
                if kw in corte["descricao"].lower():
                    score += 1

            scored.append({
                "id": corte["id"],
                "nome": corte["nome"],
                "descricao": corte["descricao"],
                "tags": tags,
                "tempo_min": corte["tempo_execucao"],
                "preco": corte["preco"],
                "relevancia": score
            })

        scored.sort(key=lambda x: x["relevancia"], reverse=True)
        top = [s for s in scored if s["relevancia"] > 0][:5] or scored[:3]

        return json.dumps({"sugestoes": top, "total": len(top)}, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool(
    name="listar_produtos",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def listar_produtos(params: ListarProdutosInput) -> str:
    """Lista os produtos disponíveis na barbearia para venda.

    Args:
        params: ListarProdutosInput com categoria opcional e filtro de disponibilidade.

    Returns:
        JSON com: produtos (list de dicts com id, nome, descricao, preco, categoria, estoque),
        total (int).
    """
    conn = get_db()
    try:
        query = "SELECT * FROM produtos WHERE 1=1"
        args = []
        if params.apenas_disponiveis:
            query += " AND estoque > 0 AND ativo = 1"
        if params.categoria:
            query += " AND categoria = ?"
            args.append(params.categoria)
        query += " ORDER BY categoria, nome"

        produtos = rows_to_list(conn.execute(query, args).fetchall())
        return json.dumps({"produtos": produtos, "total": len(produtos)}, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool(
    name="fazer_pedido_produto",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
)
async def fazer_pedido_produto(params: FazerPedidoInput) -> str:
    """Registra um pedido de produto para o cliente, com opção de retirada no dia do corte.

    Args:
        params: FazerPedidoInput com cliente_id, produto_id, quantidade, retirada e agendamento_id opcional.

    Returns:
        JSON com: sucesso (bool), pedido (dict com id, produto, preco_total, retirada, status).
    """
    conn = get_db()
    try:
        produto = row_to_dict(conn.execute("SELECT * FROM produtos WHERE id = ?", (params.produto_id,)).fetchone() or {})
        if not produto:
            return json.dumps({"sucesso": False, "erro": "Produto não encontrado."}, ensure_ascii=False)
        if produto.get("estoque", 0) < params.quantidade:
            return json.dumps({"sucesso": False, "erro": f"Estoque insuficiente. Disponível: {produto['estoque']} unidade(s)."}, ensure_ascii=False)

        novo_id = str(uuid.uuid4())
        agora = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO pedidos_produto (id, cliente_id, agendamento_id, produto_id, quantidade, retirada, status, criado_em) VALUES (?,?,?,?,?,?,?,?)",
            (novo_id, params.cliente_id, params.agendamento_id, params.produto_id, params.quantidade, params.retirada, "confirmado", agora)
        )
        conn.execute(
            "UPDATE produtos SET estoque = estoque - ? WHERE id = ?",
            (params.quantidade, params.produto_id)
        )
        conn.commit()

        return json.dumps({
            "sucesso": True,
            "pedido": {
                "id": novo_id,
                "produto": produto["nome"],
                "quantidade": params.quantidade,
                "preco_unitario": produto["preco"],
                "preco_total": produto["preco"] * params.quantidade,
                "retirada": params.retirada,
                "status": "confirmado"
            }
        }, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool(
    name="historico_cliente",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def historico_cliente(params: HistoricoClienteInput) -> str:
    """Retorna o histórico completo de um cliente: agendamentos, pedidos e preferências.

    Args:
        params: HistoricoClienteInput com cliente_id e limite.

    Returns:
        JSON com: cliente (dict), agendamentos (list), pedidos (list), preferencias (list).
    """
    conn = get_db()
    try:
        cliente = row_to_dict(conn.execute("SELECT * FROM clientes WHERE id = ?", (params.cliente_id,)).fetchone() or {})
        if not cliente:
            return json.dumps({"erro": "Cliente não encontrado."}, ensure_ascii=False)

        agendamentos = rows_to_list(conn.execute(
            "SELECT * FROM agendamentos WHERE cliente_id = ? ORDER BY data_hora DESC LIMIT ?",
            (params.cliente_id, params.limite)
        ).fetchall())

        pedidos = rows_to_list(conn.execute(
            """SELECT p.*, pr.nome as produto_nome, pr.preco
               FROM pedidos_produto p
               JOIN produtos pr ON p.produto_id = pr.id
               WHERE p.cliente_id = ? ORDER BY p.criado_em DESC LIMIT ?""",
            (params.cliente_id, params.limite)
        ).fetchall())

        preferencias = rows_to_list(conn.execute(
            "SELECT tipo, valor, atualizado_em FROM preferencias_cliente WHERE cliente_id = ?",
            (params.cliente_id,)
        ).fetchall())

        return json.dumps({
            "cliente": cliente,
            "agendamentos": agendamentos,
            "pedidos": pedidos,
            "preferencias": preferencias
        }, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool(
    name="salvar_mensagem_sessao",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
)
async def salvar_mensagem_sessao(params: SalvarSessaoInput) -> str:
    """Salva uma mensagem no histórico da sessão do WhatsApp do cliente.

    Usado para manter contexto entre conversas e permitir retomada natural.

    Args:
        params: SalvarSessaoInput com telefone, mensagem e papel ('user' ou 'assistant').

    Returns:
        JSON com: sucesso (bool).
    """
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO historico_sessao (id, telefone, mensagem, papel, criado_em) VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), params.telefone, params.mensagem, params.papel, datetime.now().isoformat())
        )
        conn.commit()
        return json.dumps({"sucesso": True}, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool(
    name="buscar_sessao",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def buscar_sessao(params: BuscarSessaoInput) -> str:
    """Busca o histórico de conversa de um telefone para retomar o contexto.

    Args:
        params: BuscarSessaoInput com telefone e limite de mensagens.

    Returns:
        JSON com: historico (list de dicts com papel, mensagem, criado_em).
    """
    conn = get_db()
    try:
        rows = rows_to_list(conn.execute(
            "SELECT papel, mensagem, criado_em FROM historico_sessao WHERE telefone = ? ORDER BY criado_em DESC LIMIT ?",
            (params.telefone, params.limite)
        ).fetchall())
        rows.reverse()  # Ordem cronológica
        return json.dumps({"historico": rows, "total": len(rows)}, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool(
    name="relatorio_barbeiro",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def relatorio_barbeiro(params: RelatorioBarbeiroInput) -> str:
    """Gera relatório do período para o barbeiro: agendamentos, receita e produtos vendidos.

    Args:
        params: RelatorioBarbeiroInput com data_inicio e data_fim (YYYY-MM-DD).

    Returns:
        JSON com: periodo, total_agendamentos, receita_estimada, agendamentos_por_servico,
        produtos_vendidos, top_clientes.
    """
    conn = get_db()
    try:
        inicio = f"{params.data_inicio} 00:00:00"
        fim = f"{params.data_fim} 23:59:59"

        agendamentos = rows_to_list(conn.execute(
            """SELECT a.*, c.nome as cliente_nome
               FROM agendamentos a JOIN clientes c ON a.cliente_id = c.id
               WHERE a.data_hora BETWEEN ? AND ? AND a.status != 'cancelado'
               ORDER BY a.data_hora""",
            (inicio, fim)
        ).fetchall())

        # Agrupa por serviço
        por_servico = {}
        for ag in agendamentos:
            s = ag["servico"]
            por_servico[s] = por_servico.get(s, 0) + 1

        pedidos_periodo = rows_to_list(conn.execute(
            """SELECT p.quantidade, pr.nome, pr.preco, p.status
               FROM pedidos_produto p JOIN produtos pr ON p.produto_id = pr.id
               WHERE p.criado_em BETWEEN ? AND ? AND p.status != 'cancelado'""",
            (inicio, fim)
        ).fetchall())

        receita_produtos = sum(p["quantidade"] * p["preco"] for p in pedidos_periodo)

        # Preço médio por serviço (estimativa)
        preco_servico = {"corte": 35, "barba": 35, "corte+barba": 65, "progressiva": 120}
        receita_cortes = sum(preco_servico.get(ag["servico"], 40) for ag in agendamentos)

        top_clientes = rows_to_list(conn.execute(
            """SELECT c.nome, COUNT(*) as total
               FROM agendamentos a JOIN clientes c ON a.cliente_id = c.id
               WHERE a.data_hora BETWEEN ? AND ? AND a.status != 'cancelado'
               GROUP BY c.id ORDER BY total DESC LIMIT 5""",
            (inicio, fim)
        ).fetchall())

        return json.dumps({
            "periodo": {"inicio": params.data_inicio, "fim": params.data_fim},
            "total_agendamentos": len(agendamentos),
            "receita_estimada_cortes": receita_cortes,
            "receita_produtos": receita_produtos,
            "receita_total": receita_cortes + receita_produtos,
            "agendamentos_por_servico": por_servico,
            "produtos_vendidos": pedidos_periodo,
            "top_clientes": top_clientes
        }, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool(
    name="gerenciar_produto",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
)
async def gerenciar_produto(params: GerenciarProdutoInput) -> str:
    """Cria ou atualiza um produto no catálogo da barbearia (uso do barbeiro/admin).

    Se produto_id for omitido, cria novo produto. Se informado, atualiza os campos fornecidos.

    Args:
        params: GerenciarProdutoInput com campos do produto.

    Returns:
        JSON com: sucesso (bool), produto (dict).
    """
    conn = get_db()
    try:
        if not params.produto_id:
            # Criação
            campos_obrigatorios = [params.nome, params.descricao, params.preco, params.categoria]
            if any(c is None for c in campos_obrigatorios):
                return json.dumps({"sucesso": False, "erro": "nome, descricao, preco e categoria são obrigatórios para criar produto."}, ensure_ascii=False)
            novo_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO produtos (id, nome, descricao, preco, categoria, estoque, ativo) VALUES (?,?,?,?,?,?,?)",
                (novo_id, params.nome, params.descricao, params.preco, params.categoria, params.estoque or 0, 1)
            )
            conn.commit()
            produto = row_to_dict(conn.execute("SELECT * FROM produtos WHERE id = ?", (novo_id,)).fetchone())
            return json.dumps({"sucesso": True, "acao": "criado", "produto": produto}, ensure_ascii=False)
        else:
            # Atualização parcial
            updates = []
            vals = []
            if params.nome is not None: updates.append("nome = ?"); vals.append(params.nome)
            if params.descricao is not None: updates.append("descricao = ?"); vals.append(params.descricao)
            if params.preco is not None: updates.append("preco = ?"); vals.append(params.preco)
            if params.categoria is not None: updates.append("categoria = ?"); vals.append(params.categoria)
            if params.estoque is not None: updates.append("estoque = ?"); vals.append(params.estoque)
            if params.ativo is not None: updates.append("ativo = ?"); vals.append(1 if params.ativo else 0)
            if not updates:
                return json.dumps({"sucesso": False, "erro": "Nenhum campo para atualizar."}, ensure_ascii=False)

            vals.append(params.produto_id)
            conn.execute(f"UPDATE produtos SET {', '.join(updates)} WHERE id = ?", vals)
            conn.commit()
            produto = row_to_dict(conn.execute("SELECT * FROM produtos WHERE id = ?", (params.produto_id,)).fetchone() or {})
            return json.dumps({"sucesso": True, "acao": "atualizado", "produto": produto}, ensure_ascii=False)
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run(transport="streamable-http")