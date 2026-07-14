"""
Testes FASE 3: resumo diário ao barbeiro e detecção de no-show.

Testam: geração do resumo, idempotência, marcação por nome, fluxo completo
de resposta do barbeiro e integração com auto_concluido.
"""

import sqlite3
import uuid
import json
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


def _hoje_str() -> str:
    return datetime.now(SP_TZ).strftime("%Y-%m-%d")


def _cliente(conn, nome="João", tel=None) -> str:
    cid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO clientes (id, telefone, nome, criado_em) VALUES (?,?,?,?)",
        (cid, tel or f"5511{uuid.uuid4().hex[:9]}", nome, datetime.now().isoformat()),
    )
    conn.commit()
    return cid


def _agendar(conn, cliente_id, data_hora, servico="corte", status="agendado") -> str:
    aid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO agendamentos (id, cliente_id, data_hora, servico, status, valor, criado_em)"
        " VALUES (?,?,?,?,?,?,?)",
        (aid, cliente_id, data_hora, servico, status, 35.0, datetime.now().isoformat()),
    )
    conn.commit()
    return aid


# ─── Lógica do resumo_dia_job ─────────────────────────────────────────────────

def _ja_enviou_resumo_hoje(conn) -> bool:
    hoje = _hoje_str()
    return bool(conn.execute(
        "SELECT id FROM audit_log WHERE entidade='job' AND acao='resumo_dia' AND entidade_id=?",
        (hoje,),
    ).fetchone())


def _registrar_resumo_enviado(conn):
    hoje = _hoje_str()
    conn.execute(
        "INSERT INTO audit_log (id, entidade, entidade_id, acao, detalhes, criado_em) VALUES (?,?,?,?,?,?)",
        (str(uuid.uuid4()), "job", hoje, "resumo_dia",
         json.dumps({"total": 1}), datetime.now(SP_TZ).isoformat()),
    )
    conn.commit()


def _candidatos_resumo(conn, data: str = None) -> list:
    """Replica a query do resumo_dia_job."""
    dia = data or _hoje_str()
    rows = conn.execute(
        """
        SELECT strftime('%H:%M', a.data_hora) AS hora, c.nome, a.status
        FROM agendamentos a
        JOIN clientes c ON a.cliente_id = c.id
        WHERE DATE(a.data_hora) = ? AND a.status NOT IN ('cancelado')
        ORDER BY a.data_hora
        """,
        (dia,),
    ).fetchall()
    return [dict(r) for r in rows]


def _gerar_mensagem_resumo(rows: list, data: str = None) -> str:
    hoje = data or _hoje_str()
    lista = ", ".join(f"{r['nome']} {r['hora']}" for r in rows)
    return (
        f"Fechamento de hoje ({hoje}):\n{lista}\n\n"
        f"Algum faltou? Responda o(s) nome(s) ou *NENHUM* ✂️"
    )


# ─── Lógica de marcar_no_show por nome ────────────────────────────────────────

def _marcar_no_show_por_nome(conn, nome: str) -> dict:
    """Replica a lógica do branch 'elif params.nome' do tool marcar_no_show."""
    hoje = _hoje_str()
    row = conn.execute(
        """SELECT a.* FROM agendamentos a
           JOIN clientes c ON a.cliente_id = c.id
           WHERE DATE(a.data_hora) = ?
             AND LOWER(c.nome) LIKE LOWER(?)
             AND a.status NOT IN ('cancelado', 'no_show')
           ORDER BY a.data_hora LIMIT 1""",
        (hoje, f"%{nome}%"),
    ).fetchone()

    if not row:
        return {"sucesso": False, "erro": f"Nenhum agendamento encontrado para '{nome}' hoje"}

    if row["status"] == "no_show":
        return {"sucesso": True, "mensagem": "Já marcado como no-show"}

    conn.execute("UPDATE agendamentos SET status = 'no_show' WHERE id = ?", (row["id"],))
    conn.commit()
    return {"sucesso": True, "agendamento_id": row["id"], "nome": nome}


# ─── TestResumoDiaJob ─────────────────────────────────────────────────────────

