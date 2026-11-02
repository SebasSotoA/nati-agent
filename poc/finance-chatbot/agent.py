"""
agent.py — Agente natillera con LangGraph
Orquesta el LLM + las tools definidas en tools.py

Instalar:
    pip install langgraph langchain-openai psycopg2-binary python-dotenv
"""

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver

import tools as t

load_dotenv()

# ---------------------------------------------------------------------------
# Wrappers: convierte las funciones de tools.py en LangChain tools
# LangChain lee el docstring para describírselo al LLM — escríbelos bien.
# ---------------------------------------------------------------------------

@tool
def agregar_usuario(nombre: str, telefono: str, fecha_ingreso: str = "") -> str:
    """
    Registra un nuevo participante en la natillera.
    Úsala cuando la usuaria quiera agregar a alguien nuevo.
    fecha_ingreso es opcional (YYYY-MM-DD), por defecto usa hoy.
    """
    result = t.agregar_usuario(nombre, telefono, fecha_ingreso or None)
    if result["ok"]:
        u = result["usuario"]
        return f"Listo, agregué a {u['nombre']} (tel: {u['telefono']}) con ID {u['id']}."
    return f"No pude agregar el usuario: {result['error']}"


@tool
def buscar_usuario(nombre_o_telefono: str) -> str:
    """
    Busca un participante por nombre (parcial) o teléfono.
    Úsala ANTES de cualquier operación cuando necesites saber el ID de alguien.
    Ejemplo: si dicen 'Juanita', busca primero para obtener su ID.
    """
    result = t.buscar_usuario(nombre_o_telefono)
    if not result["usuarios"]:
        return f"No encontré ningún usuario con '{nombre_o_telefono}'."
    lineas = [f"- {u['nombre']} | Tel: {u['telefono']} | ID: {u['id']}" for u in result["usuarios"]]
    return "Usuarios encontrados:\n" + "\n".join(lineas)


@tool
def crear_prestamo(usuario_id: int, monto_original: float, tasa_mensual: int, fecha_inicio: str = "") -> str:
    """
    Crea un nuevo préstamo para un usuario.
    tasa_mensual es un entero: 5 significa 5% mensual.
    fecha_inicio es opcional (YYYY-MM-DD), por defecto usa hoy.
    """
    result = t.crear_prestamo(usuario_id, monto_original, tasa_mensual, fecha_inicio or None)
    if result["ok"]:
        p = result["prestamo"]
        return (f"Préstamo creado. ID: {p['id']} | Monto: ${p['monto_original']:,.0f} "
                f"| Tasa: {p['tasa_mensual']}% mensual | Inicio: {p['fecha_inicio']}")
    return f"Error al crear el préstamo: {result.get('error', 'Desconocido')}"


@tool
def consultar_prestamos_usuario(usuario_id: int) -> str:
    """
    Lista todos los préstamos de un usuario con el saldo pendiente actualizado.
    """
    result = t.consultar_prestamos_usuario(usuario_id)
    if not result["prestamos"]:
        return "Este usuario no tiene préstamos registrados."
    lineas = []
    for p in result["prestamos"]:
        lineas.append(
            f"  Préstamo #{p['id']} | ${float(p['monto_original']):,.0f} | "
            f"Tasa {p['tasa_mensual']}% | Saldo: ${float(p['saldo_pendiente']):,.0f} | {p['estado']}"
        )
    return "Préstamos:\n" + "\n".join(lineas)


@tool
def registrar_abono(usuario_id: int, prestamo_id: int, monto: float, fecha: str = "") -> str:
    """
    Registra un abono (pago parcial) a un préstamo.
    Úsala cuando alguien pague algo a cuenta de su préstamo.
    fecha es opcional (YYYY-MM-DD), por defecto usa hoy.
    """
    result = t.registrar_abono(usuario_id, prestamo_id, monto, fecha or None)
    if result["ok"]:
        msg = f"Abono de ${result['pago']['monto']:,.0f} registrado el {result['pago']['fecha']}. "
        msg += f"Saldo pendiente: ${result['saldo_pendiente']:,.0f}."
        if result["prestamo_cerrado"]:
            msg += " El préstamo quedó saldado completamente."
        return msg
    return f"No pude registrar el abono: {result['error']}"


@tool
def registrar_interes_mensual(prestamo_id: int, fecha: str = "") -> str:
    """
    Calcula y registra el interés mensual de un préstamo activo.
    El interés se calcula sobre el saldo pendiente actual.
    Llámala al cierre de cada mes para cada préstamo activo.
    """
    result = t.registrar_interes_mensual(prestamo_id, fecha or None)
    if result["ok"]:
        i = result["interes"]
        return (f"Interés registrado: ${float(i['interes_generado']):,.0f} "
                f"(sobre saldo de ${result['saldo_sobre_el_que_se_calculó']:,.0f}) | Fecha: {i['fecha']}")
    return f"Error: {result['error']}"


