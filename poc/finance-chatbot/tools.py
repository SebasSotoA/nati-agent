"""
tools.py — Herramientas del agente natillera
Cada función es una tool independiente que el LLM puede llamar directamente.
No hay IF/ELSE de intención: el modelo decide cuál usar en runtime.
"""

from datetime import date
from typing import Optional
import os
import re
import psycopg2
from psycopg2 import errorcodes
from psycopg2.extras import RealDictCursor

# ---------------------------------------------------------------------------
# Conexión a Supabase (usa la connection string de tu proyecto Supabase)
# ---------------------------------------------------------------------------
def get_conn():
    return psycopg2.connect(
        os.environ["SUPABASE_DB_URL"],  # postgresql://postgres:PASSWORD@db.xxxx.supabase.co:5432/postgres
        cursor_factory=RealDictCursor
    )


def _normalizar_telefono(telefono: str) -> str:
    """Convierte whatsapp:+573001234567 o +573001234567 en 10 dígitos locales."""
    texto = telefono.strip()
    if texto.lower().startswith("whatsapp:"):
        texto = texto.split(":", 1)[1]
    digitos = re.sub(r"\D", "", texto)
    if len(digitos) > 10:
        digitos = digitos[-10:]
    return digitos


def _validar_usuario(cur, usuario_id: int) -> bool:
    cur.execute("SELECT 1 FROM usuarios WHERE id = %s", (usuario_id,))
    return cur.fetchone() is not None


def _saldo_prestamo_sql(alias: str = "p") -> str:
    """Saldo = capital + intereses generados − pagos."""
    return f"""(
        {alias}.monto_original
        + COALESCE((
            SELECT SUM(interes_generado) FROM intereses_prestamo
            WHERE prestamo_id = {alias}.id
        ), 0)
        - COALESCE((
            SELECT SUM(monto) FROM pagos_prestamo
            WHERE prestamo_id = {alias}.id
        ), 0)
    )"""


