"""
Testes FASE 1: fluxo de estados do agendamento.

Todos os testes usam SQLite em memória — sem dependências externas.
Testam: schema, migração de dados, transições de estado, auto-concluido e métricas.
"""

import sqlite3
import uuid
import pytest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SP_TZ = ZoneInfo("America/Sao_Paulo")

PRECO_SERVICO = {
    "corte": 35.0,
    "barba": 35.0,
    "corte+barba": 65.0,
    "progressiva": 120.0,
}

SCHEMA_SQL = """
CREATE TABLE clientes (
    id TEXT PRIMARY KEY,
    telefone TEXT UNIQUE NOT NULL,
    nome TEXT NOT NULL,
    criado_em TEXT NOT NULL,
    ultima_visita TEXT,
    total_visitas INTEGER DEFAULT 0,
    notas TEXT DEFAULT '',
    ultima_recuperacao_em TEXT
);

CREATE TABLE agendamentos (
    id TEXT PRIMARY KEY,
    cliente_id TEXT NOT NULL,
    data_hora TEXT NOT NULL,
    servico TEXT NOT NULL,
    status TEXT DEFAULT 'agendado',
    valor REAL,
    confirmado_em TEXT,
    lembrete_enviado_em TEXT,
    observacoes TEXT DEFAULT '',
    criado_em TEXT NOT NULL,
    FOREIGN KEY (cliente_id) REFERENCES clientes(id)
);

CREATE TABLE audit_log (
    id TEXT PRIMARY KEY,
    entidade TEXT NOT NULL,
    entidade_id TEXT NOT NULL,
    acao TEXT NOT NULL,
    detalhes TEXT NOT NULL,
    criado_em TEXT NOT NULL
);
"""


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _cliente(conn, nome="João") -> str:
    cid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO clientes (id, telefone, nome, criado_em) VALUES (?,?,?,?)",
        (cid, f"551199{uuid.uuid4().hex[:8]}", nome, datetime.now().isoformat()),
    )
    conn.commit()
    return cid


def _agendar(conn, cliente_id, data_hora, servico="corte", status="agendado") -> str:
    aid = str(uuid.uuid4())
    valor = PRECO_SERVICO.get(servico, 0.0)
    conn.execute(
        "INSERT INTO agendamentos (id, cliente_id, data_hora, servico, status, valor, criado_em)"
        " VALUES (?,?,?,?,?,?,?)",
        (aid, cliente_id, data_hora, servico, status, valor, datetime.now().isoformat()),
    )
    conn.commit()
    return aid


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ─── Schema ──────────────────────────────────────────────────────────────────

class TestSchema:
    def test_colunas_novas_existem(self):
        conn = _db()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agendamentos)")}
        assert "valor" in cols
        assert "confirmado_em" in cols
        assert "lembrete_enviado_em" in cols

    def test_status_padrao_agendado(self):
        conn = _db()
        cid = _cliente(conn)
        aid = _agendar(conn, cid, _fmt(datetime.now() + timedelta(hours=2)))
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "agendado"

    def test_valor_populado_no_insert(self):
        conn = _db()
        cid = _cliente(conn)
        aid = _agendar(conn, cid, _fmt(datetime.now() + timedelta(hours=2)), servico="corte+barba")
        row = conn.execute("SELECT valor FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["valor"] == 65.0


# ─── Migração de dados ────────────────────────────────────────────────────────

def _run_migration(conn: sqlite3.Connection):
    """Replica a lógica de migração do init_db do Server.py."""
    agora = datetime.now(SP_TZ).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE agendamentos SET status = 'concluido'"
        " WHERE data_hora < ? AND status NOT IN ('cancelado', 'concluido', 'no_show')",
        (agora,),
    )
    conn.execute(
        "UPDATE agendamentos SET status = 'agendado'"
        " WHERE data_hora >= ? AND status IN ('pendente', 'confirmado')",
        (agora,),
    )
    for servico, preco in PRECO_SERVICO.items():
        conn.execute(
            "UPDATE agendamentos SET valor = ? WHERE valor IS NULL AND LOWER(servico) = ?",
            (preco, servico),
        )
    conn.commit()


