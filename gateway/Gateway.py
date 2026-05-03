"""
Gateway FastAPI — recebe webhooks do WhatsApp (via Evolution API),
orquestra Gemini com function calling para as ferramentas da barbearia.
"""

import os
import json
import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()

GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY", "")
EVOLUTION_API_URL   = os.getenv("EVOLUTION_API_URL", "http://localhost:8080")
EVOLUTION_API_KEY   = os.getenv("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE  = os.getenv("EVOLUTION_INSTANCE", "barbershop")
MCP_SERVER_URL      = os.getenv("MCP_SERVER_URL", "http://localhost:8001/mcp")

genai.configure(api_key=GEMINI_API_KEY)

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

app = FastAPI(title="Barbershop Gateway Gemini", version="2.0.0")

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
        history = []
        for h in historico:
            role = "user" if h["papel"] == "user" else "model"
            history.append({"role": role, "parts": [{"text": h["mensagem"]}]})
        primeira_mensagem = f"[Telefone do cliente: {telefone}]\n{texto}" if not historico else texto
        model = genai.GenerativeModel(model_name="gemini-1.5-flash", system_instruction=SYSTEM_PROMPT, tools=[{"function_declarations": TOOLS}])
        chat = model.start_chat(history=history)
        response = chat.send_message(primeira_mensagem)
        resposta_final = ""
        for _ in range(10):
            tool_calls = [p.function_call for p in response.parts if hasattr(p, "function_call") and p.function_call.name]
            if not tool_calls:
                for part in response.parts:
                    if hasattr(part, "text") and part.text:
                        resposta_final += part.text
                break
            tool_results = []
            for fc in tool_calls:
                args = dict(fc.args) if fc.args else {}
                resultado = await call_mcp_tool(fc.name, args)
                tool_results.append({"function_response": {"name": fc.name, "response": {"result": resultado}}})
            response = chat.send_message(tool_results)
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
    return {"status": "ok", "engine": "gemini-1.5-flash"}

@app.post("/test-message")
async def test_message(telefone: str, texto: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(processar_mensagem, telefone, texto)
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("gateway:app", host="0.0.0.0", port=8000, reload=True)
