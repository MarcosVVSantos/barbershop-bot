import os, sys, json, time, httpx, asyncio, sqlite3, uuid
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse
from dotenv import load_dotenv
from openai import AsyncOpenAI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

load_dotenv()

DEEPSEEK_API_KEY   = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL  = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
EVOLUTION_API_URL  = os.getenv("EVOLUTION_API_URL", "http://localhost:8080")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "barbershop")
MCP_SERVER_URL     = os.getenv("MCP_SERVER_URL", "http://localhost:8001/mcp")
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "")
BARBEIRO_PHONE      = os.getenv("BARBEIRO_PHONE", "")
DIAS_INATIVIDADE    = int(os.getenv("DIAS_INATIVIDADE", "30"))
HORARIO_RECUPERACAO = int(os.getenv("HORARIO_RECUPERACAO", "10"))
DB_PATH             = Path(os.getenv("DB_PATH", "/app/data/barbershop.db"))
SP_TZ               = ZoneInfo("America/Sao_Paulo")

# ── Configuração multi-tenant (barbershops.yaml) ──────────────────────────────
_BARBERSHOPS_PATH = Path(os.getenv("BARBERSHOPS_PATH", "/app/barbershops.yaml"))

def _load_shop_config() -> dict:
    """Lê barbershops.yaml e retorna config do tenant 'default'."""
    if not _HAS_YAML or not _BARBERSHOPS_PATH.exists():
        return {}
    try:
        with open(_BARBERSHOPS_PATH, encoding="utf-8") as f:
            data = _yaml.safe_load(f)
        shops = data.get("barbershops", [])
        return next((s for s in shops if s.get("id") == "default"), shops[0] if shops else {})
    except Exception as e:
        print(f"[WARN] Erro ao ler barbershops.yaml: {e}", flush=True)
        return {}

_shop_cfg = _load_shop_config()
_jobs_cfg = _shop_cfg.get("jobs", {})

# Env vars têm precedência; barbershops.yaml preenche o que estiver vazio
if not BARBEIRO_PHONE and _shop_cfg.get("dono_phone"):
    BARBEIRO_PHONE = _shop_cfg["dono_phone"]
if _shop_cfg.get("evolution_instance"):
    EVOLUTION_INSTANCE = _shop_cfg["evolution_instance"] or EVOLUTION_INSTANCE
DIAS_INATIVIDADE    = int(_jobs_cfg.get("dias_inatividade", DIAS_INATIVIDADE))
HORARIO_RECUPERACAO = int(_jobs_cfg.get("horario_recuperacao", HORARIO_RECUPERACAO))

deepseek_client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL,
)
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

LID_MAP_FILE = Path("/app/data/lid_map.json")

def _load_lid_map() -> dict:
    if LID_MAP_FILE.exists():
        try:
            return json.loads(LID_MAP_FILE.read_text())
        except Exception:
            pass
    return {}

def _save_lid_map(lid_map: dict):
    try:
        LID_MAP_FILE.write_text(json.dumps(lid_map, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"[WARN] Não foi possível salvar lid_map: {e}", flush=True)

async def _resolver_lid_via_api(lid: str) -> str | None:
    """Consulta a Evolution API para obter o telefone real de um LID desconhecido."""
    try:
        url = f"{EVOLUTION_API_URL}/contact/findContacts/{EVOLUTION_INSTANCE}"
        headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=3.0) as http:
            r = await http.post(url, headers=headers, json={"where": {"remoteJid": f"{lid}@lid"}})
            if r.status_code == 200:
                contacts = r.json()
                if isinstance(contacts, list):
                    for c in contacts:
                        jid = c.get("remoteJid") or c.get("id", "")
                        if jid.endswith("@s.whatsapp.net"):
                            return jid.replace("@s.whatsapp.net", "")
    except Exception as e:
        print(f"[WARN] Resolução de LID via API falhou: {e}", flush=True)
    return None

LID_MAP: dict = _load_lid_map()

# Dedup: armazena msg_id -> timestamp para evitar processar a mesma mensagem duas vezes
_processed: dict[str, float] = {}

# ── Config do negócio ─────────────────────────────────────────────────────────
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/app/config.json"))

def _load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception as e:
            print(f"[WARN] Erro ao ler config.json: {e}", flush=True)
    return {}

