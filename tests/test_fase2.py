"""
Testes FASE 2: job de confirmação prévia e resposta do cliente.

Testam: seleção de agendamentos para lembrete, idempotência, resposta afirmativa/negativa,
notificação ao barbeiro e casos de borda.
"""

import sqlite3
import uuid
import pytest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SP_TZ = ZoneInfo("America/Sao_Paulo")

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


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _cliente(conn, nome="João", tel=None) -> str:
    cid = str(uuid.uuid4())
    tel = tel or f"551199{uuid.uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO clientes (id, telefone, nome, criado_em) VALUES (?,?,?,?)",
        (cid, tel, nome, datetime.now().isoformat()),
    )
    conn.commit()
    return cid


def _agendar(conn, cliente_id, data_hora, servico="corte", status="agendado",
             criado_em=None, lembrete_enviado_em=None) -> str:
    aid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO agendamentos (id, cliente_id, data_hora, servico, status, valor, lembrete_enviado_em, criado_em)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (aid, cliente_id, data_hora, servico, status, 35.0,
         lembrete_enviado_em, criado_em or datetime.now().isoformat()),
    )
    conn.commit()
    return aid


# ─── Lógica do job de lembretes ───────────────────────────────────────────────

def _candidatos_lembrete(conn, antecedencia_h=3) -> list:
    """Replica a query do enviar_lembretes_job."""
    agora = datetime.now(SP_TZ)
    agora_str = agora.strftime("%Y-%m-%d %H:%M:%S")
    limite_str = (agora + timedelta(hours=antecedencia_h)).strftime("%Y-%m-%d %H:%M:%S")

    rows = conn.execute(
        """
        SELECT a.id, a.data_hora, a.servico, a.criado_em, c.telefone, c.nome
        FROM agendamentos a
        JOIN clientes c ON a.cliente_id = c.id
        WHERE a.status = 'agendado'
          AND a.lembrete_enviado_em IS NULL
          AND a.data_hora > ?
          AND a.data_hora <= ?
        """,
        (agora_str, limite_str),
    ).fetchall()

    # Filtra reservas feitas com menos de antecedencia_h horas de antecedência
    result = []
    for row in rows:
        try:
            criado = datetime.fromisoformat(row["criado_em"]).replace(tzinfo=None)
            dh = datetime.strptime(row["data_hora"][:16], "%Y-%m-%d %H:%M")
            if (dh - criado) >= timedelta(hours=antecedencia_h):
                result.append(dict(row))
        except Exception:
            result.append(dict(row))
    return result


def _marcar_lembrete_enviado(conn, agendamento_id):
    conn.execute(
        "UPDATE agendamentos SET lembrete_enviado_em = ? WHERE id = ?",
        (datetime.now(SP_TZ).isoformat(), agendamento_id),
    )
    conn.commit()


# ─── Lógica de resposta à confirmação ────────────────────────────────────────

def _responder_confirmacao(conn, agendamento_id, confirmou: bool) -> dict:
    """Replica a lógica do tool responder_confirmacao (sem Calendar)."""
    row = conn.execute("SELECT * FROM agendamentos WHERE id = ?", (agendamento_id,)).fetchone()
    if not row:
        return {"sucesso": False, "erro": "não encontrado"}

    agora = datetime.now(SP_TZ).isoformat()
    if confirmou:
        conn.execute(
            "UPDATE agendamentos SET status = 'confirmado', confirmado_em = ? WHERE id = ?",
            (agora, agendamento_id),
        )
        novo_status = "confirmado"
        notificar_barbeiro = False
    else:
        conn.execute("UPDATE agendamentos SET status = 'cancelado' WHERE id = ?", (agendamento_id,))
        novo_status = "cancelado"
        notificar_barbeiro = True
    conn.commit()

    return {
        "sucesso": True,
        "status": novo_status,
        "data_hora": row["data_hora"],
        "notificar_barbeiro": notificar_barbeiro,
    }


# ─── TestJobLembretes ─────────────────────────────────────────────────────────

