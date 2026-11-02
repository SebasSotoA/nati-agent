-- =========================================
-- TABLA USUARIOS
-- =========================================
CREATE TABLE public.usuarios (
  id SERIAL PRIMARY KEY,
  nombre TEXT NOT NULL,
  telefono TEXT NOT NULL UNIQUE CHECK (length(telefono) = 10),
  fecha_ingreso DATE NOT NULL
);

-- =========================================
-- TABLA AHORROS
-- Registro de aportes de ahorro
-- =========================================
CREATE TABLE public.ahorros (
  id SERIAL PRIMARY KEY,
  usuario_id INTEGER NOT NULL,
  fecha DATE NOT NULL,
  monto NUMERIC(12,2) NOT NULL CHECK (monto > 0),

  CONSTRAINT ahorros_usuario_id_fkey
    FOREIGN KEY (usuario_id)
    REFERENCES public.usuarios(id)
);

-- =========================================
-- TABLA PRESTAMOS
-- =========================================
CREATE TABLE public.prestamos (
  id SERIAL PRIMARY KEY,

  usuario_id INTEGER NOT NULL,

  monto_original NUMERIC(12,2) NOT NULL CHECK (monto_original > 0),

  -- Porcentaje mensual entre 0 y 99
  tasa_mensual SMALLINT NOT NULL
    CHECK (tasa_mensual >= 0 AND tasa_mensual <= 99),

  fecha_inicio DATE NOT NULL,

  estado TEXT NOT NULL DEFAULT 'activo'
    CHECK (estado IN ('activo', 'cerrado')),

  CONSTRAINT prestamos_usuario_id_fkey
    FOREIGN KEY (usuario_id)
    REFERENCES public.usuarios(id)
);

-- =========================================
-- TABLA PAGOS_PRESTAMO
-- Abonos realizados a préstamos
-- =========================================
CREATE TABLE public.pagos_prestamo (
  id SERIAL PRIMARY KEY,

  prestamo_id INTEGER NOT NULL,
  usuario_id INTEGER NOT NULL,

  fecha DATE NOT NULL,

  monto NUMERIC(12,2) NOT NULL CHECK (monto > 0),

  CONSTRAINT pagos_prestamo_prestamo_id_fkey
    FOREIGN KEY (prestamo_id)
    REFERENCES public.prestamos(id),

  CONSTRAINT pagos_prestamo_usuario_id_fkey
    FOREIGN KEY (usuario_id)
    REFERENCES public.usuarios(id)
);

-- =========================================
-- TABLA INTERESES_PRESTAMO
-- Historial de intereses generados
-- =========================================
CREATE TABLE public.intereses_prestamo (
  id SERIAL PRIMARY KEY,

  prestamo_id INTEGER NOT NULL,

  fecha DATE NOT NULL,

  interes_generado NUMERIC(12,2) NOT NULL
    CHECK (interes_generado >= 0),

  CONSTRAINT intereses_prestamo_prestamo_id_fkey
    FOREIGN KEY (prestamo_id)
    REFERENCES public.prestamos(id)
);

-- =========================================
-- TABLA BOLETAS_RIFA
-- Registro mensual de pago de boletas
-- =========================================
CREATE TABLE public.boletas_rifa (
  id SERIAL PRIMARY KEY,

  usuario_id INTEGER NOT NULL,

  -- Mes correspondiente de la boleta
  mes INTEGER NOT NULL
    CHECK (mes >= 1 AND mes <= 12),

  anio INTEGER NOT NULL,

  fecha_pago DATE,

  pagada BOOLEAN NOT NULL DEFAULT false,

  numero_1 SMALLINT NOT NULL
    CHECK (numero_1 >= 0 AND numero_1 <= 99),

  numero_2 SMALLINT NOT NULL
    CHECK (numero_2 >= 0 AND numero_2 <= 99),

  CONSTRAINT boletas_rifa_usuario_id_fkey
    FOREIGN KEY (usuario_id)
    REFERENCES public.usuarios(id),

  CONSTRAINT boleta_unica_mes
    UNIQUE(usuario_id, mes, anio)
);

-- =========================================
-- ÍNDICE: un interés por préstamo por mes
-- (ejecutar también si la BD ya existía)
-- =========================================
CREATE UNIQUE INDEX IF NOT EXISTS intereses_prestamo_unico_mes
  ON public.intereses_prestamo (
    prestamo_id,
    EXTRACT(YEAR FROM fecha),
    EXTRACT(MONTH FROM fecha)
  );