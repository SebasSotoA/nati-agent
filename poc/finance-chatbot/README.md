# Natillera IA — Agente con LangGraph + Supabase

## Arquitectura

```
WhatsApp → Twilio → webhook.py → agent.py → tools.py → Supabase (PostgreSQL)
```

El LLM recibe el mensaje, razona, elige cuál tool llamar, ejecuta la función
Python, obtiene el resultado y responde en lenguaje natural.
**No hay IF/ELSE de intención, no hay ramas manuales.**

## Instalación

```bash
pip install langgraph langchain-openai psycopg2-binary fastapi uvicorn twilio python-dotenv
```

## Variables de entorno (.env)

```env
OPENAI_API_KEY=sk-...
SUPABASE_DB_URL=postgresql://postgres:PASSWORD@db.XXXX.supabase.co:5432/postgres
```

La `SUPABASE_DB_URL` la encuentras en Supabase → Settings → Database → Connection string (URI).

## Correr en consola (sin WhatsApp)

```bash
python agent.py
```

## Correr con WhatsApp (Twilio)

```bash
uvicorn webhook:app --host 0.0.0.0 --port 8000
```

Luego en Twilio Sandbox for WhatsApp configura el webhook como:
`https://TU-DOMINIO/whatsapp/webhook`

Para desarrollo local usa ngrok:
```bash
ngrok http 8000
```

## Tools disponibles

| Tool | Qué hace |
|------|----------|
| `buscar_usuario` | Busca por nombre parcial o teléfono |
| `agregar_usuario` | Registra un nuevo participante |
| `crear_prestamo` | Crea un préstamo con monto y tasa |
| `consultar_prestamos_usuario` | Lista préstamos con saldo actualizado |
| `registrar_abono` | Registra un pago a un préstamo |
| `registrar_interes_mensual` | Calcula y guarda el interés del mes |
| `registrar_boleta` | Registra boleta de natillera |
| `marcar_boleta_pagada` | Marca boleta como pagada |
| `consultar_saldo_usuario` | Resumen financiero completo |
| `resumen_general` | Métricas globales de la natillera |

## Ejemplos de conversación

```
Tú: Juanita me abonó 80 mil hoy
Agente: [busca Juanita] [consulta sus préstamos] [registra el abono]
        "Abono de $80.000 registrado. Saldo pendiente de Juanita: $320.000."

Tú: Cuánto debe Pedro en total?
Agente: [busca Pedro] [consulta saldo]
        "Pedro tiene deuda activa de $450.000 en 2 préstamos..."

Tú: Agrega a Carlos Pérez, teléfono 3154567890
Agente: [agrega usuario]
        "Listo, Carlos Pérez quedó registrado con ID 12."

Tú: Quiénes no han pagado la boleta de mayo?
Agente: [consulta boletas mes 5] → lista los pendientes
```

## Extender con una tool nueva

Solo agrega la función en `tools.py` y crea el wrapper `@tool` en `agent.py`.
El agente lo usa automáticamente sin tocar ningún flujo.

```python
# tools.py
def calcular_reparto_natillera(anio: int) -> dict:
    """Divide los intereses del año entre todos los participantes"""
    ...

# agent.py
@tool
def calcular_reparto_natillera(anio: int) -> str:
    """Calcula cuánto le corresponde a cada participante al final del año."""
    result = t.calcular_reparto_natillera(anio)
    ...

TOOLS = [..., calcular_reparto_natillera]  # solo añadir aquí
```