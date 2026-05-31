import os, sys, json, time, httpx, asyncio, sqlite3
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from openai import AsyncOpenAI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

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
            f"{10 + i}. {r}" for i, r in enumerate(extras)
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
11. Use emojis moderadamente{regras_extras_str}
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
- "relatorio" ou "relatorio da semana" → chame relatorio_barbeiro com data_inicio=7 dias atras e data_fim=hoje (formato YYYY-MM-DD)
- "top clientes" → chame top_clientes com limite=5
- "estoque" → chame listar_produtos com apenas_disponiveis=false
- "add estoque [produto] [qtd]" → chame atualizar_estoque com modo="adicionar" e a quantidade informada
- "estoque [produto] [qtd]" (com numero) → chame atualizar_estoque com modo="definir" e a quantidade informada
- "sem estoque [produto]" → chame atualizar_estoque com modo="zerar" e quantidade=0
- "recuperar clientes" ou "recuperar clientes [N] dias" → chame recuperar_clientes com dias=N (padrao 30)

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
    {"name":"salvar_mensagem_sessao","description":"Salva mensagem na sessao.","parameters":{"type":"object","properties":{"telefone":{"type":"string"},"mensagem":{"type":"string"},"papel":{"type":"string"}},"required":["telefone","mensagem","papel"]}}
]

ADMIN_TOOLS = [
    {"type":"function","function":{"name":"agenda_hoje","description":"Lista agendamentos do dia para o barbeiro.","parameters":{"type":"object","properties":{"data":{"type":"string"}}}}},
    {"type":"function","function":{"name":"listar_pedidos_pendentes","description":"Lista pedidos de produtos pendentes/confirmados.","parameters":{"type":"object","properties":{"status":{"type":"string"},"apenas_hoje":{"type":"boolean"}}}}},
    {"type":"function","function":{"name":"relatorio_barbeiro","description":"Relatorio do periodo: agendamentos, receita e produtos.","parameters":{"type":"object","properties":{"data_inicio":{"type":"string"},"data_fim":{"type":"string"}},"required":["data_inicio","data_fim"]}}},
    {"type":"function","function":{"name":"top_clientes","description":"Top clientes por frequencia.","parameters":{"type":"object","properties":{"limite":{"type":"integer"}}}}},
    {"type":"function","function":{"name":"listar_produtos","description":"Lista produtos com estoque.","parameters":{"type":"object","properties":{"categoria":{"type":"string"},"apenas_disponiveis":{"type":"boolean"}}}}},
    {"type":"function","function":{"name":"atualizar_estoque","description":"Atualiza estoque de um produto pelo nome.","parameters":{"type":"object","properties":{"nome_produto":{"type":"string"},"quantidade":{"type":"integer"},"modo":{"type":"string","enum":["adicionar","definir","zerar"]}},"required":["nome_produto","modo"]}}},
    {"type":"function","function":{"name":"recuperar_clientes","description":"Envia mensagem de recuperacao no WhatsApp para clientes inativos ha mais de N dias.","parameters":{"type":"object","properties":{"dias":{"type":"integer","description":"Dias de inatividade (padrao 30)"}}}}},
]

LLM_TOOLS = [
    {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
    for t in TOOLS
]

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
        CronTrigger(hour=HORARIO_RECUPERACAO, minute=0),
        id="recuperacao_clientes",
        replace_existing=True,
    )
    scheduler.start()
    print(f"[SCHEDULER] Agendado: recuperacao diaria as {HORARIO_RECUPERACAO}:00 SP", flush=True)
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
                if r.status_code == 400:
                    _mcp_session_id = None
                    body = await r.aread()
                    return json.dumps({"erro": f"MCP 400: {body[:200]}"})
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
                    futuros = [a for a in agendamentos if a.get("status") == "confirmado"]
                    if futuros:
                        ags_str = "; ".join(
                            f"id={a['id']} data={a['data_hora']} servico={a['servico']}"
                            for a in futuros
                        )
                        contexto_cliente += f". Agendamentos confirmados: [{ags_str}]"
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