def _obtener_saldo_prestamo(cur, prestamo_id: int) -> Optional[float]:
    cur.execute(
        f"SELECT {_saldo_prestamo_sql('p')} AS saldo FROM prestamos p WHERE p.id = %s",
        (prestamo_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return float(row["saldo"])


# ---------------------------------------------------------------------------
# USUARIOS
# ---------------------------------------------------------------------------

def agregar_usuario(nombre: str, telefono: str, fecha_ingreso: Optional[str] = None) -> dict:
    """
    Registra un nuevo participante en la natillera.

    Args:
        nombre: Nombre completo de la persona
        telefono: Número de 10 dígitos (sin +57)
        fecha_ingreso: Fecha en formato YYYY-MM-DD. Si no se da, usa hoy.

    Returns:
        dict con el usuario creado o un mensaje de error si ya existe.
    """
    fecha = fecha_ingreso or str(date.today())
    telefono = _normalizar_telefono(telefono)
    if len(telefono) != 10:
        return {"ok": False, "error": "El teléfono debe tener exactamente 10 dígitos (sin +57)"}
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO usuarios (nombre, telefono, fecha_ingreso)
                VALUES (%s, %s, %s)
                RETURNING id, nombre, telefono, fecha_ingreso
                """,
                (nombre, telefono, fecha)
            )
            conn.commit()
            row = dict(cur.fetchone())
            return {"ok": True, "usuario": row}
    except psycopg2.IntegrityError as e:
        if e.pgcode == errorcodes.UNIQUE_VIOLATION:
            return {"ok": False, "error": f"Ya existe un usuario con el teléfono {telefono}"}
        if e.pgcode == errorcodes.CHECK_VIOLATION:
            return {"ok": False, "error": "El teléfono debe tener exactamente 10 dígitos"}
        raise
    except Exception as e:
        return {"ok": False, "error": str(e)}


def buscar_usuario(nombre_o_telefono: str) -> dict:
    """
    Busca un usuario por nombre (parcial) o por teléfono exacto.
    Útil cuando el agente necesita resolver a quién se refiere el mensaje.

    Args:
        nombre_o_telefono: Texto libre para buscar

    Returns:
        Lista de usuarios que coinciden
    """
    q = f"%{nombre_o_telefono}%"
    telefono = _normalizar_telefono(nombre_o_telefono)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, nombre, telefono, fecha_ingreso
            FROM usuarios
            WHERE nombre ILIKE %s OR telefono = %s OR telefono = %s
            ORDER BY nombre
            LIMIT 10
            """,
            (q, nombre_o_telefono, telefono)
        )
        rows = [dict(r) for r in cur.fetchall()]
    return {"usuarios": rows, "total": len(rows)}


# ---------------------------------------------------------------------------
# PRÉSTAMOS
# ---------------------------------------------------------------------------

def crear_prestamo(
    usuario_id: int,
    monto_original: float,
    tasa_mensual: int,
    fecha_inicio: Optional[str] = None
) -> dict:
    """
    Crea un nuevo préstamo para un usuario.

    Args:
        usuario_id: ID del usuario en la tabla usuarios
        monto_original: Monto prestado en pesos
        tasa_mensual: Tasa de interés mensual en porcentaje entero (ej: 5 = 5%)
        fecha_inicio: Fecha en YYYY-MM-DD. Si no se da, usa hoy.

    Returns:
        dict con el préstamo creado
    """
    fecha = fecha_inicio or str(date.today())
    if monto_original <= 0:
        return {"ok": False, "error": "El monto del préstamo debe ser mayor a cero"}
    if not 0 <= tasa_mensual <= 99:
        return {"ok": False, "error": "La tasa mensual debe estar entre 0 y 99"}
    try:
        with get_conn() as conn, conn.cursor() as cur:
            if not _validar_usuario(cur, usuario_id):
                return {"ok": False, "error": f"No existe un usuario con ID {usuario_id}"}
            cur.execute(
                """
                INSERT INTO prestamos (usuario_id, monto_original, tasa_mensual, fecha_inicio, estado)
                VALUES (%s, %s, %s, %s, 'activo')
                RETURNING id, usuario_id, monto_original, tasa_mensual, fecha_inicio, estado
                """,
                (usuario_id, monto_original, tasa_mensual, fecha)
            )
            conn.commit()
            row = dict(cur.fetchone())
        return {"ok": True, "prestamo": row}
    except psycopg2.IntegrityError as e:
        if e.pgcode == errorcodes.FOREIGN_KEY_VIOLATION:
            return {"ok": False, "error": f"No existe un usuario con ID {usuario_id}"}
        raise


def consultar_prestamos_usuario(usuario_id: int) -> dict:
    """
    Lista todos los préstamos (activos y cerrados) de un usuario,
    con el saldo pendiente calculado dinámicamente.

    Args:
        usuario_id: ID del usuario

    Returns:
        Lista de préstamos con saldo pendiente por cada uno
    """
    with get_conn() as conn, conn.cursor() as cur:
        saldo_sql = _saldo_prestamo_sql("p")
        cur.execute(
            f"""
            SELECT
                p.id,
                p.monto_original,
                p.tasa_mensual,
                p.fecha_inicio,
                p.estado,
                COALESCE((
                    SELECT SUM(monto) FROM pagos_prestamo WHERE prestamo_id = p.id
                ), 0) AS total_pagado,
                COALESCE((
                    SELECT SUM(interes_generado) FROM intereses_prestamo WHERE prestamo_id = p.id
                ), 0) AS total_intereses,
                {saldo_sql} AS saldo_pendiente
            FROM prestamos p
            WHERE p.usuario_id = %s
            ORDER BY p.fecha_inicio DESC
            """,
            (usuario_id,)
        )
        rows = [dict(r) for r in cur.fetchall()]
    return {"prestamos": rows}


# ---------------------------------------------------------------------------
# ABONOS / PAGOS
# ---------------------------------------------------------------------------

def registrar_abono(
    usuario_id: int,
    prestamo_id: int,
    monto: float,
    fecha: Optional[str] = None
) -> dict:
    """
    Registra un abono a un préstamo existente.

    Args:
        usuario_id: ID del usuario que paga
        prestamo_id: ID del préstamo al que se abona
        monto: Valor del abono en pesos
        fecha: Fecha del pago en YYYY-MM-DD. Si no se da, usa hoy.

    Returns:
        dict con confirmación y nuevo saldo pendiente
    """
    fecha_pago = fecha or str(date.today())
    if monto <= 0:
        return {"ok": False, "error": "El monto del abono debe ser mayor a cero"}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, estado FROM prestamos WHERE id = %s AND usuario_id = %s",
            (prestamo_id, usuario_id)
        )
        prestamo = cur.fetchone()
        if not prestamo:
            return {"ok": False, "error": "Préstamo no encontrado para ese usuario"}
        if prestamo["estado"] == "cerrado":
            return {"ok": False, "error": "El préstamo ya está cerrado"}

        saldo_antes = _obtener_saldo_prestamo(cur, prestamo_id)
        if saldo_antes is None:
            return {"ok": False, "error": "Préstamo no encontrado"}
        if monto > saldo_antes:
            return {
                "ok": False,
                "error": f"El abono (${monto:,.0f}) supera el saldo pendiente (${saldo_antes:,.0f})",
            }

        cur.execute(
            """
            INSERT INTO pagos_prestamo (prestamo_id, usuario_id, fecha, monto)
            VALUES (%s, %s, %s, %s)
            RETURNING id, monto, fecha
            """,
            (prestamo_id, usuario_id, fecha_pago, monto)
        )
        pago = dict(cur.fetchone())

        saldo = _obtener_saldo_prestamo(cur, prestamo_id)
        if saldo <= 0:
            cur.execute("UPDATE prestamos SET estado = 'cerrado' WHERE id = %s", (prestamo_id,))

        conn.commit()

    return {
        "ok": True,
        "pago": pago,
        "saldo_pendiente": max(saldo, 0),
        "prestamo_cerrado": saldo <= 0
    }