class TestMigracaoDados:
    def test_passado_vira_concluido(self):
        conn = _db()
        cid = _cliente(conn)
        passado = _fmt(datetime.now(SP_TZ) - timedelta(hours=2))
        aid = _agendar(conn, cid, passado, status="agendado")
        _run_migration(conn)
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "concluido"

    def test_passado_pendente_vira_concluido(self):
        conn = _db()
        cid = _cliente(conn)
        passado = _fmt(datetime.now(SP_TZ) - timedelta(days=5))
        aid = _agendar(conn, cid, passado, status="pendente")
        _run_migration(conn)
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "concluido"

    def test_passado_confirmado_vira_concluido(self):
        conn = _db()
        cid = _cliente(conn)
        passado = _fmt(datetime.now(SP_TZ) - timedelta(days=1))
        aid = _agendar(conn, cid, passado, status="confirmado")
        _run_migration(conn)
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "concluido"

    def test_cancelado_nao_muda(self):
        conn = _db()
        cid = _cliente(conn)
        passado = _fmt(datetime.now(SP_TZ) - timedelta(days=1))
        aid = _agendar(conn, cid, passado, status="cancelado")
        _run_migration(conn)
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "cancelado"

    def test_no_show_nao_muda(self):
        conn = _db()
        cid = _cliente(conn)
        passado = _fmt(datetime.now(SP_TZ) - timedelta(hours=3))
        aid = _agendar(conn, cid, passado, status="no_show")
        _run_migration(conn)
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "no_show"

    def test_futuro_confirmado_vira_agendado(self):
        conn = _db()
        cid = _cliente(conn)
        futuro = _fmt(datetime.now(SP_TZ) + timedelta(days=2))
        aid = _agendar(conn, cid, futuro, status="confirmado")
        _run_migration(conn)
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "agendado"

    def test_valor_populado_na_migracao(self):
        conn = _db()
        cid = _cliente(conn)
        # Insere sem valor (simula registro antigo)
        aid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO agendamentos (id, cliente_id, data_hora, servico, status, criado_em)"
            " VALUES (?,?,?,?,?,?)",
            (aid, cid, _fmt(datetime.now(SP_TZ) - timedelta(days=1)), "barba", "agendado", datetime.now().isoformat()),
        )
        conn.commit()
        _run_migration(conn)
        row = conn.execute("SELECT valor FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["valor"] == 35.0

    def test_migracao_idempotente(self):
        """Rodar a migração duas vezes não deve mudar nada na segunda rodada."""
        conn = _db()
        cid = _cliente(conn)
        passado = _fmt(datetime.now(SP_TZ) - timedelta(hours=1))
        futuro = _fmt(datetime.now(SP_TZ) + timedelta(days=1))
        aid_p = _agendar(conn, cid, passado, status="agendado")
        aid_f = _agendar(conn, cid, futuro, status="confirmado")
        _run_migration(conn)
        _run_migration(conn)  # segunda rodada — deve ser no-op
        assert conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid_p,)).fetchone()["status"] == "concluido"
        assert conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid_f,)).fetchone()["status"] == "agendado"


# ─── Transições de estado ─────────────────────────────────────────────────────

def _marcar_no_show(conn: sqlite3.Connection, agendamento_id: str):
    conn.execute("UPDATE agendamentos SET status = 'no_show' WHERE id = ?", (agendamento_id,))
    conn.commit()


def _confirmar(conn: sqlite3.Connection, agendamento_id: str):
    agora = datetime.now().isoformat()
    conn.execute(
        "UPDATE agendamentos SET status = 'confirmado', confirmado_em = ? WHERE id = ?",
        (agora, agendamento_id),
    )
    conn.commit()


def _cancelar(conn: sqlite3.Connection, agendamento_id: str):
    conn.execute("UPDATE agendamentos SET status = 'cancelado' WHERE id = ?", (agendamento_id,))
    conn.commit()


class TestTransicaoEstados:
    def test_agendado_para_confirmado(self):
        conn = _db()
        cid = _cliente(conn)
        futuro = _fmt(datetime.now(SP_TZ) + timedelta(days=1))
        aid = _agendar(conn, cid, futuro)
        _confirmar(conn, aid)
        row = conn.execute("SELECT status, confirmado_em FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "confirmado"
        assert row["confirmado_em"] is not None

    def test_agendado_para_cancelado(self):
        conn = _db()
        cid = _cliente(conn)
        futuro = _fmt(datetime.now(SP_TZ) + timedelta(days=1))
        aid = _agendar(conn, cid, futuro)
        _cancelar(conn, aid)
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "cancelado"

    def test_agendado_para_no_show(self):
        conn = _db()
        cid = _cliente(conn)
        passado = _fmt(datetime.now(SP_TZ) - timedelta(hours=1))
        aid = _agendar(conn, cid, passado)
        _marcar_no_show(conn, aid)
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "no_show"

    def test_confirmado_para_no_show(self):
        conn = _db()
        cid = _cliente(conn)
        passado = _fmt(datetime.now(SP_TZ) - timedelta(hours=1))
        aid = _agendar(conn, cid, passado, status="confirmado")
        _marcar_no_show(conn, aid)
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "no_show"


# ─── Auto-concluido ───────────────────────────────────────────────────────────

def _run_auto_concluido(conn: sqlite3.Connection):
    """Replica a lógica do auto_concluido_job do Gateway.py."""
    agora = datetime.now(SP_TZ).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE agendamentos SET status = 'concluido'"
        " WHERE data_hora < ? AND status = 'agendado'",
        (agora,),
    )
    conn.commit()