def _build_system_prompt(cfg: dict) -> str:
    negocio    = cfg.get("negocio", {})
    assistente = cfg.get("assistente", {})
    servicos   = cfg.get("servicos", {})

    nome      = negocio.get("nome", "nosso estabelecimento")
    descricao = negocio.get("descricao", "")
    tom       = assistente.get("tom", "simpático e profissional")
    extras    = assistente.get("regras_extras", [])

    servicos_str = ", ".join(
        f"{s} ({d} min)" for s, d in servicos.items()
    ) if servicos else "consulte a equipe para saber os serviços disponíveis"

    regras_extras_str = ""
    if extras:
        regras_extras_str = "\n" + "\n".join(
            f"{12 + i}. {r}" for i, r in enumerate(extras)
        )

    return f"""Voce eh o assistente virtual d{_artigo(negocio.get('tipo',''))} {nome}, {tom}, em portugues brasileiro.
{f'Sobre nos: {descricao}' if descricao else ''}
Servicos oferecidos: {servicos_str}
A data de hoje e: {{hoje}}.

REGRAS CRITICAS DE USO DE IDs:
- O campo "id" retornado por identificar_cliente eh o cliente_id. Use EXATAMENTE esse UUID em todas as ferramentas que pedem cliente_id
- NUNCA use o telefone como cliente_id. NUNCA invente IDs
- O campo "id" dos agendamentos retornados por historico_cliente eh o agendamento_id para cancelar_agendamento
- NUNCA peca IDs ao usuario — voce obtem tudo via ferramentas

REGRA DE ACAO IMEDIATA:
- Se voce precisa buscar informacao (historico, horarios, agendamentos), chame a ferramenta AGORA. NUNCA diga "vou buscar", "um minutinho", "so um segundo" sem chamar a ferramenta no mesmo momento. Acao primeiro, texto depois.

REGRAS DE SEQUENCIA — chame UMA ferramenta por vez, aguarde o resultado antes de chamar a proxima:
1. Sempre use identificar_cliente PRIMEIRO com o telefone (somente telefone, sem nome). Aguarde o resultado para obter o cliente_id
2. Se cliente novo (status='novo'), PERGUNTE o nome ao usuario ANTES de chamar identificar_cliente novamente. NUNCA use nomes genericos
3. DATAS: Se o cliente informar apenas o dia (ex: 'dia 22'), assuma o mes e ano atuais. Sempre converta para YYYY-MM-DD
4. VALIDACAO DE HORARIO: Chame listar_horarios_disponiveis antes de agendar. Se o horario pedido nao estiver disponivel, sugira os proximos. Somente chame agendar_corte se o horario estiver disponivel
5. Apos agendar_corte retornar sucesso, o agendamento esta CONCLUIDO. NAO chame agendar_corte de novo na mesma conversa
6. CANCELAMENTO: (1) identificar_cliente, (2) historico_cliente com o cliente_id obtido, (3) apresente os agendamentos ao cliente, (4) cancelar_agendamento com o id do agendamento escolhido. Nunca peca o ID ao usuario
7. CANCELAR E REAGENDAR: execute em ordem: cancelar_agendamento → listar_horarios_disponiveis → agendar_corte. Nunca pule o cancelamento
8. Salve preferencias mencionadas pelo cliente
9. FLUXO DE PRODUTOS — execute em ordem quando cliente demonstrar interesse em produto:
   a) Chame listar_produtos para buscar o catalogo atualizado com IDs reais
   b) Apresente os produtos com nome e preco (nao invente valores)
   c) Se cliente quiser comprar, chame fazer_pedido_produto com o produto_id correto do catalogo
   d) Confirme: "Ótimo! [Produto] (R$XX) separado para voce retirar na visita. Pagamento na barbearia!"
   e) NUNCA invente produto_id — use sempre o id retornado por listar_produtos
10. Apos confirmar agendamento, ofereca produtos se houver. Se cliente recusar, despeça-se gentilmente
11. RESPOSTA A LEMBRETE: Se o contexto indicar AGUARDANDO CONFIRMACAO, interprete a mensagem como resposta ao lembrete e chame responder_confirmacao(agendamento_id, confirmou). "sim", "vou", "estarei la", "confirmo" = confirmou=True. "nao vou", "nao consigo", "desmarcar", "cancelar" = confirmou=False. NAO chame cancelar_agendamento nesse caso
12. Use emojis moderadamente{regras_extras_str}
"""

def _artigo(tipo: str) -> str:
    femininos = {"nutricionista", "adega", "loja", "clinica", "farmacia", "academia"}
    return "a" if tipo.lower() in femininos else "o"

_cfg = _load_config()
SYSTEM_PROMPT = _build_system_prompt(_cfg)

ADMIN_SYSTEM_PROMPT = """Voce eh o assistente de gestao da barbearia. O usuario eh o BARBEIRO/DONO.
Responda de forma DIRETA e COMPACTA — estilo painel de controle no WhatsApp. Sem papo de atendimento.
A data de hoje e: {hoje}.

COMANDOS DISPONIVEIS:
- "agenda" ou "agenda hoje" → chame agenda_hoje sem parametros
- "agenda DD/MM" ou "agenda amanha" → chame agenda_hoje com a data convertida para YYYY-MM-DD
- "pedidos" ou "pedidos pendentes" → chame listar_pedidos_pendentes com apenas_hoje=true
- "relatorio" (sozinho) → chame gerar_relatorio_mensal sem parametros (parcial do mes corrente)
- "relatorio da semana" ou "relatorio [periodo]" → chame relatorio_barbeiro com data_inicio e data_fim no formato YYYY-MM-DD
- "top clientes" → chame top_clientes com limite=5
- "estoque" → chame listar_produtos com apenas_disponiveis=false
- "add estoque [produto] [qtd]" → chame atualizar_estoque com modo="adicionar" e a quantidade informada
- "estoque [produto] [qtd]" (com numero) → chame atualizar_estoque com modo="definir" e a quantidade informada
- "sem estoque [produto]" → chame atualizar_estoque com modo="zerar" e quantidade=0
- "recuperar clientes" ou "recuperar clientes [N] dias" → chame recuperar_clientes com dias=N (padrao 30)
- "faltou {{hora}}" ou "faltou {{nome}}" → chame marcar_no_show com data_hora=hoje + hora (ex: "faltou 14:00") ou nome= (ex: "faltou João" → nome="João")
- Resposta ao resumo diario ("Algum faltou?"): se barbeiro citar nome(s) → chame marcar_no_show(nome=...) para CADA nome mencionado, um por vez. Se responder "NENHUM", "nenhum", "todos vieram" ou similar → responda "Ok! Os demais serao marcados como concluido automaticamente." sem chamar ferramenta

FORMATO DAS RESPOSTAS (compacto, sem texto desnecessario):
- Use emojis como icones de secao: 📅 agenda, 📦 pedidos, 📊 relatorio, 👥 clientes, 🧴 estoque
- Liste itens um por linha com dados essenciais
- Sem saudacoes longas, sem perguntas, sem despedidas
- Se nao reconhecer o comando, liste os comandos disponiveis
"""

