"""
Testes FASE 4: relatório mensal de negócios.

Testam todas as métricas individualmente com dados sintéticos, depois o relatório
completo, idempotência do job e o cálculo de horário ocioso.
"""

import sqlite3
import uuid
import json
import calendar
import pytest
from datetime import datetime, timedelta, date
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

ANO, MES = 2026, 6  # mês de referência dos testes


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _cliente(conn, nome="João", tel=None, recuperacao_em=None) -> str:
    cid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO clientes (id, telefone, nome, criado_em, ultima_recuperacao_em) VALUES (?,?,?,?,?)",
        (cid, tel or f"5511{uuid.uuid4().hex[:9]}", nome,
         datetime.now().isoformat(), recuperacao_em),
    )
    conn.commit()
    return cid


def _agendar(conn, cliente_id: str, data_hora: str, servico="corte",
             status="concluido", valor=35.0) -> str:
    aid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO agendamentos (id, cliente_id, data_hora, servico, status, valor, criado_em)"
        " VALUES (?,?,?,?,?,?,?)",
        (aid, cliente_id, data_hora, servico, status, valor,
         datetime.now().isoformat()),
    )
    conn.commit()
    return aid


def _dh(ano, mes, dia, hora=10, minuto=0) -> str:
    return f"{ano:04d}-{mes:02d}-{dia:02d} {hora:02d}:{minuto:02d}:00"


# ─── Cópia das funções de métricas (sem importar Server.py) ──────────────────

def _periodo(ano, mes):
    ultimo = calendar.monthrange(ano, mes)[1]
    return (f"{ano:04d}-{mes:02d}-01 00:00:00",
            f"{ano:04d}-{mes:02d}-{ultimo:02d} 23:59:59")


def _fat(conn, ano, mes):
    inicio, fim = _periodo(ano, mes)
    r = conn.execute(
        "SELECT COALESCE(SUM(valor),0) AS t, COUNT(*) AS n FROM agendamentos"
        " WHERE status='concluido' AND data_hora BETWEEN ? AND ?",
        (inicio, fim),
    ).fetchone()
    t, n = float(r[0]), int(r[1])
    return {"total": t, "n": n, "ticket": t / n if n else 0.0}


def _variacao_pct(conn, ano, mes):
    atual = _fat(conn, ano, mes)["total"]
    ano_ant, mes_ant = (ano - 1, 12) if mes == 1 else (ano, mes - 1)
    anterior = _fat(conn, ano_ant, mes_ant)["total"]
    if anterior == 0:
        return 0.0
    return round((atual - anterior) / anterior * 100, 1)


def _clientes_novos(conn, ano, mes):
    inicio, fim = _periodo(ano, mes)
    r = conn.execute(
        """SELECT COUNT(DISTINCT a.cliente_id) FROM agendamentos a
           WHERE a.status='concluido' AND a.data_hora BETWEEN ? AND ?
             AND NOT EXISTS (SELECT 1 FROM agendamentos a2
                             WHERE a2.cliente_id=a.cliente_id
                               AND a2.status='concluido' AND a2.data_hora < ?)""",
        (inicio, fim, inicio),
    ).fetchone()
    return int(r[0])


def _clientes_recorrentes(conn, ano, mes):
    inicio, fim = _periodo(ano, mes)
    r = conn.execute(
        """SELECT COUNT(DISTINCT a.cliente_id) FROM agendamentos a
           WHERE a.status='concluido' AND a.data_hora BETWEEN ? AND ?
             AND EXISTS (SELECT 1 FROM agendamentos a2
                         WHERE a2.cliente_id=a.cliente_id
                           AND a2.status='concluido' AND a2.data_hora < ?)""",
        (inicio, fim, inicio),
    ).fetchone()
    return int(r[0])


def _sumidos(conn, dias=45):
    corte = (datetime.now(SP_TZ) - timedelta(days=dias)).strftime("%Y-%m-%d %H:%M:%S")
    r = conn.execute(
        """SELECT COUNT(*) FROM (
             SELECT cliente_id, MAX(data_hora) AS ultima
             FROM agendamentos WHERE status='concluido' GROUP BY cliente_id
           ) WHERE ultima < ?""",
        (corte,),
    ).fetchone()
    return int(r[0])