class TestAutoConcluido:
    def test_passado_agendado_vira_concluido(self):
        conn = _db()
        cid = _cliente(conn)
        passado = _fmt(datetime.now(SP_TZ) - timedelta(minutes=30))
        aid = _agendar(conn, cid, passado)
        _run_auto_concluido(conn)
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "concluido"

    def test_futuro_agendado_nao_muda(self):
        conn = _db()
        cid = _cliente(conn)
        futuro = _fmt(datetime.now(SP_TZ) + timedelta(hours=2))
        aid = _agendar(conn, cid, futuro)
        _run_auto_concluido(conn)
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "agendado"

    def test_no_show_nao_vira_concluido(self):
        conn = _db()
        cid = _cliente(conn)
        passado = _fmt(datetime.now(SP_TZ) - timedelta(hours=1))
        aid = _agendar(conn, cid, passado, status="no_show")
        _run_auto_concluido(conn)
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "no_show"

    def test_idempotente(self):
        conn = _db()
        cid = _cliente(conn)
        passado = _fmt(datetime.now(SP_TZ) - timedelta(hours=1))
        aid = _agendar(conn, cid, passado)
        _run_auto_concluido(conn)
        _run_auto_concluido(conn)
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "concluido"

    def test_silencio_barbeiro_nunca_vira_no_show(self):
        """Garante que o job auto_concluido nunca cria no_show — só concluido."""
        conn = _db()
        cid = _cliente(conn)
        passado = _fmt(datetime.now(SP_TZ) - timedelta(hours=2))
        aid = _agendar(conn, cid, passado)
        _run_auto_concluido(conn)
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] != "no_show"
        assert row["status"] == "concluido"


# ─── Métricas FASE 4 (preparação) ────────────────────────────────────────────

def _faturamento(conn: sqlite3.Connection, ano: int, mes: int) -> float:
    inicio = f"{ano:04d}-{mes:02d}-01"
    import calendar
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    fim = f"{ano:04d}-{mes:02d}-{ultimo_dia:02d} 23:59:59"
    row = conn.execute(
        "SELECT COALESCE(SUM(valor), 0) FROM agendamentos"
        " WHERE status = 'concluido' AND data_hora BETWEEN ? AND ?",
        (inicio, fim),
    ).fetchone()
    return float(row[0])


def _perda_no_show(conn: sqlite3.Connection, ano: int, mes: int) -> float:
    inicio = f"{ano:04d}-{mes:02d}-01"
    import calendar
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    fim = f"{ano:04d}-{mes:02d}-{ultimo_dia:02d} 23:59:59"
    row = conn.execute(
        "SELECT COALESCE(SUM(valor), 0) FROM agendamentos"
        " WHERE status = 'no_show' AND data_hora BETWEEN ? AND ?",
        (inicio, fim),
    ).fetchone()
    return float(row[0])


class TestMetricasFase4:
    def test_faturamento_so_conta_concluido(self):
        conn = _db()
        cid = _cliente(conn)
        mes_passado = "2026-06-15 10:00:00"
        _agendar(conn, cid, mes_passado, servico="corte", status="concluido")
        _agendar(conn, cid, mes_passado, servico="barba", status="no_show")
        _agendar(conn, cid, mes_passado, servico="corte+barba", status="cancelado")
        resultado = _faturamento(conn, 2026, 6)
        assert resultado == 35.0  # só o corte concluido

    def test_perda_no_show_soma_valor(self):
        conn = _db()
        cid = _cliente(conn)
        mes_passado = "2026-06-20 14:00:00"
        _agendar(conn, cid, mes_passado, servico="progressiva", status="no_show")
        _agendar(conn, cid, mes_passado, servico="corte", status="no_show")
        resultado = _perda_no_show(conn, 2026, 6)
        assert resultado == 120.0 + 35.0  # progressiva + corte

    def test_faturamento_mes_sem_concluido(self):
        conn = _db()
        resultado = _faturamento(conn, 2026, 6)
        assert resultado == 0.0

    def test_faturamento_nao_vaza_entre_meses(self):
        conn = _db()
        cid = _cliente(conn)
        _agendar(conn, cid, "2026-05-31 23:59:00", servico="corte", status="concluido")
        _agendar(conn, cid, "2026-07-01 00:01:00", servico="corte", status="concluido")
        resultado = _faturamento(conn, 2026, 6)
        assert resultado == 0.0
