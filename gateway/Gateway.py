"""
Gateway FastAPI — recebe webhooks do WhatsApp (via Evolution API),
orquestra DeepSeek V3 com function calling para as ferramentas da barbearia.
"""

import os
import json
import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

DEEPSEEK_API_KEY    = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL   = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
EVOLUTION_API_URL   = os.getenv("EVOLUTION_API_URL", "http://localhost:8080")
EVOLUTION_API_KEY   = os.getenv("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE  = os.getenv("EVOLUTION_INSTANCE", "barbershop")
MCP_SERVER_URL      = os.getenv("MCP_SERVER_URL", "http://localhost:8001/mcp")

deepseek_client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL,
)
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

SYSTEM_PROMPT = """Voce eh o assistente virtual da barbearia, um atendente simpatico,
profissional e descontraido que fala portugues brasileiro naturalmente.
REGRAS:
1. Sempre identifique o cliente pelo telefone no inicio usando identificar_cliente
2. Se for cliente novo, peca o nome de forma simpatica
3. Salve preferencias quando o cliente mencionar o que gosta
4. Ao confirmar agendamento, ofereca produtos relacionados
5. Use linguagem informal mas profissional
6. Sempre confirme dados antes de agendar (data, hora, servico)
7. Em caso de horario ocupado, sugira os proximos disponiveis
8. Use emojis moderadamente
"""

TOOLS = [
    {"name": "identificar_cliente", "description": "Identifica ou cadastra cliente pelo WhatsApp. Chame sempre no inicio.", "parameters": {"type": "object", "properties": {"telefone": {"type": "string"}, "nome": {"type": "string"}}, "required": ["telefone"]}},
    {"name": "listar_horarios_disponiveis", "description": "Retorna horarios livres em uma data.", "parameters": {"type": "object", "properties": {"data": {"type": "string"}}, "required": ["data"]}},
    {"name": "agendar_corte", "description": "Cria agendamento para o cliente.", "parameters": {"type": "object", "properties": {"cliente_id": {"type": "string"}, "data": {"type": "string"}, "hora": {"type": "string"}, "servico": {"type": "string"}, "observacoes": {"type": "string"}}, "required": ["cliente_id", "data", "hora", "servico"]}},
    {"name": "cancelar_agendamento", "description": "Cancela agendamento.", "parameters": {"type": "object", "properties": {"agendamento_id": {"type": "string"}, "motivo": {"type": "string"}}, "required": ["agendamento_id"]}},
    {"name": "atualizar_preferencias_cliente", "description": "Salva preferencias do cliente.", "parameters": {"type": "object", "properties": {"cliente_id": {"type": "string"}, "tipo": {"type": "string"}, "valor": {"type": "string"}}, "required": ["cliente_id", "tipo", "valor"]}},
    {"name": "sugerir_corte", "description": "Sugere cortes pelo perfil do cliente.", "parameters": {"type": "object", "properties": {"descricao": {"type": "string"}, "cliente_id": {"type": "string"}}, "required": ["descricao"]}},
    {"name": "listar_produtos", "description": "Lista produtos da barbearia.", "parameters": {"type": "object", "properties": {"categoria": {"type": "string"}, "apenas_disponiveis": {"type": "boolean"}}}},
    {"name": "fazer_pedido_produto", "description": "Registra pedido de produto.", "parameters": {"type": "object", "properties": {"cliente_id": {"type": "string"}, "produto_id": {"type": "string"}, "quantidade": {"type": "integer"}, "retirada": {"type": "string"}, "agendamento_id": {"type": "string"}}, "required": ["cliente_id", "produto_id"]}},
    {"name": "historico_cliente", "description": "Historico completo do cliente.", "parameters": {"type": "object", "properties": {"cliente_id": {"type": "string"}, "limite": {"type": "integer"}}, "required": ["cliente_id"]}},
    {"name": "buscar_sessao", "description": "Busca historico de conversa.", "parameters": {"type": "object", "properties": {"telefone": {"type": "string"}, "limite": {"type": "integer"}}, "required": ["telefone"]}},
    {"name": "salvar_mensagem_sessao", "description": "Salva mensagem na sessao.", "parameters": {"type": "object", "properties": {"telefone": {"type": "string"}, "mensagem": {"type": "string"}, "papel": {"type": "string"}}, "required": ["telefone", "mensagem", "papel"]}}
]

LLM_TOOLS = [
    {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
    for t in TOOLS
]

app = FastAPI(title="Barbershop Gateway DeepSeek", version="2.0.0")

async def call_mcp_tool(tool_name: str, arguments: dict) -> str:
    async with httpx.AsyncClient(timeout=15.0) as http:
        try:
            payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments}}
            r = await http.post(MCP_SERVER_URL, json=payload)
            result = r.json()
            if "result" in result:
                content = result["result"].get("content", [])
                if content:
                    return content[0].get("text", "{}")
            return json.dumps({"erro": "Sem resposta do MCP"})
        except Exception as e:
            return json.dumps({"erro": str(e)})

async def send_whatsapp_message(telefone: str, texto: str):
    url = f"{EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    payload = {"number": telefone, "text": texto, "delay": 1200}
    async with httpx.AsyncClient(timeout=15.0) as http:
        try:
            r = await http.post(url, json=payload, headers=headers)
            r.raise_for_status()
        except Exception as e:
            print(f"[ERRO] Falha ao enviar mensagem: {e}")

async def processar_mensagem(telefone: str, texto: str):
    try:
        historico_raw = await call_mcp_tool("buscar_sessao", {"telefone": telefone, "limite": 20})
        historico = json.loads(historico_raw).get("historico", [])
        await call_mcp_tool("salvar_mensagem_sessao", {"telefone": telefone, "mensagem": texto, "papel": "user"})

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for h in historico:
            role = "user" if h["papel"] == "user" else "assistant"
            messages.append({"role": role, "content": h["mensagem"]})
        primeira_mensagem = f"[Telefone do cliente: {telefone}]\n{texto}" if not historico else texto
        messages.append({"role": "user", "content": primeira_mensagem})

        resposta_final = ""
        for _ in range(10):
            response = await deepseek_client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=LLM_TOOLS,
                tool_choice="auto",
                parallel_tool_calls=False,
            )
            choice = response.choices[0]
            msg = choice.message
            if not msg.tool_calls:
                resposta_final = msg.content or ""
                break
            assistant_msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ]
            }
            messages.append(assistant_msg)
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                resultado = await call_mcp_tool(tc.function.name, args)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": resultado})

        if not resposta_final:
            resposta_final = "Desculpa, tive um problema aqui. Pode repetir?"
        await call_mcp_tool("salvar_mensagem_sessao", {"telefone": telefone, "mensagem": resposta_final, "papel": "assistant"})
        await send_whatsapp_message(telefone, resposta_final)
    except Exception as e:
        print(f"[ERRO] processar_mensagem: {e}")
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
    telefone = key.get("remoteJid", "").replace("@s.whatsapp.net", "").replace("@g.us", "")
    texto = (message.get("conversation") or message.get("extendedTextMessage", {}).get("text") or "").strip()
    if not telefone or not texto:
        return JSONResponse({"ok": True})
    background_tasks.add_task(processar_mensagem, telefone, texto)
    return JSONResponse({"ok": True})

@app.get("/health")
async def health():
    return {"status": "ok", "engine": MODEL}

@app.post("/test-message")
async def test_message(telefone: str, texto: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(processar_mensagem, telefone, texto)
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("gateway:app", host="0.0.0.0", port=8000, reload=True)