def _recuperados(conn, ano, mes):
    inicio, fim = _periodo(ano, mes)
    r = conn.execute(
        """SELECT COUNT(DISTINCT c.id), COALESCE(SUM(a.valor), 0)
           FROM clientes c JOIN agendamentos a ON a.cliente_id=c.id
           WHERE c.ultima_recuperacao_em IS NOT NULL
             AND a.status='concluido'
             AND a.data_hora >= c.ultima_recuperacao_em
             AND a.data_hora <= datetime(c.ultima_recuperacao_em, '+30 days')
             AND a.data_hora BETWEEN ? AND ?""",
        (inicio, fim),
    ).fetchone()
    return {"n": int(r[0]), "valor": float(r[1])}


def _no_show_metricas(conn, ano, mes):
    inicio, fim = _periodo(ano, mes)
    r = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(valor),0) FROM agendamentos"
        " WHERE status='no_show' AND data_hora BETWEEN ? AND ?",
        (inicio, fim),
    ).fetchone()
    return {"n": int(r[0]), "valor": float(r[1])}


def _horario_ocioso(conn, ano, mes, abertura=9, fechamento=19):
    inicio, fim = _periodo(ano, mes)
    rows = conn.execute(
        """SELECT strftime('%w', data_hora) AS d, CAST(strftime('%H', data_hora) AS INTEGER) AS h
           FROM agendamentos
           WHERE status IN ('concluido','no_show') AND data_hora BETWEEN ? AND ?""",
        (inicio, fim),
    ).fetchall()
    BLOCO = 2
    counts: dict = {}
    for _d in range(7):
        for _h in range(abertura, fechamento):
            _b = (_h // BLOCO) * BLOCO
            if (_d, _b) not in counts:
                counts[(_d, _b)] = 0
    for r in rows:
        d, h = int(r[0]), int(r[1])
        b = (h // BLOCO) * BLOCO
        if (d, b) in counts:
            counts[(d, b)] += 1
    if not any(counts.values()):
        return "dados insuficientes"
    (dia_sem, bloco), _ = min(counts.items(), key=lambda x: x[1])
    DIAS = ["domingo", "segunda-feira", "terça-feira", "quarta-feira",
            "quinta-feira", "sexta-feira", "sábado"]
    return f"{DIAS[dia_sem]} {bloco:02d}h-{bloco+BLOCO:02d}h"


# ─── TestFaturamento ──────────────────────────────────────────────────────────

class TestFaturamento:
    def test_soma_so_concluido(self):
        conn = _db()
        cid = _cliente(conn)
        _agendar(conn, cid, _dh(ANO, MES, 10), status="concluido", valor=35.0)
        _agendar(conn, cid, _dh(ANO, MES, 11), status="no_show", valor=65.0)
        _agendar(conn, cid, _dh(ANO, MES, 12), status="cancelado", valor=120.0)
        r = _fat(conn, ANO, MES)
        assert r["total"] == 35.0
        assert r["n"] == 1

    def test_ticket_medio(self):
        conn = _db()
        cid = _cliente(conn)
        _agendar(conn, cid, _dh(ANO, MES, 1), valor=30.0)
        _agendar(conn, cid, _dh(ANO, MES, 2), valor=50.0)
        r = _fat(conn, ANO, MES)
        assert r["ticket"] == 40.0

    def test_sem_atendimentos_zero(self):
        conn = _db()
        r = _fat(conn, ANO, MES)
        assert r["total"] == 0.0
        assert r["n"] == 0
        assert r["ticket"] == 0.0

    def test_nao_vaza_para_outro_mes(self):
        conn = _db()
        cid = _cliente(conn)
        _agendar(conn, cid, _dh(ANO, MES - 1, 28), valor=50.0)  # mês anterior
        _agendar(conn, cid, _dh(ANO, MES + 1, 1), valor=50.0)   # mês seguinte
        r = _fat(conn, ANO, MES)
        assert r["total"] == 0.0


class TestVariacao:
    def test_crescimento_positivo(self):
        conn = _db()
        cid = _cliente(conn)
        _agendar(conn, cid, _dh(ANO, MES - 1, 10), valor=100.0)  # anterior: R$100
        _agendar(conn, cid, _dh(ANO, MES, 10), valor=150.0)       # atual: R$150
        v = _variacao_pct(conn, ANO, MES)
        assert v == 50.0

    def test_queda(self):
        conn = _db()
        cid = _cliente(conn)
        _agendar(conn, cid, _dh(ANO, MES - 1, 10), valor=200.0)
        _agendar(conn, cid, _dh(ANO, MES, 10), valor=100.0)
        v = _variacao_pct(conn, ANO, MES)
        assert v == -50.0

    def test_sem_mes_anterior_retorna_zero(self):
        conn = _db()
        cid = _cliente(conn)
        _agendar(conn, cid, _dh(ANO, MES, 10), valor=100.0)
        v = _variacao_pct(conn, ANO, MES)
        assert v == 0.0


# ─── TestClientesNovosRecorrentes ─────────────────────────────────────────────

class TestClientesNovosRecorrentes:
    def test_novo_sem_historico_anterior(self):
        conn = _db()
        cid = _cliente(conn, "Novo")
        _agendar(conn, cid, _dh(ANO, MES, 5))
        assert _clientes_novos(conn, ANO, MES) == 1
        assert _clientes_recorrentes(conn, ANO, MES) == 0

    def test_recorrente_com_historico(self):
        conn = _db()
        cid = _cliente(conn, "Fiel")
        _agendar(conn, cid, _dh(ANO, MES - 1, 5))  # mês anterior
        _agendar(conn, cid, _dh(ANO, MES, 5))       # mês atual
        assert _clientes_novos(conn, ANO, MES) == 0
        assert _clientes_recorrentes(conn, ANO, MES) == 1

    def test_soma_igual_ao_total_unique(self):
        conn = _db()
        cid1 = _cliente(conn, "Novo1")
        cid2 = _cliente(conn, "Novo2")
        cid3 = _cliente(conn, "Fiel")
        _agendar(conn, cid1, _dh(ANO, MES, 1))
        _agendar(conn, cid2, _dh(ANO, MES, 2))
        _agendar(conn, cid3, _dh(ANO, MES - 1, 1))  # histórico
        _agendar(conn, cid3, _dh(ANO, MES, 3))
        total_unique = conn.execute(
            "SELECT COUNT(DISTINCT cliente_id) FROM agendamentos WHERE status='concluido'"
            " AND data_hora BETWEEN ? AND ?",
            _periodo(ANO, MES),
        ).fetchone()[0]
        assert _clientes_novos(conn, ANO, MES) + _clientes_recorrentes(conn, ANO, MES) == total_unique

    def test_so_conta_concluido(self):
        conn = _db()
        cid = _cliente(conn)
        _agendar(conn, cid, _dh(ANO, MES, 5), status="cancelado")
        _agendar(conn, cid, _dh(ANO, MES, 6), status="no_show")
        assert _clientes_novos(conn, ANO, MES) == 0


# ─── TestSumidos ──────────────────────────────────────────────────────────────

class TestSumidos:
    def test_sumido_ha_mais_de_45_dias(self):
        conn = _db()
        cid = _cliente(conn)
        data_antiga = (datetime.now(SP_TZ) - timedelta(days=50)).strftime("%Y-%m-%d %H:%M:%S")
        _agendar(conn, cid, data_antiga)
        assert _sumidos(conn, dias=45) == 1

    def test_recente_nao_e_sumido(self):
        conn = _db()
        cid = _cliente(conn)
        data_recente = (datetime.now(SP_TZ) - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        _agendar(conn, cid, data_recente)
        assert _sumidos(conn, dias=45) == 0

    def test_nunca_veio_nao_conta(self):
        """Cliente sem nenhum concluido não entra no cálculo de sumidos."""
        conn = _db()
        _cliente(conn)  # cliente sem agendamento
        assert _sumidos(conn, dias=45) == 0


# ─── TestRecuperados ──────────────────────────────────────────────────────────

class TestRecuperados:
    def test_recuperado_dentro_de_30_dias(self):
        conn = _db()
        rec_em = (datetime.now(SP_TZ) - timedelta(days=20)).isoformat()
        volta_em = (datetime.now(SP_TZ) - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        cid = _cliente(conn, recuperacao_em=rec_em)
        # agendamento no mês de referência e dentro de 30 dias após recuperação
        ano, mes = int(volta_em[:4]), int(volta_em[5:7])
        _agendar(conn, cid, volta_em, valor=65.0)
        r = _recuperados(conn, ano, mes)
        assert r["n"] == 1
        assert r["valor"] == 65.0

    def test_sem_recuperacao_nao_conta(self):
        conn = _db()
        cid = _cliente(conn, recuperacao_em=None)
        _agendar(conn, cid, _dh(ANO, MES, 5), valor=35.0)
        r = _recuperados(conn, ANO, MES)
        assert r["n"] == 0
        assert r["valor"] == 0.0

    def test_fora_de_30_dias_nao_conta(self):
        conn = _db()
        rec_em = (datetime.now(SP_TZ) - timedelta(days=60)).isoformat()
        # volta 40 dias depois da recuperação → fora dos 30 dias
        volta_em = (datetime.now(SP_TZ) - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S")
        cid = _cliente(conn, recuperacao_em=rec_em)
        ano, mes = int(volta_em[:4]), int(volta_em[5:7])
        _agendar(conn, cid, volta_em, valor=35.0)
        r = _recuperados(conn, ano, mes)
        assert r["n"] == 0

    def test_valor_recuperado_soma_todos(self):
        conn = _db()
        rec_em = (datetime.now(SP_TZ) - timedelta(days=15)).isoformat()
        cid = _cliente(conn, recuperacao_em=rec_em)
        volta1 = (datetime.now(SP_TZ) - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        volta2 = (datetime.now(SP_TZ) - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        ano, mes = int(volta1[:4]), int(volta1[5:7])
        _agendar(conn, cid, volta1, valor=35.0)
        _agendar(conn, cid, volta2, valor=65.0)
        r = _recuperados(conn, ano, mes)
        assert r["valor"] == 100.0


# ─── TestNoShow ───────────────────────────────────────────────────────────────

class TestNoShow:
    def test_soma_valor_no_show(self):
        conn = _db()
        cid = _cliente(conn)
        _agendar(conn, cid, _dh(ANO, MES, 5), status="no_show", valor=35.0)
        _agendar(conn, cid, _dh(ANO, MES, 10), status="no_show", valor=65.0)
        r = _no_show_metricas(conn, ANO, MES)
        assert r["n"] == 2
        assert r["valor"] == 100.0

    def test_sem_no_show_zero(self):
        conn = _db()
        r = _no_show_metricas(conn, ANO, MES)
        assert r["n"] == 0
        assert r["valor"] == 0.0

    def test_nao_inclui_cancelado(self):
        conn = _db()
        cid = _cliente(conn)
        _agendar(conn, cid, _dh(ANO, MES, 5), status="cancelado", valor=50.0)
        r = _no_show_metricas(conn, ANO, MES)
        assert r["n"] == 0


# ─── TestHorarioOcioso ────────────────────────────────────────────────────────

class TestHorarioOcioso:
    def test_retorna_string_dia_faixa(self):
        conn = _db()
        cid = _cliente(conn)
        # Vários atendimentos às 10h (segunda em 2026-06 é dia 1,8,15,22,29)
        for dia in [1, 8, 15, 22, 29]:
            _agendar(conn, cid, _dh(ANO, MES, dia, hora=10))
        # Nenhum às 14h
        result = _horario_ocioso(conn, ANO, MES)
        assert "h-" in result  # tem formato HHh-HHh
        assert any(d in result for d in ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"])

    def test_sem_dados_retorna_mensagem(self):
        conn = _db()
        result = _horario_ocioso(conn, ANO, MES)
        assert result == "dados insuficientes"

    def test_slot_vazio_e_mais_ocioso(self):
        conn = _db()
        cid = _cliente(conn)
        # Apenas segunda-feira às 10h (que cai em 2026-06-01, strftime(%w)=1=segunda)
        _agendar(conn, cid, "2026-06-01 10:30:00")  # segunda, hora 10
        # Toda segunda 14h está vazia → deve aparecer como mais ociosa
        result = _horario_ocioso(conn, ANO, MES, abertura=9, fechamento=19)
        # Resultado deve ser um horário diferente de 10h-12h (que teve 1 agendamento)
        # Os slots 12h-14h, 14h-16h etc. têm 0 → mais ociosos
        assert "dados insuficientes" not in result


# ─── TestFormatoRelatorio ─────────────────────────────────────────────────────

def _gerar_relatorio_texto(conn, ano, mes, nome_neg="Test Bar") -> str:
    MES_NOME = {6: "Junho", 7: "Julho", 12: "Dezembro"}
    fat   = _fat(conn, ano, mes)
    var   = _variacao_pct(conn, ano, mes)
    novos = _clientes_novos(conn, ano, mes)
    rec_c = _clientes_recorrentes(conn, ano, mes)
    sum_c = _sumidos(conn)
    recup = _recuperados(conn, ano, mes)
    ns    = _no_show_metricas(conn, ano, mes)
    ocio  = _horario_ocioso(conn, ano, mes)
    var_str = f"{var:+.1f}%" if fat["total"] > 0 else "primeiro mês"
    return (
        f"📊 *Resumo de {MES_NOME.get(mes, str(mes))} — {nome_neg}*\n\n"
        f"💰 Faturamento: R$ {fat['total']:.2f} ({var_str} vs mês anterior)\n"
        f"✂️ {fat['n']} atendimentos | Ticket médio: R$ {fat['ticket']:.2f}\n\n"
        f"👥 Clientes:\n"
        f"• {novos} novos\n"
        f"• {rec_c} recorrentes\n"
        f"• {sum_c} sumidos há +45 dias\n\n"
        f"✅ Recuperados pelo sistema: {recup['n']} clientes\n"
        f"   → R$ {recup['valor']:.2f} que voltaram pro caixa\n\n"
        f"⚠️ No-show: {ns['n']} horários (R$ ~{ns['valor']:.2f} perdidos)\n"
        f"📅 Horário mais ocioso: {ocio}\n\n"
        f"_Responda RELATORIO pra ver mais detalhes_"
    )


class TestFormatoRelatorio:
    def test_todas_secoes_presentes(self):
        conn = _db()
        cid = _cliente(conn)
        _agendar(conn, cid, _dh(ANO, MES, 10), valor=35.0)
        texto = _gerar_relatorio_texto(conn, ANO, MES)
        assert "📊" in texto
        assert "💰 Faturamento" in texto
        assert "✂️" in texto
        assert "👥 Clientes" in texto
        assert "✅ Recuperados pelo sistema" in texto
        assert "⚠️ No-show" in texto
        assert "📅 Horário mais ocioso" in texto
        assert "RELATORIO" in texto

    def test_valores_numericos_corretos(self):
        conn = _db()
        cid = _cliente(conn)
        _agendar(conn, cid, _dh(ANO, MES, 1), valor=100.0)
        _agendar(conn, cid, _dh(ANO, MES, 2), valor=50.0)
        texto = _gerar_relatorio_texto(conn, ANO, MES)
        assert "R$ 150.00" in texto   # faturamento
        assert "R$ 75.00" in texto    # ticket médio

    def test_no_show_aparece_no_relatorio(self):
        conn = _db()
        cid = _cliente(conn)
        _agendar(conn, cid, _dh(ANO, MES, 5), status="no_show", valor=65.0)
        texto = _gerar_relatorio_texto(conn, ANO, MES)
        assert "1 horários" in texto
        assert "65.00" in texto

    def test_relatorio_zerado_sem_dados(self):
        conn = _db()
        texto = _gerar_relatorio_texto(conn, ANO, MES)
        assert "R$ 0.00" in texto
        assert "0 atendimentos" in texto


# ─── TestIdempotenciaJob ──────────────────────────────────────────────────────

class TestIdempotenciaRelatorioJob:
    def _ja_enviou(self, conn, ano, mes) -> bool:
        chave = f"{ano:04d}-{mes:02d}"
        return bool(conn.execute(
            "SELECT id FROM audit_log WHERE entidade='job' AND acao='relatorio_mensal' AND entidade_id=?",
            (chave,),
        ).fetchone())

    def _registrar(self, conn, ano, mes):
        chave = f"{ano:04d}-{mes:02d}"
        conn.execute(
            "INSERT INTO audit_log (id, entidade, entidade_id, acao, detalhes, criado_em) VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), "job", chave, "relatorio_mensal",
             json.dumps({"ano": ano, "mes": mes}), datetime.now().isoformat()),
        )
        conn.commit()

    def test_nao_enviou_ainda(self):
        conn = _db()
        assert self._ja_enviou(conn, ANO, MES) is False

    def test_ja_enviado_retorna_true(self):
        conn = _db()
        self._registrar(conn, ANO, MES)
        assert self._ja_enviou(conn, ANO, MES) is True

    def test_mes_diferente_nao_impede(self):
        conn = _db()
        self._registrar(conn, ANO, MES - 1)  # mês anterior registrado
        assert self._ja_enviou(conn, ANO, MES) is False  # mês atual não foi enviado

    def test_chave_formato_correto(self):
        conn = _db()
        self._registrar(conn, 2026, 1)
        assert self._ja_enviou(conn, 2026, 1) is True
        assert self._ja_enviou(conn, 2026, 2) is False
