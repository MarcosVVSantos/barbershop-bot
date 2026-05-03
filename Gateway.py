import os, json, time, httpx
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from groq import AsyncGroq

load_dotenv()

GROQ_API_KEY       = os.getenv("GROQ_API_KEY", "")
EVOLUTION_API_URL  = os.getenv("EVOLUTION_API_URL", "http://localhost:8080")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "barbershop")
MCP_SERVER_URL     = os.getenv("MCP_SERVER_URL", "http://localhost:8001/mcp")

groq_client = AsyncGroq(api_key=GROQ_API_KEY)
MODEL = "llama-3.3-70b-versatile"

LID_MAP = {
    "12481846079597": "5511977957833",
}

# Dedup: armazena msg_id -> timestamp para evitar processar a mesma mensagem duas vezes
_processed: dict[str, float] = {}

SYSTEM_PROMPT = """Voce eh o assistente virtual da barbearia, simpatico e descontraido em portugues brasileiro.
REGRAS:
1. Sempre use identificar_cliente no inicio com o telefone do cliente
2. Se cliente novo, peca o nome
3. Salve preferencias mencionadas
4. Ao confirmar agendamento, ofereca produtos
5. Confirme sempre data, hora e servico antes de agendar
6. Se horario ocupado, sugira proximos disponiveis
7. Use emojis moderadamente: corte barba agenda produtos
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
    {"name":"historico_cliente","description":"Historico do cliente.","parameters":{"type":"object","properties":{"cliente_id":{"type":"string"},"limite":{"type":"integer"}},"required":["cliente_id"]}},
    {"name":"buscar_sessao","description":"Historico de conversa.","parameters":{"type":"object","properties":{"telefone":{"type":"string"},"limite":{"type":"integer"}},"required":["telefone"]}},
    {"name":"salvar_mensagem_sessao","description":"Salva mensagem na sessao.","parameters":{"type":"object","properties":{"telefone":{"type":"string"},"mensagem":{"type":"string"},"papel":{"type":"string"}},"required":["telefone","mensagem","papel"]}}
]

GROQ_TOOLS = [
    {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
    for t in TOOLS
]

app = FastAPI()

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

async def send_whatsapp_message(telefone: str, texto: str):
    url = f"{EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    payload = {"number": telefone, "textMessage": {"text": texto}, "delay": 1200}
    print(f"[DEBUG] Enviando WhatsApp para {telefone} url={url}", flush=True)
    async with httpx.AsyncClient(timeout=15.0) as http:
        try:
            r = await http.post(url, json=payload, headers=headers)
            print(f"[DEBUG] WhatsApp status={r.status_code} body={r.text[:300]}", flush=True)
        except Exception as e:
            print(f"[ERRO] WhatsApp: {e}", flush=True)

async def processar_mensagem(telefone: str, texto: str):
    print(f"[DEBUG] Iniciando processamento para {telefone}: {texto}", flush=True)
    try:
        historico_raw = await call_mcp_tool("buscar_sessao", {"telefone": telefone, "limite": 20})
        historico = json.loads(historico_raw).get("historico", [])
        await call_mcp_tool("salvar_mensagem_sessao", {"telefone": telefone, "mensagem": texto, "papel": "user"})

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for h in historico:
            role = "user" if h["papel"] == "user" else "assistant"
            messages.append({"role": role, "content": h["mensagem"]})

        primeira = f"ATENÇÃO: O telefone do cliente é {telefone}. Use EXATAMENTE esse número ao chamar identificar_cliente. Mensagem do cliente: {texto}" if not historico else texto
        messages.append({"role": "user", "content": primeira})

        resposta_final = ""

        for i in range(10):
            print(f"[DEBUG] Chamando Groq iter={i}", flush=True)
            response = await groq_client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=GROQ_TOOLS,
                tool_choice="auto",
                parallel_tool_calls=False
            )
            print(f"[DEBUG] Groq respondeu iter={i}", flush=True)

            msg = response.choices[0].message

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
                resultado = await call_mcp_tool(tc.function.name, args)
                print(f"[DEBUG] Tool result: {resultado[:100]}", flush=True)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": resultado})

        if not resposta_final:
            resposta_final = "Desculpa, tive um problema. Pode repetir?"

        await call_mcp_tool("salvar_mensagem_sessao", {"telefone": telefone, "mensagem": resposta_final, "papel": "assistant"})
        await send_whatsapp_message(telefone, resposta_final)
        print(f"[OK] Resposta enviada para {telefone}: {resposta_final[:80]}", flush=True)

    except Exception as e:
        msg_erro = str(e)
        print(f"[ERRO] processar_mensagem: {msg_erro}", flush=True)
        if "429" in msg_erro or "rate_limit" in msg_erro:
            await send_whatsapp_message(telefone, "Estou sobrecarregado agora, tenta em alguns minutinhos! ⏳")
        else:
            await send_whatsapp_message(telefone, "Desculpa, tive um problema tecnico. Tenta de novo!")

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
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
    if remote_jid.endswith("@lid"):
        lid = remote_jid.replace("@lid", "")
        if lid not in LID_MAP:
            return JSONResponse({"ok": True})
        telefone = LID_MAP[lid]
    elif remote_jid.endswith("@s.whatsapp.net"):
        telefone = remote_jid.replace("@s.whatsapp.net", "")
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

    background_tasks.add_task(processar_mensagem, telefone, texto)
    return JSONResponse({"ok": True})

@app.get("/health")
async def health():
    return {"status": "ok", "engine": MODEL}

@app.post("/test-message")
async def test_message(telefone: str, texto: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(processar_mensagem, telefone, texto)
    return {"ok": True}