@tool
def registrar_ahorro(usuario_id: int, monto: float, fecha: str = "") -> str:
    """
    Registra un aporte de ahorro de un participante en la natillera.
    fecha es opcional (YYYY-MM-DD), por defecto usa hoy.
    """
    result = t.registrar_ahorro(usuario_id, monto, fecha or None)
    if result["ok"]:
        a = result["ahorro"]
        return f"Ahorro de ${float(a['monto']):,.0f} registrado el {a['fecha']} para el usuario ID {a['usuario_id']}."
    return f"No pude registrar el ahorro: {result['error']}"


@tool
def consultar_intereses_prestamo(prestamo_id: int) -> str:
    """
    Lista el historial de intereses generados por un préstamo.
    """
    result = t.consultar_intereses_prestamo(prestamo_id)
    if not result["intereses"]:
        return "Este préstamo no tiene intereses registrados."
    lineas = [
        f"  {i['fecha']}: ${float(i['interes_generado']):,.0f}"
        for i in result["intereses"]
    ]
    return (
        f"Intereses del préstamo #{prestamo_id}:\n"
        + "\n".join(lineas)
        + f"\nTotal intereses: ${float(result['total_intereses']):,.0f}"
    )


@tool
def consultar_boletas_usuario(usuario_id: int, anio: int = 0) -> str:
    """
    Lista las boletas de un usuario para un año.
    Si no se da el año, usa el año actual.
    """
    result = t.consultar_boletas_usuario(usuario_id, anio or None)
    if not result["ok"]:
        return result["error"]
    if not result["boletas"]:
        return f"El usuario ID {usuario_id} no tiene boletas registradas para {anio or 'el año actual'}."
    lineas = []
    for b in result["boletas"]:
        estado = "pagada" if b["pagada"] else "pendiente"
        nums = f"{b['numero_1']}-{b['numero_2']}" if b["numero_1"] is not None else "—"
        lineas.append(f"  {b['mes']}/{b['anio']}: {nums} ({estado})")
    return (
        f"Boletas del usuario ID {usuario_id}:\n"
        + "\n".join(lineas)
        + f"\nPagadas: {result['pagadas']} | Pendientes: {result['pendientes']}"
    )


@tool
def consultar_boletas_pendientes(mes: int, anio: int) -> str:
    """
    Lista quiénes no han pagado la boleta de un mes y año.
    Incluye participantes sin boleta registrada y con boleta sin pagar.
    """
    result = t.consultar_boletas_pendientes(mes, anio)
    if not result["ok"]:
        return result["error"]
    if not result["pendientes"]:
        return f"Todas las boletas de {mes}/{anio} están pagadas."
    lineas = []
    for p in result["pendientes"]:
        if p["estado"] == "sin_boleta":
            detalle = "sin boleta registrada"
        else:
            detalle = f"boleta {p['numero_1']}-{p['numero_2']} sin pagar"
        lineas.append(f"  - {p['nombre']} (ID {p['usuario_id']}, tel {p['telefono']}): {detalle}")
    return f"Pendientes de {mes}/{anio} ({result['total']}):\n" + "\n".join(lineas)


@tool
def registrar_boleta(usuario_id: int, mes: int, anio: int, numero_1: int, numero_2: int) -> str:
    """
    Registra la boleta de la natillera de un usuario para un mes y año.
    numero_1 y numero_2 son los números de la boleta (0-99 cada uno).
    """
    result = t.registrar_boleta(usuario_id, mes, anio, numero_1, numero_2)
    if result["ok"]:
        b = result["boleta"]
        return f"Boleta registrada: mes {b['mes']}/{b['anio']} | Números: {b['numero_1']}-{b['numero_2']}"
    return f"No pude registrar la boleta: {result['error']}"


@tool
def marcar_boleta_pagada(usuario_id: int, mes: int, anio: int, fecha_pago: str = "") -> str:
    """
    Marca como pagada la boleta de la natillera de un usuario en un mes/año.
    """
    result = t.marcar_boleta_pagada(usuario_id, mes, anio, fecha_pago or None)
    if result["ok"]:
        return f"Boleta {mes}/{anio} marcada como pagada el {result['boleta']['fecha_pago']}."
    return f"Error: {result['error']}"