TOOLS = [
    {"name":"identificar_cliente","description":"Identifica ou cadastra cliente pelo WhatsApp. SEMPRE chame no inicio.","parameters":{"type":"object","properties":{"telefone":{"type":"string"},"nome":{"type":"string"}},"required":["telefone"]}},
    {"name":"listar_horarios_disponiveis","description":"Horarios livres em uma data YYYY-MM-DD.","parameters":{"type":"object","properties":{"data":{"type":"string"}},"required":["data"]}},
    {"name":"agendar_corte","description":"Cria agendamento.","parameters":{"type":"object","properties":{"cliente_id":{"type":"string"},"data":{"type":"string"},"hora":{"type":"string"},"servico":{"type":"string"},"observacoes":{"type":"string"}},"required":["cliente_id","data","hora","servico"]}},
    {"name":"cancelar_agendamento","description":"Cancela agendamento.","parameters":{"type":"object","properties":{"agendamento_id":{"type":"string"},"motivo":{"type":"string"}},"required":["agendamento_id"]}},
    {"name":"atualizar_preferencias_cliente","description":"Salva preferencias do cliente.","parameters":{"type":"object","properties":{"cliente_id":{"type":"string"},"tipo":{"type":"string"},"valor":{"type":"string"}},"required":["cliente_id","tipo","valor"]}},
    {"name":"sugerir_corte","description":"Sugere cortes pelo perfil.","parameters":{"type":"object","properties":{"descricao":{"type":"string"},"cliente_id":{"type":"string"}},"required":["descricao"]}},
    {"name":"listar_produtos","description":"Lista produtos da barbearia.","parameters":{"type":"object","properties":{"categoria":{"type":"string"},"apenas_disponiveis":{"type":"boolean"}}}},
    {"name":"fazer_pedido_produto","description":"Registra pedido de produto.","parameters":{"type":"object","properties":{"cliente_id":{"type":"string"},"produto_id":{"type":"string"},"quantidade":{"type":"integer"},"retirada":{"type":"string"},"agendamento_id":{"type":"string"}},"required":["cliente_id","produto_id"]}},
    {"name":"listar_pedidos_pendentes","description":"Lista pedidos de produtos pendentes/confirmados para o barbeiro ver durante o atendimento.","parameters":{"type":"object","properties":{"status":{"type":"string"},"apenas_hoje":{"type":"boolean"}}}},
    {"name":"agenda_hoje","description":"Lista agendamentos de um dia para o barbeiro. Data opcional (padrao hoje).","parameters":{"type":"object","properties":{"data":{"type":"string"}}}},
    {"name":"top_clientes","description":"Top clientes por frequencia de visitas.","parameters":{"type":"object","properties":{"limite":{"type":"integer"}}}},
    {"name":"relatorio_barbeiro","description":"Relatorio de agendamentos e receita do periodo.","parameters":{"type":"object","properties":{"data_inicio":{"type":"string"},"data_fim":{"type":"string"}},"required":["data_inicio","data_fim"]}},
    {"name":"historico_cliente","description":"Historico do cliente.","parameters":{"type":"object","properties":{"cliente_id":{"type":"string"},"limite":{"type":"integer"}},"required":["cliente_id"]}},
    {"name":"buscar_sessao","description":"Historico de conversa.","parameters":{"type":"object","properties":{"telefone":{"type":"string"},"limite":{"type":"integer"}},"required":["telefone"]}},
    {"name":"salvar_mensagem_sessao","description":"Salva mensagem na sessao.","parameters":{"type":"object","properties":{"telefone":{"type":"string"},"mensagem":{"type":"string"},"papel":{"type":"string"}},"required":["telefone","mensagem","papel"]}},
    {"name":"responder_confirmacao","description":"Registra resposta do cliente ao lembrete de confirmacao de horario. Use SOMENTE quando contexto indicar AGUARDANDO CONFIRMACAO. confirmou=True se vai comparecer, False se cancelou.","parameters":{"type":"object","properties":{"agendamento_id":{"type":"string"},"confirmou":{"type":"boolean"}},"required":["agendamento_id","confirmou"]}}
]

ADMIN_TOOLS = [
    {"type":"function","function":{"name":"agenda_hoje","description":"Lista agendamentos do dia para o barbeiro.","parameters":{"type":"object","properties":{"data":{"type":"string"}}}}},
    {"type":"function","function":{"name":"listar_pedidos_pendentes","description":"Lista pedidos de produtos pendentes/confirmados.","parameters":{"type":"object","properties":{"status":{"type":"string"},"apenas_hoje":{"type":"boolean"}}}}},
    {"type":"function","function":{"name":"relatorio_barbeiro","description":"Relatorio do periodo: agendamentos, receita e produtos.","parameters":{"type":"object","properties":{"data_inicio":{"type":"string"},"data_fim":{"type":"string"}},"required":["data_inicio","data_fim"]}}},
    {"type":"function","function":{"name":"top_clientes","description":"Top clientes por frequencia.","parameters":{"type":"object","properties":{"limite":{"type":"integer"}}}}},
    {"type":"function","function":{"name":"listar_produtos","description":"Lista produtos com estoque.","parameters":{"type":"object","properties":{"categoria":{"type":"string"},"apenas_disponiveis":{"type":"boolean"}}}}},
    {"type":"function","function":{"name":"atualizar_estoque","description":"Atualiza estoque de um produto pelo nome.","parameters":{"type":"object","properties":{"nome_produto":{"type":"string"},"quantidade":{"type":"integer"},"modo":{"type":"string","enum":["adicionar","definir","zerar"]}},"required":["nome_produto","modo"]}}},
    {"type":"function","function":{"name":"recuperar_clientes","description":"Envia mensagem de recuperacao no WhatsApp para clientes inativos ha mais de N dias.","parameters":{"type":"object","properties":{"dias":{"type":"integer","description":"Dias de inatividade (padrao 30)"}}}}},
    {"type":"function","function":{"name":"marcar_no_show","description":"Marca cliente como no-show (faltou sem avisar). Aceita agendamento_id, data_hora ou nome (busca no dia de hoje). Use nome= para processar respostas ao resumo diario.","parameters":{"type":"object","properties":{"agendamento_id":{"type":"string"},"data_hora":{"type":"string","description":"YYYY-MM-DD HH:MM"},"nome":{"type":"string","description":"Nome parcial do cliente — busca automaticamente no dia de hoje"}}}}},
    {"type":"function","function":{"name":"gerar_relatorio_mensal","description":"Gera relatorio mensal de negocios com faturamento, clientes, no-show e horario ocioso. Sem parametros = mes corrente parcial.","parameters":{"type":"object","properties":{"ano":{"type":"integer"},"mes":{"type":"integer","description":"1-12"}}}}},
]