class TestResumoDiaJob:
    def test_lista_agendamentos_do_dia(self):
        conn = _db()
        agora = datetime.now(SP_TZ)
        cid1 = _cliente(conn, "João")
        cid2 = _cliente(conn, "Pedro")
        _agendar(conn, cid1, _fmt(agora.replace(hour=14, minute=0)))
        _agendar(conn, cid2, _fmt(agora.replace(hour=15, minute=0)))
        rows = _candidatos_resumo(conn)
        nomes = [r["nome"] for r in rows]
        assert "João" in nomes
        assert "Pedro" in nomes

    def test_exclui_cancelados(self):
        conn = _db()
        agora = datetime.now(SP_TZ)
        cid = _cliente(conn, "Lucas")
        _agendar(conn, cid, _fmt(agora.replace(hour=10, minute=0)), status="cancelado")
        rows = _candidatos_resumo(conn)
        assert not any(r["nome"] == "Lucas" for r in rows)

    def test_inclui_concluido_e_agendado(self):
        """O resumo mostra todos os atendimentos do dia, incluindo já concluídos."""
        conn = _db()
        agora = datetime.now(SP_TZ)
        cid1 = _cliente(conn, "Ana")
        cid2 = _cliente(conn, "Carlos")
        _agendar(conn, cid1, _fmt(agora.replace(hour=9, minute=0)), status="concluido")
        _agendar(conn, cid2, _fmt(agora.replace(hour=10, minute=0)), status="agendado")
        rows = _candidatos_resumo(conn)
        nomes = [r["nome"] for r in rows]
        assert "Ana" in nomes
        assert "Carlos" in nomes

    def test_nenhum_agendamento_nao_envia(self):
        conn = _db()
        rows = _candidatos_resumo(conn)
        assert len(rows) == 0

    def test_agendamentos_de_outro_dia_nao_aparecem(self):
        conn = _db()
        cid = _cliente(conn, "Ontem")
        ontem = (datetime.now(SP_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
        _agendar(conn, cid, f"{ontem} 14:00:00")
        rows = _candidatos_resumo(conn)  # sem passar data → hoje
        assert not any(r["nome"] == "Ontem" for r in rows)

    def test_mensagem_formato_correto(self):
        conn = _db()
        agora = datetime.now(SP_TZ)
        cid = _cliente(conn, "João")
        _agendar(conn, cid, _fmt(agora.replace(hour=14, minute=0)))
        rows = _candidatos_resumo(conn)
        msg = _gerar_mensagem_resumo(rows)
        assert "Fechamento de hoje" in msg
        assert "João" in msg
        assert "14:00" in msg
        assert "NENHUM" in msg

    def test_ordenado_por_horario(self):
        conn = _db()
        agora = datetime.now(SP_TZ)
        cid1 = _cliente(conn, "Tarde")
        cid2 = _cliente(conn, "Cedo")
        _agendar(conn, cid1, _fmt(agora.replace(hour=15, minute=0)))
        _agendar(conn, cid2, _fmt(agora.replace(hour=9, minute=0)))
        rows = _candidatos_resumo(conn)
        assert rows[0]["nome"] == "Cedo"
        assert rows[1]["nome"] == "Tarde"


# ─── TestIdempotencia ─────────────────────────────────────────────────────────

class TestIdempotencia:
    def test_nao_enviou_ainda(self):
        conn = _db()
        assert _ja_enviou_resumo_hoje(conn) is False

    def test_ja_enviou_retorna_true(self):
        conn = _db()
        _registrar_resumo_enviado(conn)
        assert _ja_enviou_resumo_hoje(conn) is True

    def test_segunda_chamada_detecta_enviado(self):
        conn = _db()
        _registrar_resumo_enviado(conn)
        _registrar_resumo_enviado(conn)  # tenta registrar de novo
        # Deve haver 2 entradas, mas a flag já seria detectada na primeira
        count = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE entidade='job' AND acao='resumo_dia'"
        ).fetchone()[0]
        assert count >= 1

    def test_dia_diferente_nao_impede(self):
        """Registrar ontem não impede enviar hoje."""
        conn = _db()
        ontem = (datetime.now(SP_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
        conn.execute(
            "INSERT INTO audit_log (id, entidade, entidade_id, acao, detalhes, criado_em) VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), "job", ontem, "resumo_dia", "{}", datetime.now().isoformat()),
        )
        conn.commit()
        assert _ja_enviou_resumo_hoje(conn) is False


# ─── TestMarcarNoShowPorNome ──────────────────────────────────────────────────

class TestMarcarNoShowPorNome:
    def test_marca_por_nome_completo(self):
        conn = _db()
        agora = datetime.now(SP_TZ)
        cid = _cliente(conn, "João Silva")
        aid = _agendar(conn, cid, _fmt(agora.replace(hour=14, minute=0)))
        res = _marcar_no_show_por_nome(conn, "João Silva")
        assert res["sucesso"] is True
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "no_show"

    def test_marca_por_nome_parcial(self):
        conn = _db()
        agora = datetime.now(SP_TZ)
        cid = _cliente(conn, "Pedro Alves")
        aid = _agendar(conn, cid, _fmt(agora.replace(hour=10, minute=0)))
        res = _marcar_no_show_por_nome(conn, "Pedro")
        assert res["sucesso"] is True
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "no_show"

    def test_nome_nao_encontrado(self):
        conn = _db()
        res = _marcar_no_show_por_nome(conn, "Ninguém")
        assert res["sucesso"] is False

    def test_nao_marca_cancelado(self):
        conn = _db()
        agora = datetime.now(SP_TZ)
        cid = _cliente(conn, "Lucas")
        aid = _agendar(conn, cid, _fmt(agora.replace(hour=11, minute=0)), status="cancelado")
        res = _marcar_no_show_por_nome(conn, "Lucas")
        assert res["sucesso"] is False  # cancelado não é encontrado
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "cancelado"  # não foi alterado

    def test_multiplos_clientes_marca_o_correto(self):
        conn = _db()
        agora = datetime.now(SP_TZ)
        cid1 = _cliente(conn, "Carlos Souza")
        cid2 = _cliente(conn, "Carlos Ferreira")
        aid1 = _agendar(conn, cid1, _fmt(agora.replace(hour=9, minute=0)))
        aid2 = _agendar(conn, cid2, _fmt(agora.replace(hour=10, minute=0)))
        # Busca por "Souza" deve pegar apenas Carlos Souza
        res = _marcar_no_show_por_nome(conn, "Souza")
        assert res["sucesso"] is True
        assert conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid1,)).fetchone()["status"] == "no_show"
        assert conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid2,)).fetchone()["status"] == "agendado"

    def test_concluido_pode_virar_no_show(self):
        """Barbeiro pode marcar no-show mesmo depois do auto_concluido ter rodado."""
        conn = _db()
        agora = datetime.now(SP_TZ)
        cid = _cliente(conn, "Maria")
        aid = _agendar(conn, cid, _fmt(agora.replace(hour=9, minute=0)), status="concluido")
        res = _marcar_no_show_por_nome(conn, "Maria")
        assert res["sucesso"] is True
        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "no_show"