class TestJobLembretes:
    def test_encontra_agendamento_na_janela(self):
        conn = _db()
        cid = _cliente(conn)
        # Criado há 5h, horário daqui a 2h (dentro da janela de 3h)
        agora = datetime.now(SP_TZ)
        criado_ha = (agora - timedelta(hours=5)).isoformat()
        dh = _fmt(agora + timedelta(hours=2))
        aid = _agendar(conn, cid, dh, criado_em=criado_ha)
        candidatos = _candidatos_lembrete(conn, antecedencia_h=3)
        ids = [c["id"] for c in candidatos]
        assert aid in ids

    def test_ignora_fora_da_janela(self):
        conn = _db()
        cid = _cliente(conn)
        # Horário daqui a 5h — fora da janela de 3h
        agora = datetime.now(SP_TZ)
        criado_ha = (agora - timedelta(hours=10)).isoformat()
        dh = _fmt(agora + timedelta(hours=5))
        aid = _agendar(conn, cid, dh, criado_em=criado_ha)
        candidatos = _candidatos_lembrete(conn, antecedencia_h=3)
        assert aid not in [c["id"] for c in candidatos]

    def test_ignora_lembrete_ja_enviado(self):
        conn = _db()
        cid = _cliente(conn)
        agora = datetime.now(SP_TZ)
        criado_ha = (agora - timedelta(hours=5)).isoformat()
        dh = _fmt(agora + timedelta(hours=2))
        aid = _agendar(conn, cid, dh, criado_em=criado_ha, lembrete_enviado_em=agora.isoformat())
        candidatos = _candidatos_lembrete(conn)
        assert aid not in [c["id"] for c in candidatos]

    def test_ignora_status_diferente_de_agendado(self):
        conn = _db()
        cid = _cliente(conn)
        agora = datetime.now(SP_TZ)
        criado_ha = (agora - timedelta(hours=5)).isoformat()
        dh = _fmt(agora + timedelta(hours=2))
        for status in ("confirmado", "cancelado", "concluido", "no_show"):
            aid = _agendar(conn, cid, dh, status=status, criado_em=criado_ha)
            candidatos = _candidatos_lembrete(conn)
            assert aid not in [c["id"] for c in candidatos]

    def test_ignora_reserva_recente(self):
        """Não envia lembrete se o agendamento foi feito com menos de N horas de antecedência."""
        conn = _db()
        cid = _cliente(conn)
        agora = datetime.now(SP_TZ)
        # Criado 1h atrás, horário daqui a 2h → antecedência total = 3h, mas criado há só 1h
        criado_ha = (agora - timedelta(hours=1)).isoformat()
        dh = _fmt(agora + timedelta(hours=2))
        aid = _agendar(conn, cid, dh, criado_em=criado_ha)
        candidatos = _candidatos_lembrete(conn, antecedencia_h=3)
        assert aid not in [c["id"] for c in candidatos]

    def test_ignora_passado(self):
        """Não envia lembrete para agendamentos que já passaram."""
        conn = _db()
        cid = _cliente(conn)
        agora = datetime.now(SP_TZ)
        criado_ha = (agora - timedelta(hours=5)).isoformat()
        dh = _fmt(agora - timedelta(hours=1))  # já passou
        aid = _agendar(conn, cid, dh, criado_em=criado_ha)
        candidatos = _candidatos_lembrete(conn)
        assert aid not in [c["id"] for c in candidatos]

    def test_idempotente(self):
        """Marcar lembrete_enviado_em impede envio duplo."""
        conn = _db()
        cid = _cliente(conn)
        agora = datetime.now(SP_TZ)
        criado_ha = (agora - timedelta(hours=5)).isoformat()
        dh = _fmt(agora + timedelta(hours=2))
        aid = _agendar(conn, cid, dh, criado_em=criado_ha)
        # Primeira rodada — encontra
        assert aid in [c["id"] for c in _candidatos_lembrete(conn)]
        # Simula envio
        _marcar_lembrete_enviado(conn, aid)
        # Segunda rodada — não encontra mais
        assert aid not in [c["id"] for c in _candidatos_lembrete(conn)]

    def test_multiplos_clientes_todos_recebem(self):
        conn = _db()
        agora = datetime.now(SP_TZ)
        criado_ha = (agora - timedelta(hours=5)).isoformat()
        dh = _fmt(agora + timedelta(hours=2))
        ids = []
        for i in range(3):
            cid = _cliente(conn, nome=f"Cliente{i}")
            ids.append(_agendar(conn, cid, dh, criado_em=criado_ha))
        candidatos = _candidatos_lembrete(conn)
        for aid in ids:
            assert aid in [c["id"] for c in candidatos]


# ─── TestResponderConfirmacao ─────────────────────────────────────────────────