LLM_TOOLS = [
    {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
    for t in TOOLS
]

async def auto_concluido_job():
    """Marca como concluido agendamentos passados que ainda estão como agendado.

    Roda a cada hora. Garante que silêncio do barbeiro nunca vira no-show —
    apenas appointments explicitamente marcados pelo barbeiro recebem no_show.
    """
    print("[SCHEDULER] auto_concluido: iniciando", flush=True)
    try:
        agora = datetime.now(SP_TZ).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.execute(
            "UPDATE agendamentos SET status = 'concluido'"
            " WHERE data_hora < ? AND status = 'agendado'",
            (agora,),
        )
        conn.commit()
        n = cur.rowcount
        conn.close()
        if n:
            print(f"[SCHEDULER] auto_concluido: {n} agendamento(s) → concluido", flush=True)
    except Exception as e:
        print(f"[ERRO] auto_concluido_job: {e}", flush=True)


async def enviar_lembretes_job():
    """Envia lembrete de confirmação para clientes com agendamento próximo.

    Roda a cada 30 min. Critérios para envio:
    - status = 'agendado' e lembrete_enviado_em IS NULL
    - agendamento dentro das próximas N horas (configurável)
    - agendamento foi feito com pelo menos N horas de antecedência (evita lembrete pós-booking imediato)
    """
    antecedencia = int(_jobs_cfg.get("confirmacao_antecedencia_horas", 3))
    print(f"[SCHEDULER] lembretes: buscando agendamentos em até {antecedencia}h", flush=True)
    try:
        agora = datetime.now(SP_TZ)
        agora_str = agora.strftime("%Y-%m-%d %H:%M:%S")
        limite_str = (agora + timedelta(hours=antecedencia)).strftime("%Y-%m-%d %H:%M:%S")

        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
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
        conn.close()

        enviados = 0
        for row in rows:
            # Pula se o agendamento foi feito com menos de N horas de antecedência
            try:
                criado = datetime.fromisoformat(row["criado_em"]).replace(tzinfo=None)
                data_hora = datetime.strptime(row["data_hora"][:16], "%Y-%m-%d %H:%M")
                if (data_hora - criado) < timedelta(hours=antecedencia):
                    continue
            except Exception:
                pass

            hora = row["data_hora"][11:16]
            mensagem = (
                f"Oi {row['nome']}! Lembrete: seu horário é hoje às *{hora}*. "
                f"Confirma sua presença? Responda *SIM* ou *NÃO PODEREI IR* ✂️"
            )
            await send_whatsapp_message(row["telefone"], mensagem)

            # Marca lembrete como enviado (idempotência)
            conn2 = sqlite3.connect(str(DB_PATH))
            conn2.execute(
                "UPDATE agendamentos SET lembrete_enviado_em = ? WHERE id = ?",
                (agora.isoformat(), row["id"]),
            )
            conn2.commit()
            conn2.close()
            enviados += 1

        if enviados:
            print(f"[SCHEDULER] lembretes: {enviados} lembrete(s) enviado(s)", flush=True)
    except Exception as e:
        print(f"[ERRO] enviar_lembretes_job: {e}", flush=True)


async def resumo_dia_job():
    """Envia ao barbeiro o resumo de atendimentos do dia para detecção de no-show.

    Roda no horário de fechamento + 30 min (configurável em barbershops.yaml).
    Idempotente: registra envio no audit_log, nunca envia duas vezes no mesmo dia.
    Silêncio do barbeiro → auto_concluido_job marca os restantes como concluido.
    """
    print("[SCHEDULER] resumo_dia: iniciando", flush=True)
    if not BARBEIRO_PHONE:
        print("[SCHEDULER] resumo_dia: BARBEIRO_PHONE não configurado, pulando", flush=True)
        return
    try:
        hoje = datetime.now(SP_TZ).strftime("%Y-%m-%d")

        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

        # Idempotência: verifica se já enviou hoje
        ja_enviado = conn.execute(
            "SELECT id FROM audit_log WHERE entidade='job' AND acao='resumo_dia' AND entidade_id=?",
            (hoje,),
        ).fetchone()
        if ja_enviado:
            print("[SCHEDULER] resumo_dia: já enviado hoje, pulando", flush=True)
            conn.close()
            return

        rows = conn.execute(
            """
            SELECT strftime('%H:%M', a.data_hora) AS hora, c.nome, a.status
            FROM agendamentos a
            JOIN clientes c ON a.cliente_id = c.id
            WHERE DATE(a.data_hora) = ? AND a.status NOT IN ('cancelado')
            ORDER BY a.data_hora
            """,
            (hoje,),
        ).fetchall()
        conn.close()

        if not rows:
            print("[SCHEDULER] resumo_dia: nenhum agendamento hoje, pulando", flush=True)
            return

        lista = ", ".join(f"{r['nome']} {r['hora']}" for r in rows)
        mensagem = (
            f"Fechamento de hoje ({hoje}):\n{lista}\n\n"
            f"Algum faltou? Responda o(s) nome(s) ou *NENHUM* ✂️"
        )
        await send_whatsapp_message(BARBEIRO_PHONE, mensagem)

        # Registra envio para idempotência
        conn2 = sqlite3.connect(str(DB_PATH))
        conn2.execute(
            "INSERT INTO audit_log (id, entidade, entidade_id, acao, detalhes, criado_em) VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), "job", hoje, "resumo_dia",
             json.dumps({"total": len(rows)}, ensure_ascii=False),
             datetime.now(SP_TZ).isoformat()),
        )
        conn2.commit()
        conn2.close()
        print(f"[SCHEDULER] resumo_dia: enviado ({len(rows)} agendamentos)", flush=True)
    except Exception as e:
        print(f"[ERRO] resumo_dia_job: {e}", flush=True)


async def relatorio_mensal_job():
    """Envia relatório do mês anterior ao barbeiro no 1º de cada mês às 9h."""
    print("[SCHEDULER] relatorio_mensal: iniciando", flush=True)
    if not BARBEIRO_PHONE:
        print("[SCHEDULER] relatorio_mensal: BARBEIRO_PHONE não configurado, pulando", flush=True)
        return
    try:
        agora = datetime.now(SP_TZ)
        ano_ref = agora.year if agora.month > 1 else agora.year - 1
        mes_ref = agora.month - 1 if agora.month > 1 else 12
        chave = f"{ano_ref:04d}-{mes_ref:02d}"

        conn = sqlite3.connect(str(DB_PATH))
        ja_enviado = conn.execute(
            "SELECT id FROM audit_log WHERE entidade='job' AND acao='relatorio_mensal' AND entidade_id=?",
            (chave,),
        ).fetchone()
        conn.close()
        if ja_enviado:
            print(f"[SCHEDULER] relatorio_mensal: já enviado para {chave}, pulando", flush=True)
            return

        raw = await call_mcp_tool("gerar_relatorio_mensal", {"ano": ano_ref, "mes": mes_ref})
        data = json.loads(raw)
        texto = data.get("relatorio", "")
        if texto:
            await send_whatsapp_message(BARBEIRO_PHONE, texto)

        conn2 = sqlite3.connect(str(DB_PATH))
        conn2.execute(
            "INSERT INTO audit_log (id, entidade, entidade_id, acao, detalhes, criado_em) VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), "job", chave, "relatorio_mensal",
             json.dumps({"ano": ano_ref, "mes": mes_ref}),
             datetime.now(SP_TZ).isoformat()),
        )
        conn2.commit()
        conn2.close()
        print(f"[SCHEDULER] relatorio_mensal: enviado para {chave}", flush=True)
    except Exception as e:
        print(f"[ERRO] relatorio_mensal_job: {e}", flush=True)