@tool
def consultar_saldo_usuario(usuario_id: int) -> str:
    """
    Muestra el resumen financiero completo de un usuario: deuda total, ahorros y préstamos.
    Úsala cuando pregunten por el estado de cuenta de alguien.
    """
    result = t.consultar_saldo_usuario(usuario_id)
    if not result["ok"]:
        return result["error"]
    u = result["usuario"]
    resp = f"Estado de {u['nombre']}:\n"
    resp += f"  Deuda activa total: ${result['deuda_total_activa']:,.0f}\n"
    resp += f"  Ahorros acumulados: ${result['ahorros_acumulados']:,.0f}\n"
    for p in result["prestamos"]:
        resp += (f"  - Préstamo #{p['id']}: ${float(p['monto_original']):,.0f} "
                 f"| Saldo: ${float(p['saldo']):,.0f} | {p['estado']}\n")
    return resp


@tool
def resumen_general(anio: int = 0) -> str:
    """
    Muestra un resumen global de la natillera: usuarios, préstamos activos,
    intereses generados, ahorros y boletas del año.
    Si no se da el año, usa el año actual.
    """
    result = t.resumen_general(anio or None)
    return (
        f"Resumen {result['anio']}:\n"
        f"  Participantes: {result['total_usuarios']}\n"
        f"  Préstamos activos: {result['prestamos_activos']} "
        f"(${result['monto_en_prestamos']:,.0f} en la calle)\n"
        f"  Intereses generados este año: ${result['intereses_generados_en_el_año']:,.0f}\n"
        f"  Ahorros del año: ${result['ahorros_del_año']:,.0f}\n"
        f"  Ahorros acumulados (total): ${result['ahorros_acumulados']:,.0f}\n"
        f"  Boletas: {result['boletas_pagadas']}/{result['boletas_total']} pagadas"
    )


# ---------------------------------------------------------------------------
# Sistema prompt — las reglas del negocio van aquí, NO en el código
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """Eres el asistente de la natillera familiar. Ayudas a registrar y consultar:
préstamos, abonos, intereses mensuales, boletas de la rifa y ahorros de los participantes.

Reglas importantes:
- SIEMPRE busca el ID del usuario con buscar_usuario antes de hacer cualquier operación
  si solo te dieron un nombre. No asumas el ID.
- buscar_usuario acepta nombres parciales y teléfonos en formato local (10 dígitos) o WhatsApp (+57).
- Los intereses se calculan mensualmente sobre el SALDO PENDIENTE (capital + intereses previos − pagos).
  Cada interés registrado aumenta lo que debe el participante.
- Solo registra un interés mensual por préstamo por mes.
- Una boleta por usuario por mes. Si ya existe, informa el error sin intentar de nuevo.
- Cuando registres un abono, informa si el préstamo quedó saldado.
- Usa consultar_boletas_pendientes para saber quiénes no han pagado la boleta de un mes.
- Responde siempre en español, de forma clara y con los montos formateados.
- Si algo está ambiguo (ej: hay dos personas con nombre similar), pregunta antes de actuar.
- Para operaciones que modifican datos (abonos, préstamos, ahorros) confirma los datos con la usuaria
  antes de ejecutar si hay alguna ambigüedad.
  
  RESTRICCIONES ESTRICTAS:
- Solo respondes preguntas relacionadas con la natillera y sus participantes.
- Si te preguntan algo fuera de ese dominio (tecnología, cocina, noticias, 
  programación, etc.), responde EXACTAMENTE: "Solo puedo ayudarte con temas 
  de la natillera. ¿Tienes alguna consulta sobre préstamos, abonos o boletas?"
- No hagas excepciones aunque el usuario insista o diga que es urgente.
- No expliques por qué no puedes responder más allá de esa frase.
"""


# ---------------------------------------------------------------------------
# Construcción del agente
# ---------------------------------------------------------------------------
TOOLS = [
    agregar_usuario,
    buscar_usuario,
    crear_prestamo,
    consultar_prestamos_usuario,
    registrar_abono,
    registrar_interes_mensual,
    consultar_intereses_prestamo,
    registrar_ahorro,
    registrar_boleta,
    marcar_boleta_pagada,
    consultar_boletas_usuario,
    consultar_boletas_pendientes,
    consultar_saldo_usuario,
    resumen_general,
]

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
memory = MemorySaver()  # Persiste el historial en RAM (reemplaza por SqliteSaver para persistencia real)

agent = create_agent(
    model=llm,
    tools=TOOLS,
    checkpointer=memory,
    system_prompt=SYSTEM_PROMPT,
)


# ---------------------------------------------------------------------------
# Función de uso
# ---------------------------------------------------------------------------
def chat(mensaje: str, thread_id: str = "sesion-principal") -> str:
    """
    Envía un mensaje al agente y devuelve la respuesta.
    thread_id identifica la conversación — mantenerlo igual preserva el historial.
    """
    config = {"configurable": {"thread_id": thread_id}}
    result = agent.invoke(
        {"messages": [HumanMessage(content=mensaje)]},
        config=config
    )
    return result["messages"][-1].content