class TestResponderConfirmacao:
    def test_confirmou_true_muda_status(self):
        conn = _db()
        cid = _cliente(conn)
        agora = datetime.now(SP_TZ)
        dh = _fmt(agora + timedelta(hours=1))
        aid = _agendar(conn, cid, dh, lembrete_enviado_em=agora.isoformat())
        resultado = _responder_confirmacao(conn, aid, confirmou=True)
        assert resultado["sucesso"] is True
        assert resultado["status"] == "confirmado"
        assert resultado["notificar_barbeiro"] is False
        row = conn.execute("SELECT status, confirmado_em FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "confirmado"
        assert row["confirmado_em"] is not None

    def test_confirmou_false_cancela(self):
        conn = _db()
        cid = _cliente(conn)
        agora = datetime.now(SP_TZ)
        dh = _fmt(agora + timedelta(hours=1))
        aid = _agendar(conn, cid, dh, lembrete_enviado_em=agora.isoformat())
        resultado = _responder_confirmacao(conn, aid, confirmou=False)
        assert resultado["sucesso"] is True
        assert resultado["status"] == "cancelado"
        assert resultado["notificar_barbeiro"] is True
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "cancelado"

    def test_notificar_barbeiro_so_quando_cancela(self):
        conn = _db()
        cid = _cliente(conn)
        agora = datetime.now(SP_TZ)
        dh = _fmt(agora + timedelta(hours=1))
        aid_sim = _agendar(conn, cid, dh, lembrete_enviado_em=agora.isoformat())
        aid_nao = _agendar(conn, cid, _fmt(agora + timedelta(hours=2)), lembrete_enviado_em=agora.isoformat())
        assert _responder_confirmacao(conn, aid_sim, confirmou=True)["notificar_barbeiro"] is False
        assert _responder_confirmacao(conn, aid_nao, confirmou=False)["notificar_barbeiro"] is True

    def test_agendamento_inexistente(self):
        conn = _db()
        resultado = _responder_confirmacao(conn, "id-que-nao-existe", confirmou=True)
        assert resultado["sucesso"] is False

    def test_idempotente_confirmacao(self):
        """Confirmar duas vezes mantém status confirmado."""
        conn = _db()
        cid = _cliente(conn)
        dh = _fmt(datetime.now(SP_TZ) + timedelta(hours=1))
        aid = _agendar(conn, cid, dh, lembrete_enviado_em=datetime.now(SP_TZ).isoformat())
        _responder_confirmacao(conn, aid, confirmou=True)
        _responder_confirmacao(conn, aid, confirmou=True)
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "confirmado"

    def test_sem_resposta_mantem_agendado(self):
        """Sem resposta não deve alterar status — o job não faz nada com quem não respondeu."""
        conn = _db()
        cid = _cliente(conn)
        dh = _fmt(datetime.now(SP_TZ) + timedelta(hours=1))
        aid = _agendar(conn, cid, dh, lembrete_enviado_em=datetime.now(SP_TZ).isoformat())
        # Não chama _responder_confirmacao — simula silêncio do cliente
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "agendado"  # manteve agendado, não penaliza


# ─── TestContextoAguardandoConfirmacao ───────────────────────────────────────

class TestContextoAguardandoConfirmacao:
    def _detecta_aguardando(self, agendamentos: list) -> list:
        """Replica a lógica de detecção no Gateway.py pre-fetch."""
        return [a for a in agendamentos if a.get("status") == "agendado" and a.get("lembrete_enviado_em")]

    def test_detecta_com_lembrete_enviado(self):
        ags = [{"id": "x", "status": "agendado", "lembrete_enviado_em": "2026-07-13T10:00:00"}]
        assert len(self._detecta_aguardando(ags)) == 1

    def test_nao_detecta_sem_lembrete(self):
        ags = [{"id": "x", "status": "agendado", "lembrete_enviado_em": None}]
        assert len(self._detecta_aguardando(ags)) == 0

    def test_nao_detecta_status_diferente(self):
        ags = [{"id": "x", "status": "confirmado", "lembrete_enviado_em": "2026-07-13T10:00:00"}]
        assert len(self._detecta_aguardando(ags)) == 0

    def test_multiple_pega_o_mais_recente(self):
        """Com múltiplos pendentes, pega o primeiro da lista (mais próximo)."""
        ags = [
            {"id": "a1", "status": "agendado", "lembrete_enviado_em": "2026-07-13T08:00:00", "data_hora": "2026-07-13 11:00:00"},
            {"id": "a2", "status": "agendado", "lembrete_enviado_em": "2026-07-13T08:00:00", "data_hora": "2026-07-13 14:00:00"},
        ]
        detectados = self._detecta_aguardando(ags)
        assert len(detectados) == 2
        assert detectados[0]["id"] == "a1"