async def enviar_recuperacao_clientes(dias: int = None):
    if dias is None:
        dias = DIAS_INATIVIDADE
    print(f"[SCHEDULER] Iniciando recuperacao de clientes inativos (dias={dias})", flush=True)
    try:
        raw = await call_mcp_tool("listar_clientes_inativos", {"dias": dias, "limite": 100})
        data = json.loads(raw)
        clientes = data.get("clientes", [])
        print(f"[SCHEDULER] {len(clientes)} clientes inativos encontrados", flush=True)
        agora = datetime.now().isoformat()
        enviados = 0
        for c in clientes:
            telefone = c.get("telefone", "")
            nome = c.get("nome", "")
            if not telefone:
                continue
            mensagem = (
                f"Oi {nome}! Faz um tempinho que voce nao aparece por aqui. "
                f"Temos novidades na barbearia e queremos te ver! "
                f"Que tal agendar um horario? E so mandar uma mensagem. Te esperamos! ✂️"
            )
            await send_whatsapp_message(telefone, mensagem)
            try:
                conn_rec = sqlite3.connect(str(DB_PATH))
                conn_rec.execute(
                    "UPDATE clientes SET ultima_recuperacao_em = ? WHERE id = ?",
                    (agora, c["id"])
                )
                conn_rec.commit()
                conn_rec.close()
            except Exception as db_e:
                print(f"[WARN] Falha ao atualizar ultima_recuperacao_em: {db_e}", flush=True)
            enviados += 1
        print(f"[SCHEDULER] Recuperacao concluida: {enviados} mensagens enviadas", flush=True)
    except Exception as e:
        print(f"[ERRO] enviar_recuperacao_clientes: {e}", flush=True)


@asynccontextmanager
async def lifespan(app_: FastAPI):
    scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")
    scheduler.add_job(
        enviar_recuperacao_clientes,
        CronTrigger(hour=HORARIO_RECUPERACAO, minute=0, timezone="America/Sao_Paulo"),
        id="recuperacao_clientes",
        replace_existing=True,
    )
    scheduler.add_job(
        auto_concluido_job,
        CronTrigger(minute=5, timezone="America/Sao_Paulo"),  # toda hora aos :05
        id="auto_concluido",
        replace_existing=True,
    )
    scheduler.add_job(
        enviar_lembretes_job,
        CronTrigger(minute="0,30", timezone="America/Sao_Paulo"),  # a cada 30 min
        id="lembretes_confirmacao",
        replace_existing=True,
    )
    _horario_resumo = _jobs_cfg.get("horario_resumo_dia", "19:30")
    try:
        _h_resumo, _m_resumo = map(int, _horario_resumo.split(":"))
    except Exception:
        _h_resumo, _m_resumo = 19, 30
    scheduler.add_job(
        resumo_dia_job,
        CronTrigger(hour=_h_resumo, minute=_m_resumo, timezone="America/Sao_Paulo"),
        id="resumo_dia",
        replace_existing=True,
    )
    scheduler.add_job(
        relatorio_mensal_job,
        CronTrigger(day=1, hour=9, minute=0, timezone="America/Sao_Paulo"),
        id="relatorio_mensal",
        replace_existing=True,
    )
    scheduler.start()
    print(f"[SCHEDULER] Agendado: recuperacao diaria as {HORARIO_RECUPERACAO}:00 SP", flush=True)
    print("[SCHEDULER] Agendado: auto_concluido a cada hora (:05)", flush=True)
    print("[SCHEDULER] Agendado: lembretes_confirmacao a cada 30 min (:00/:30)", flush=True)
    print(f"[SCHEDULER] Agendado: resumo_dia as {_horario_resumo} SP", flush=True)
    print("[SCHEDULER] Agendado: relatorio_mensal no dia 1 de cada mes as 09:00 SP", flush=True)
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)

_mcp_session_id: str | None = None

async def _mcp_ensure_session() -> str | None:
    global _mcp_session_id
    if _mcp_session_id:
        return _mcp_session_id
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    payload = {"jsonrpc":"2.0","id":0,"method":"initialize","params":{
        "protocolVersion":"2024-11-05","capabilities":{},
        "clientInfo":{"name":"barbershop-gateway","version":"1.0"}
    }}
    async with httpx.AsyncClient(timeout=15.0) as http:
        r = await http.post(MCP_SERVER_URL, json=payload, headers=headers)
        _mcp_session_id = r.headers.get("mcp-session-id")
        print(f"[DEBUG] MCP init status={r.status_code} session={_mcp_session_id}", flush=True)
    return _mcp_session_id

