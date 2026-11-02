"""
webhook.py — Integración WhatsApp via Twilio → Agente natillera

Instalar:
    pip install fastapi uvicorn twilio python-dotenv

Correr:
    uvicorn webhook:app --host 0.0.0.0 --port 8000

Luego configura en Twilio: https://tu-dominio/whatsapp/webhook (POST)
(Para desarrollo local usa ngrok: ngrok http 8000)
"""

from fastapi import FastAPI, Form, Response
from twilio.twiml.messaging_response import MessagingResponse
from agent import chat

app = FastAPI()

@app.post("/whatsapp/webhook")
async def whatsapp_webhook(
    Body: str = Form(...),       # Mensaje del usuario
    From: str = Form(...),       # Número de quien escribe (ej: whatsapp:+573001234567)
):
    """
    Twilio envía POST con 'Body' (texto) y 'From' (número).
    Usamos 'From' como thread_id para que cada número tenga su propio historial.
    """
    respuesta_agente = chat(mensaje=Body, thread_id=From)

    twiml = MessagingResponse()
    twiml.message(respuesta_agente)
    return Response(content=str(twiml), media_type="application/xml")


@app.get("/health")
def health():
    return {"status": "ok"}