# ---------------------------------------------------------------------------
# INTERESES
# ---------------------------------------------------------------------------

def registrar_interes_mensual(prestamo_id: int, fecha: Optional[str] = None) -> dict:
    """
    Calcula y registra el interés mensual generado por un préstamo.
    Se debe llamar una vez al mes por cada préstamo activo.

    El interés se calcula sobre el saldo pendiente actual (no sobre monto original).

    Args:
        prestamo_id: ID del préstamo
        fecha: Fecha de corte en YYYY-MM-DD. Si no se da, usa hoy.

    Returns:
        dict con el interés calculado y registrado
    """
    fecha_corte = fecha or str(date.today())
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.id, p.tasa_mensual
            FROM prestamos p
            WHERE p.id = %s AND p.estado = 'activo'
            """,
            (prestamo_id,)
        )
        row = cur.fetchone()
        if not row:
            return {"ok": False, "error": "Préstamo no encontrado o ya cerrado"}

        cur.execute(
            """
            SELECT 1 FROM intereses_prestamo
            WHERE prestamo_id = %s
              AND EXTRACT(MONTH FROM fecha) = EXTRACT(MONTH FROM %s::date)
              AND EXTRACT(YEAR FROM fecha) = EXTRACT(YEAR FROM %s::date)
            """,
            (prestamo_id, fecha_corte, fecha_corte)
        )
        if cur.fetchone():
            return {"ok": False, "error": "Ya se registró el interés de ese mes para este préstamo"}

        saldo = _obtener_saldo_prestamo(cur, prestamo_id)
        if saldo is None or saldo <= 0:
            return {"ok": False, "error": "El préstamo no tiene saldo pendiente sobre el cual calcular interés"}

        interes = round(saldo * (row["tasa_mensual"] / 100), 2)

        try:
            cur.execute(
                """
                INSERT INTO intereses_prestamo (prestamo_id, fecha, interes_generado)
                VALUES (%s, %s, %s)
                RETURNING id, fecha, interes_generado
                """,
                (prestamo_id, fecha_corte, interes)
            )
            conn.commit()
            reg = dict(cur.fetchone())
        except psycopg2.IntegrityError as e:
            if e.pgcode == errorcodes.UNIQUE_VIOLATION:
                return {"ok": False, "error": "Ya se registró el interés de ese mes para este préstamo"}
            raise

    return {"ok": True, "interes": reg, "saldo_sobre_el_que_se_calculó": saldo}


def consultar_intereses_prestamo(prestamo_id: int) -> dict:
    """
    Lista el historial de intereses generados por un préstamo.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT fecha, interes_generado
            FROM intereses_prestamo
            WHERE prestamo_id = %s
            ORDER BY fecha
            """,
            (prestamo_id,)
        )
        rows = [dict(r) for r in cur.fetchall()]
        total = sum(r["interes_generado"] for r in rows)
    return {"intereses": rows, "total_intereses": total}


# ---------------------------------------------------------------------------
# AHORROS
# ---------------------------------------------------------------------------

def registrar_ahorro(
    usuario_id: int,
    monto: float,
    fecha: Optional[str] = None
) -> dict:
    """
    Registra un aporte de ahorro de un participante.

    Args:
        usuario_id: ID del usuario
        monto: Valor del aporte en pesos
        fecha: Fecha del aporte en YYYY-MM-DD. Si no se da, usa hoy.

    Returns:
        dict con el ahorro registrado
    """
    fecha_aporte = fecha or str(date.today())
    if monto <= 0:
        return {"ok": False, "error": "El monto del ahorro debe ser mayor a cero"}
    try:
        with get_conn() as conn, conn.cursor() as cur:
            if not _validar_usuario(cur, usuario_id):
                return {"ok": False, "error": f"No existe un usuario con ID {usuario_id}"}
            cur.execute(
                """
                INSERT INTO ahorros (usuario_id, fecha, monto)
                VALUES (%s, %s, %s)
                RETURNING id, usuario_id, fecha, monto
                """,
                (usuario_id, fecha_aporte, monto)
            )
            conn.commit()
            row = dict(cur.fetchone())
        return {"ok": True, "ahorro": row}
    except psycopg2.IntegrityError as e:
        if e.pgcode == errorcodes.FOREIGN_KEY_VIOLATION:
            return {"ok": False, "error": f"No existe un usuario con ID {usuario_id}"}
        if e.pgcode == errorcodes.CHECK_VIOLATION:
            return {"ok": False, "error": "El monto del ahorro debe ser mayor a cero"}
        raise


# ---------------------------------------------------------------------------
# BOLETAS DE RIFA / NATILLERA
# ---------------------------------------------------------------------------

def registrar_boleta(
    usuario_id: int,
    mes: int,
    anio: int,
    numero_1: int,
    numero_2: int
) -> dict:
    """
    Registra una boleta de la natillera para un usuario en un mes/año.

    Args:
        usuario_id: ID del usuario
        mes: Número de mes (1-12)
        anio: Año (ej: 2025)
        numero_1: Primer número de la boleta (0-99)
        numero_2: Segundo número de la boleta (0-99)

    Returns:
        dict con la boleta creada
    """
    with get_conn() as conn, conn.cursor() as cur:
        if not _validar_usuario(cur, usuario_id):
            return {"ok": False, "error": f"No existe un usuario con ID {usuario_id}"}
        if not 1 <= mes <= 12:
            return {"ok": False, "error": "El mes debe estar entre 1 y 12"}
        if not 0 <= numero_1 <= 99 or not 0 <= numero_2 <= 99:
            return {"ok": False, "error": "Los números de boleta deben estar entre 0 y 99"}
        try:
            cur.execute(
                """
                INSERT INTO boletas_rifa (usuario_id, mes, anio, numero_1, numero_2, pagada, fecha_pago)
                VALUES (%s, %s, %s, %s, %s, false, NULL)
                RETURNING id, mes, anio, numero_1, numero_2, pagada
                """,
                (usuario_id, mes, anio, numero_1, numero_2)
            )
            conn.commit()
            row = dict(cur.fetchone())
            return {"ok": True, "boleta": row}
        except psycopg2.IntegrityError as e:
            if e.pgcode == errorcodes.UNIQUE_VIOLATION:
                return {"ok": False, "error": f"Ya existe boleta para ese usuario en {mes}/{anio}"}
            if e.pgcode == errorcodes.FOREIGN_KEY_VIOLATION:
                return {"ok": False, "error": f"No existe un usuario con ID {usuario_id}"}
            raise


def marcar_boleta_pagada(usuario_id: int, mes: int, anio: int, fecha_pago: Optional[str] = None) -> dict:
    """
    Marca una boleta como pagada.

    Args:
        usuario_id: ID del usuario
        mes: Mes de la boleta
        anio: Año de la boleta
        fecha_pago: Fecha del pago en YYYY-MM-DD. Si no se da, usa hoy.

    Returns:
        dict con confirmación
    """
    fecha = fecha_pago or str(date.today())
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE boletas_rifa
            SET pagada = true, fecha_pago = %s
            WHERE usuario_id = %s AND mes = %s AND anio = %s
            RETURNING id, mes, anio, pagada, fecha_pago
            """,
            (fecha, usuario_id, mes, anio)
        )
        row = cur.fetchone()
        conn.commit()
    if not row:
        return {"ok": False, "error": "Boleta no encontrada"}
    return {"ok": True, "boleta": dict(row)}