async def call_mcp_tool(tool_name: str, arguments: dict) -> str:
    global _mcp_session_id
    try:
        session_id = await _mcp_ensure_session()
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        payload = {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":tool_name,"arguments":{"params":arguments}}}
        async with httpx.AsyncClient(timeout=30.0) as http:
            async with http.stream("POST", MCP_SERVER_URL, json=payload, headers=headers) as r:
                print(f"[DEBUG] MCP {tool_name} status={r.status_code}", flush=True)
                if r.status_code in (400, 404):
                    _mcp_session_id = None
                    body = await r.aread()
                    return json.dumps({"erro": f"MCP {r.status_code}: {body[:200]}"})
                ct = r.headers.get("content-type", "")
                if "text/event-stream" in ct:
                    async for line in r.aiter_lines():
                        if line.startswith("data: "):
                            try:
                                data = json.loads(line[6:])
                                if "result" in data:
                                    content = data["result"].get("content", [])
                                    if content:
                                        return content[0].get("text", "{}")
                            except Exception:
                                pass
                    return json.dumps({"erro": "Sem resultado no SSE"})
                else:
                    body = await r.aread()
                    result = json.loads(body)
                    if "result" in result:
                        content = result["result"].get("content", [])
                        if content:
                            return content[0].get("text", "{}")
                    return json.dumps({"erro": "Sem resposta"})
    except Exception as e:
        print(f"[ERRO] call_mcp_tool {tool_name}: {e}", flush=True)
        return json.dumps({"erro": str(e)})

async def send_whatsapp_message(numero: str, texto: str):
    url = f"{EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    payload = {"number": numero, "textMessage": {"text": texto}, "delay": 1200}
    print(f"[DEBUG] Enviando WhatsApp para {numero} url={url}", flush=True)
    async with httpx.AsyncClient(timeout=15.0) as http:
        try:
            r = await http.post(url, json=payload, headers=headers)
            print(f"[DEBUG] WhatsApp status={r.status_code} body={r.text[:300]}", flush=True)
        except Exception as e:
            print(f"[ERRO] WhatsApp: {e}", flush=True)

async def processar_mensagem(telefone: str, texto: str, send_to: str = None):
    print(f"[DEBUG] Iniciando processamento para {telefone}: {texto}", flush=True)
    is_barbeiro = bool(BARBEIRO_PHONE and telefone == BARBEIRO_PHONE)
    try:
        historico_raw = await call_mcp_tool("buscar_sessao", {"telefone": telefone, "limite": 20})
        historico = json.loads(historico_raw).get("historico", [])
        await call_mcp_tool("salvar_mensagem_sessao", {"telefone": telefone, "mensagem": texto, "papel": "user"})

        from datetime import date
        hoje_fmt = date.today().strftime("%d/%m/%Y")
        hoje_iso = date.today().strftime("%Y-%m-%d")

        if is_barbeiro:
            print(f"[DEBUG] Modo BARBEIRO ativado", flush=True)
            prompt = ADMIN_SYSTEM_PROMPT.format(hoje=hoje_fmt)
            tools_list = ADMIN_TOOLS
            messages = [{"role": "system", "content": prompt}]
            for h in historico:
                role = "user" if h["papel"] == "user" else "assistant"
                messages.append({"role": role, "content": h["mensagem"]})
            messages.append({"role": "user", "content": texto})
        else:
            # Identifica o cliente antes do loop — o LLM já recebe o contexto pronto
            cliente_raw = await call_mcp_tool("identificar_cliente", {"telefone": telefone})
            cliente_info = json.loads(cliente_raw)
            if cliente_info.get("status") == "novo":
                contexto_cliente = f"[CONTEXTO: cliente NOVO, telefone={telefone}. Pergunte o nome antes de qualquer acao]"
            else:
                c = cliente_info.get("cliente", {})
                contexto_cliente = (
                    f"[CONTEXTO: cliente identificado — nome={c.get('nome')}, "
                    f"cliente_id={c.get('id')}, telefone={telefone}. "
                    f"Use este cliente_id em todas as ferramentas que precisarem dele]"
                )
                try:
                    hist_raw = await call_mcp_tool("historico_cliente", {"cliente_id": c.get("id"), "limite": 5})
                    hist_data = json.loads(hist_raw)
                    agendamentos = hist_data.get("agendamentos", [])
                    futuros = [a for a in agendamentos if a.get("status") in ("agendado", "confirmado")]
                    if futuros:
                        ags_str = "; ".join(
                            f"id={a['id']} data={a['data_hora']} servico={a['servico']} status={a['status']}"
                            for a in futuros
                        )
                        contexto_cliente += f". Agendamentos ativos: [{ags_str}]"
                    # Detecta se há agendamento aguardando confirmação de lembrete
                    aguardando_conf = [a for a in agendamentos if a.get("status") == "agendado" and a.get("lembrete_enviado_em")]
                    if aguardando_conf:
                        pc = aguardando_conf[0]
                        contexto_cliente += (
                            f". AGUARDANDO CONFIRMACAO: id={pc['id']} em {pc['data_hora']} ({pc['servico']})"
                            " — lembrete enviado, cliente pode estar respondendo agora"
                        )
                except Exception as e:
                    print(f"[WARN] Pre-fetch historico_cliente falhou: {e}", flush=True)
            print(f"[DEBUG] {contexto_cliente}", flush=True)

            prompt = SYSTEM_PROMPT.format(hoje=hoje_fmt)
            tools_list = LLM_TOOLS
            messages = [{"role": "system", "content": prompt}]
            for h in historico:
                role = "user" if h["papel"] == "user" else "assistant"
                messages.append({"role": role, "content": h["mensagem"]})
            msg_com_contexto = f"{contexto_cliente}\nMensagem do cliente: {texto}"
            messages.append({"role": "user", "content": msg_com_contexto})

        resposta_final = ""

        for i in range(10):
            print(f"[DEBUG] Chamando DeepSeek iter={i}", flush=True)
            for tentativa in range(3):
                try:
                    response = await deepseek_client.chat.completions.create(
                        model=MODEL,
                        messages=messages,
                        tools=tools_list,
                        tool_choice="required" if i == 0 else "auto",
                        parallel_tool_calls=False,
                    )
                    break
                except Exception as e:
                    if "tool_use_failed" in str(e) and tentativa < 2:
                        print(f"[WARN] DeepSeek tool_use_failed, tentativa {tentativa+1}/3", flush=True)
                        continue
                    raise
            print(f"[DEBUG] DeepSeek respondeu iter={i}", flush=True)

            choice = response.choices[0]
            msg = choice.message
            finish_reason = choice.finish_reason
            print(f"[DEBUG] finish_reason={finish_reason} tool_calls={bool(msg.tool_calls)} content={repr(msg.content)[:80]}", flush=True)

            if msg.tool_calls:
                assistant_msg = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in msg.tool_calls
                    ]
                }
            else:
                assistant_msg = {"role": "assistant", "content": msg.content or ""}
            messages.append(assistant_msg)

            if not msg.tool_calls:
                resposta_final = msg.content or ""
                break

            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                print(f"[DEBUG] Tool call: {tc.function.name}({args})", flush=True)
                if tc.function.name == "recuperar_clientes":
                    dias_rec = int(args.get("dias", DIAS_INATIVIDADE))
                    asyncio.create_task(enviar_recuperacao_clientes(dias_rec))
                    resultado = json.dumps({
                        "status": "iniciado",
                        "mensagem": f"Recuperacao de clientes inativos ha mais de {dias_rec} dias iniciada em segundo plano."
                    }, ensure_ascii=False)
                elif tc.function.name == "responder_confirmacao":
                    resultado = await call_mcp_tool(tc.function.name, args)
                    # Notifica o barbeiro quando um slot vaga por recusa do cliente
                    try:
                        data = json.loads(resultado)
                        if data.get("notificar_barbeiro") and BARBEIRO_PHONE:
                            slot = data.get("data_hora", "")[:16]
                            servico = data.get("servico", "")
                            await send_whatsapp_message(
                                BARBEIRO_PHONE,
                                f"⚠️ Horário {slot} ({servico}) vagou — cliente confirmou que não vai. Slot disponível!",
                            )
                    except Exception:
                        pass
                elif tc.function.name == "marcar_no_show":
                    # Se barbeiro passou só a hora (ex: "faltou 14:00"), monta data completa
                    if "data_hora" in args and len(args["data_hora"]) <= 5:
                        from datetime import date
                        args["data_hora"] = f"{date.today().strftime('%Y-%m-%d')} {args['data_hora']}"
                    resultado = await call_mcp_tool(tc.function.name, args)
                else:
                    resultado = await call_mcp_tool(tc.function.name, args)
                print(f"[DEBUG] Tool result: {resultado[:100]}", flush=True)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": resultado})

        if not resposta_final:
            resposta_final = "Desculpa, tive um problema. Pode repetir?"

        await call_mcp_tool("salvar_mensagem_sessao", {"telefone": telefone, "mensagem": resposta_final, "papel": "assistant"})
        if send_to:
            await send_whatsapp_message(send_to, resposta_final)
            print(f"[OK] Resposta enviada para {send_to}: {resposta_final[:80]}", flush=True)
        else:
            print(f"[WARN] Resposta gerada mas sem destino de envio para {telefone}", flush=True)

    except Exception as e:
        msg_erro = str(e)
        print(f"[ERRO] processar_mensagem: {msg_erro}", flush=True)
        if send_to:
            if "429" in msg_erro or "rate_limit" in msg_erro:
                await send_whatsapp_message(send_to, "Estou sobrecarregado agora, tenta em alguns minutinhos! ⏳")
            else:
                await send_whatsapp_message(send_to, "Desculpa, tive um problema tecnico. Tenta de novo!")

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    if WEBHOOK_SECRET and request.headers.get("apikey") != WEBHOOK_SECRET:
        return JSONResponse({"ok": False}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": True})
    event = body.get("event", "")
    data = body.get("data", {})
    if event != "messages.upsert":
        return JSONResponse({"ok": True})
    message = data.get("message", {})
    key = data.get("key", {})
    if key.get("fromMe", False):
        return JSONResponse({"ok": True})
    remote_jid = key.get("remoteJid", "")
    send_to = None
    if remote_jid.endswith("@lid"):
        lid = remote_jid.replace("@lid", "")
        if lid in LID_MAP:
            telefone = LID_MAP[lid]
            send_to = telefone
        else:
            # LID desconhecido: tenta resolver via Evolution API
            telefone_real = await _resolver_lid_via_api(lid)
            if telefone_real:
                telefone = telefone_real
                send_to = telefone_real
                print(f"[INFO] LID {lid} resolvido automaticamente para {telefone_real}", flush=True)
            else:
                # LID não resolvido: usa o JID @lid diretamente — Evolution API aceita para envio
                telefone = lid
                send_to = remote_jid
                print(f"[INFO] LID {lid} não resolvido via API, enviando resposta direto para {remote_jid}", flush=True)
            LID_MAP[lid] = telefone
            _save_lid_map(LID_MAP)
    elif remote_jid.endswith("@s.whatsapp.net"):
        telefone = remote_jid.replace("@s.whatsapp.net", "")
        send_to = telefone
    else:
        return JSONResponse({"ok": True})

    texto = (message.get("conversation") or message.get("extendedTextMessage", {}).get("text") or "").strip()
    if not telefone or not texto:
        return JSONResponse({"ok": True})

    # Dedup por message ID — ignora se já processamos nos últimos 60s
    msg_id = key.get("id", "")
    now = time.time()
    if msg_id and _processed.get(msg_id, 0) > now - 60:
        return JSONResponse({"ok": True})
    if msg_id:
        _processed[msg_id] = now
        # Limpa entradas antigas para não crescer indefinidamente
        for k in [k for k, v in _processed.items() if v < now - 300]:
            del _processed[k]

    background_tasks.add_task(processar_mensagem, telefone, texto, send_to)
    return JSONResponse({"ok": True})

@app.get("/health")
async def health():
    return {"status": "ok", "engine": MODEL}

@app.post("/test-message")
async def test_message(telefone: str, texto: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(processar_mensagem, telefone, texto)
    return {"ok": True}


# ─── Página de Setup (/setup) ─────────────────────────────────────────────────

_SETUP_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Conectar WhatsApp ✂️</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f0f2f5;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:#fff;border-radius:20px;padding:36px 32px;max-width:420px;width:100%;text-align:center;box-shadow:0 2px 24px rgba(0,0,0,.10)}
.logo{font-size:40px;margin-bottom:8px}
h1{font-size:20px;font-weight:700;color:#111;margin-bottom:4px}
.subtitle{font-size:13px;color:#888;margin-bottom:22px}
.status{display:inline-flex;align-items:center;gap:7px;padding:5px 16px;border-radius:20px;font-size:13px;font-weight:600;margin-bottom:22px;transition:all .3s}
.status .dot{width:8px;height:8px;border-radius:50%;background:currentColor}
.s-wait{background:#fff8e1;color:#f59f00}
.s-conn{background:#e8f4fd;color:#1c7ed6}
.s-ok  {background:#ebfbee;color:#2f9e44}
.s-err {background:#fff5f5;color:#e03131}
#qr-wrap{display:inline-block;border:2px solid #eee;border-radius:14px;padding:14px;margin-bottom:14px;background:#fff}
#qrcode canvas,#qrcode img{display:block}
.spinner{width:48px;height:48px;border:4px solid #e9ecef;border-top-color:#25d366;border-radius:50%;animation:spin .75s linear infinite;margin:18px auto}
@keyframes spin{to{transform:rotate(360deg)}}
.timer{font-size:12px;color:#aaa;margin-bottom:16px}
.timer b{color:#555}
.steps{background:#f8f9fa;border-radius:10px;padding:14px 16px;text-align:left;font-size:13px;color:#555;line-height:1.9;margin-bottom:18px}
.steps ol{padding-left:18px}
.steps li strong{color:#222}
#connected-section{display:none}
.ok-icon{font-size:52px;margin-bottom:10px}
.ok-title{font-size:18px;font-weight:700;color:#2f9e44;margin-bottom:6px}
.ok-sub{font-size:13px;color:#777;margin-bottom:24px}
.btn{border:none;border-radius:10px;padding:10px 22px;font-size:13px;font-weight:600;cursor:pointer;transition:opacity .2s,transform .1s}
.btn:active{transform:scale(.97)}
.btn:disabled{opacity:.45;cursor:default}
.btn:hover:not(:disabled){opacity:.85}
.btn-ghost{background:#f1f3f5;color:#444}
</style>
</head>
<body>
<div class="card">
  <div class="logo">✂️</div>
  <h1>Conectar WhatsApp</h1>
  <p class="subtitle">Vincule o celular da barbearia ao bot</p>

  <div id="status-badge" class="status s-wait">
    <div class="dot"></div>
    <span id="status-text">Carregando...</span>
  </div>

  <div id="qr-section">
    <div id="spinner" class="spinner"></div>
    <div id="qr-wrap" style="display:none">
      <div id="qrcode"></div>
    </div>
    <div class="timer" id="timer-row" style="display:none">
      QR expira em <b id="countdown">14</b>s
    </div>
    <div class="steps">
      <ol>
        <li>Abra o <strong>WhatsApp</strong> no celular</li>
        <li>Toque em <strong>⋮ &rarr; Dispositivos vinculados</strong></li>
        <li>Toque em <strong>&ldquo;Vincular dispositivo&rdquo;</strong></li>
        <li>Aponte a câmera para o QR code acima</li>
      </ol>
    </div>
    <button class="btn btn-ghost" id="btn-refresh" onclick="fetchQR()" disabled>
      🔄 Gerar novo QR
    </button>
  </div>

  <div id="connected-section">
    <div class="ok-icon">✅</div>
    <div class="ok-title">WhatsApp conectado!</div>
    <div class="ok-sub">O bot está ativo e pronto para receber mensagens.</div>
    <button class="btn btn-ghost" onclick="reconnect()">🔄 Conectar outro número</button>
  </div>
</div>

<script>
let timerInt=null, pollInt=null, secs=14, connected=false;

function setStatus(state){
  const b=document.getElementById('status-badge'), t=document.getElementById('status-text');
  const cls={open:'s-ok',connecting:'s-conn',error:'s-err'};
  b.className='status '+(cls[state]||'s-wait');
  t.textContent={open:'Conectado',connecting:'Conectando...',error:'Erro — verifique os containers',close:'Aguardando leitura'}[state]||'Aguardando leitura';
}

async function pollStatus(){
  try{
    const d=await(await fetch('/setup/status')).json();
    setStatus(d.state);
    if(d.state==='open'&&!connected){
      connected=true; clearInterval(timerInt);
      document.getElementById('qr-section').style.display='none';
      document.getElementById('connected-section').style.display='block';
    } else if(d.state!=='open'&&connected){
      connected=false;
      document.getElementById('connected-section').style.display='none';
      document.getElementById('qr-section').style.display='block';
      fetchQR();
    }
  }catch(e){setStatus('error');}
}

async function fetchQR(){
  clearInterval(timerInt);
  const sp=document.getElementById('spinner'), wrap=document.getElementById('qr-wrap');
  const tr=document.getElementById('timer-row'), btn=document.getElementById('btn-refresh');
  sp.style.display='block'; wrap.style.display='none'; tr.style.display='none'; btn.disabled=true;
  try{
    const d=await(await fetch('/setup/qr')).json();
    if(d.code){
      document.getElementById('qrcode').innerHTML='';
      new QRCode(document.getElementById('qrcode'),{text:d.code,width:256,height:256,colorDark:'#000',colorLight:'#fff',correctLevel:QRCode.CorrectLevel.L});
      sp.style.display='none'; wrap.style.display='inline-block'; tr.style.display='block'; btn.disabled=false;
      startTimer();
    }
  }catch(e){setStatus('error'); sp.style.display='none'; btn.disabled=false;}
}

function startTimer(){
  secs=14; document.getElementById('countdown').textContent=secs;
  timerInt=setInterval(()=>{
    secs--; document.getElementById('countdown').textContent=secs;
    if(secs<=0){clearInterval(timerInt); fetchQR();}
  },1000);
}

async function reconnect(){
  connected=false; setStatus('connecting');
  document.getElementById('connected-section').style.display='none';
  document.getElementById('qr-section').style.display='block';
  try{await fetch('/setup/reconnect',{method:'POST'});}catch(e){}
  fetchQR();
}

async function init(){
  await pollStatus();
  if(!connected) fetchQR();
  pollInt=setInterval(pollStatus,4000);
}

init();
</script>
</body>
</html>"""


@app.get("/setup", response_class=HTMLResponse, include_in_schema=False)
async def setup_page():
    return _SETUP_HTML


@app.get("/setup/status", include_in_schema=False)
async def setup_status():
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            r = await http.get(
                f"{EVOLUTION_API_URL}/instance/connectionState/{EVOLUTION_INSTANCE}",
                headers={"apikey": EVOLUTION_API_KEY},
            )
            data = r.json()
            state = data.get("instance", {}).get("state", "close")
            return {"state": state}
    except Exception as e:
        return {"state": "error", "detail": str(e)}


@app.get("/setup/qr", include_in_schema=False)
async def setup_qr():
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            r = await http.get(
                f"{EVOLUTION_API_URL}/instance/connect/{EVOLUTION_INSTANCE}",
                headers={"apikey": EVOLUTION_API_KEY},
            )
            data = r.json()
            return {"code": data.get("code", "")}
    except Exception as e:
        return {"code": "", "error": str(e)}


@app.post("/setup/reconnect", include_in_schema=False)
async def setup_reconnect():
    async with httpx.AsyncClient(timeout=10.0) as http:
        try:
            await http.delete(
                f"{EVOLUTION_API_URL}/instance/logout/{EVOLUTION_INSTANCE}",
                headers={"apikey": EVOLUTION_API_KEY},
            )
        except Exception:
            pass
    return {"ok": True}