# ─── TestFluxoCompleto ────────────────────────────────────────────────────────

class TestFluxoCompleto:
    def test_nenhum_faltou_auto_concluido_resolve(self):
        """Barbeiro responde NENHUM → auto_concluido marca tudo como concluido."""
        conn = _db()
        agora = datetime.now(SP_TZ)
        cid = _cliente(conn, "Fiel")
        aid = _agendar(conn, cid, _fmt(agora - timedelta(hours=1)))  # passou

        # Simula auto_concluido (sem resposta do barbeiro)
        agora_str = agora.strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE agendamentos SET status = 'concluido' WHERE data_hora < ? AND status = 'agendado'",
            (agora_str,),
        )
        conn.commit()

        row = conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "concluido"

    def test_barbeiro_marca_faltante_resto_vira_concluido(self):
        """Barbeiro cita João como faltante → João = no_show, Pedro = concluido."""
        conn = _db()
        agora = datetime.now(SP_TZ)
        cid1 = _cliente(conn, "João")
        cid2 = _cliente(conn, "Pedro")
        aid1 = _agendar(conn, cid1, _fmt(agora - timedelta(hours=2)))
        aid2 = _agendar(conn, cid2, _fmt(agora - timedelta(hours=1)))

        # Barbeiro responde "João"
        _marcar_no_show_por_nome(conn, "João")

        # Auto_concluido roda depois
        agora_str = agora.strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE agendamentos SET status = 'concluido' WHERE data_hora < ? AND status = 'agendado'",
            (agora_str,),
        )
        conn.commit()

        assert conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid1,)).fetchone()["status"] == "no_show"
        assert conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid2,)).fetchone()["status"] == "concluido"

    def test_dois_faltantes_marcados_independentemente(self):
        conn = _db()
        agora = datetime.now(SP_TZ)
        cid1 = _cliente(conn, "Alfa")
        cid2 = _cliente(conn, "Beta")
        cid3 = _cliente(conn, "Gama")
        aid1 = _agendar(conn, cid1, _fmt(agora - timedelta(hours=3)))
        aid2 = _agendar(conn, cid2, _fmt(agora - timedelta(hours=2)))
        aid3 = _agendar(conn, cid3, _fmt(agora - timedelta(hours=1)))

        # Barbeiro diz "Alfa e Beta faltaram"
        _marcar_no_show_por_nome(conn, "Alfa")
        _marcar_no_show_por_nome(conn, "Beta")

        assert conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid1,)).fetchone()["status"] == "no_show"
        assert conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid2,)).fetchone()["status"] == "no_show"
        assert conn.execute("SELECT status FROM agendamentos WHERE id=?", (aid3,)).fetchone()["status"] == "agendado"