def consultar_boletas_pendientes(mes: int, anio: int) -> dict:
    """
    Lista participantes que no han pagado la boleta de un mes/año.
    Incluye quienes no tienen boleta registrada y quienes la tienen sin pagar.

    Args:
        mes: Número de mes (1-12)
        anio: Año (ej: 2025)

    Returns:
        Lista de participantes pendientes con su estado
    """
    if not 1 <= mes <= 12:
        return {"ok": False, "error": "El mes debe estar entre 1 y 12"}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                u.id,
                u.nombre,
                u.telefono,
                b.id AS boleta_id,
                b.pagada,
                b.numero_1,
                b.numero_2
            FROM usuarios u
            LEFT JOIN boletas_rifa b
              ON b.usuario_id = u.id AND b.mes = %s AND b.anio = %s
            WHERE b.id IS NULL OR b.pagada = false
            ORDER BY u.nombre
            """,
            (mes, anio)
        )
        rows = [dict(r) for r in cur.fetchall()]

    pendientes = []
    for r in rows:
        if r["boleta_id"] is None:
            estado = "sin_boleta"
        else:
            estado = "registrada_sin_pagar"
        pendientes.append({
            "usuario_id": r["id"],
            "nombre": r["nombre"],
            "telefono": r["telefono"],
            "estado": estado,
            "numero_1": r["numero_1"],
            "numero_2": r["numero_2"],
        })

    return {
        "ok": True,
        "mes": mes,
        "anio": anio,
        "pendientes": pendientes,
        "total": len(pendientes),
    }


def consultar_boletas_usuario(usuario_id: int, anio: Optional[int] = None) -> dict:
    """
    Lista las boletas de un usuario, opcionalmente filtrando por año.
    """
    anio_filtro = anio or date.today().year
    with get_conn() as conn, conn.cursor() as cur:
        if not _validar_usuario(cur, usuario_id):
            return {"ok": False, "error": f"No existe un usuario con ID {usuario_id}"}
        cur.execute(
            """
            SELECT mes, anio, numero_1, numero_2, pagada, fecha_pago
            FROM boletas_rifa
            WHERE usuario_id = %s AND anio = %s
            ORDER BY mes
            """,
            (usuario_id, anio_filtro)
        )
        rows = [dict(r) for r in cur.fetchall()]
    pagadas = sum(1 for r in rows if r["pagada"])
    return {"ok": True, "boletas": rows, "pagadas": pagadas, "pendientes": len(rows) - pagadas}


# ---------------------------------------------------------------------------
# RESUMEN / DASHBOARD
# ---------------------------------------------------------------------------

def resumen_general(anio: Optional[int] = None) -> dict:
    """
    Genera un resumen de la natillera: total prestado, recaudado en intereses,
    ahorros acumulados y estado de boletas.

    Args:
        anio: Año a consultar. Si no se da, usa el año actual.

    Returns:
        dict con métricas globales
    """
    anio_consulta = anio or date.today().year
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS total FROM usuarios")
        total_usuarios = cur.fetchone()["total"]

        cur.execute(
            "SELECT COUNT(*) AS activos, COALESCE(SUM(monto_original),0) AS total FROM prestamos WHERE estado='activo'"
        )
        r = cur.fetchone()
        prestamos_activos = r["activos"]
        monto_en_prestamos = float(r["total"])

        cur.execute(
            "SELECT COALESCE(SUM(interes_generado),0) AS total FROM intereses_prestamo WHERE EXTRACT(YEAR FROM fecha) = %s",
            (anio_consulta,)
        )
        total_intereses = float(cur.fetchone()["total"])

        cur.execute(
            "SELECT COALESCE(SUM(monto),0) AS total FROM ahorros WHERE EXTRACT(YEAR FROM fecha) = %s",
            (anio_consulta,)
        )
        ahorros_del_anio = float(cur.fetchone()["total"])

        cur.execute("SELECT COALESCE(SUM(monto),0) AS total FROM ahorros")
        ahorros_acumulados = float(cur.fetchone()["total"])

        cur.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN pagada THEN 1 ELSE 0 END) AS pagadas FROM boletas_rifa WHERE anio = %s",
            (anio_consulta,)
        )
        b = cur.fetchone()

    return {
        "anio": anio_consulta,
        "total_usuarios": total_usuarios,
        "prestamos_activos": prestamos_activos,
        "monto_en_prestamos": monto_en_prestamos,
        "intereses_generados_en_el_año": total_intereses,
        "ahorros_del_año": ahorros_del_anio,
        "ahorros_acumulados": ahorros_acumulados,
        "boletas_total": b["total"],
        "boletas_pagadas": b["pagadas"] or 0
    }


def consultar_saldo_usuario(usuario_id: int) -> dict:
    """
    Devuelve un resumen financiero completo de un usuario:
    préstamos, saldo total adeudado, ahorros e historial de pagos recientes.

    Args:
        usuario_id: ID del usuario

    Returns:
        dict con resumen del usuario
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT nombre, telefono FROM usuarios WHERE id = %s", (usuario_id,))
        usuario = cur.fetchone()
        if not usuario:
            return {"ok": False, "error": "Usuario no encontrado"}

        saldo_sql = _saldo_prestamo_sql("p")
        cur.execute(
            f"""
            SELECT p.id, p.monto_original, p.tasa_mensual, p.fecha_inicio, p.estado,
                   COALESCE((
                       SELECT SUM(monto) FROM pagos_prestamo WHERE prestamo_id = p.id
                   ), 0) AS total_pagado,
                   COALESCE((
                       SELECT SUM(interes_generado) FROM intereses_prestamo WHERE prestamo_id = p.id
                   ), 0) AS total_intereses,
                   {saldo_sql} AS saldo
            FROM prestamos p
            WHERE p.usuario_id = %s
            ORDER BY p.estado, p.fecha_inicio DESC
            """,
            (usuario_id,)
        )
        prestamos = [dict(r) for r in cur.fetchall()]
        deuda_total = sum(max(float(p["saldo"]), 0) for p in prestamos if p["estado"] == "activo")

        cur.execute(
            "SELECT COALESCE(SUM(monto),0) AS total FROM ahorros WHERE usuario_id = %s",
            (usuario_id,)
        )
        ahorros = float(cur.fetchone()["total"])

    return {
        "ok": True,
        "usuario": dict(usuario),
        "deuda_total_activa": deuda_total,
        "ahorros_acumulados": ahorros,
        "prestamos": prestamos
    }