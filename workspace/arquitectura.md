Aquí tienes el **Plan Maestro Definitivo**, integrando de forma directa todas las correcciones y piezas arquitectónicas solicitadas. Este es tu *blueprint* final de implementación.

---

### Arquitectura Objetivo

```text
                         ┌─────────────────┐
                         │    STRATEGY     │
                         │  Pure signals   │
                         └────────┬────────┘
                                  │
                               SIGNAL
                                  │
                         ┌────────▼────────┐
                         │  TRADING ENGINE │
                         └────────┬────────┘
                                  │
                    ┌─────────────▼─────────────┐
                    │            OMS            │
                    │                           │
                    │ ├─ Order Manager          │
                    │ ├─ Position Manager       │
                    │ ├─ Risk Manager           │
                    │ └─ Reconciliation         │
                    └─────────────┬─────────────┘
                                  │
┌─────────────────────────────────▼───────────┐
│              SAFETY LAYER                   │
│ ├─ Rate Limiter                             │
│ ├─ Retry / Timeout Policy                   │
│ ├─ Circuit Breaker                          │
│ ├─ Kill Switch                              │
│ ├─ Idempotency / Reconciliation             │
│ └─ Trading Calendar                         │
└─────────────────────────────────┬───────────┘
                                  │
                         ┌────────▼────────┐
                         │ BROKER INTERFACE│
                         └────────┬────────┘
                                  │
                     ┌────────────┴───────────┐
                     │                        │
                PPI Adapter               IOL Adapter
                     │
                ┌────▼─────┐
                │ PPI API  │
                └────┬─────┘
                     │
                   BYMA

 ==================== MARKET SIDE & EVENT BUS ====================

 PPI WS ───────┐
               │
 BYMA/PyOBD ───┼──→ MARKET DATA NORMALIZER
               │    (Emite objetos estandarizados `Quote`)
 Historical ───┘              │
                              ▼
                          EVENT BUS
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
                 Strategy           Logger / Recorder

```

---

### Fase 1 — Event Bus y Auditoría del Motor Base

* Inspeccionar tu código actual (`FreqBot`) para diseñar la `BrokerInterface` basándote estrictamente en los métodos que tu motor ya utiliza.
* Implementar un **Event Bus** para desacoplar componentes. Los eventos serán:
`MARKET_DATA`, `SIGNAL_GENERATED`, `ORDER_SUBMITTED`, `ORDER_ACCEPTED`, `ORDER_PARTIAL`, `ORDER_FILLED`, `ORDER_CANCELLED`, `ORDER_UNKNOWN`, `POSITION_CHANGED`, `RISK_REJECTED`.

### Fase 2 — Modelado de Dominio e InstrumentRegistry

* Implementar plazos de liquidación reales (T+0 y T+1) y crear un registro local (`InstrumentRegistry`) que descargue los metadatos desde el broker para no hardcodear información.

```python
class Settlement(Enum):
    CI = "CI"  # T+0
    T1 = "T1"  # T+1 / 24h

@dataclass
class Instrument:
    symbol: str
    market: str
    currency: str
    settlement_options: list[Settlement]
    tick_size: float
    lot_size: int

```

### Fase 3 — Broker Interface y Buying Power

Separar balances contables netos del poder de compra real. El Risk Manager validará el *Buying Power* explícitamente por plazo.

```python
@dataclass
class BuyingPower:
    currency: str
    settlement: Settlement
    available: float
    source: str

class Broker(ABC):
    @abstractmethod
    def get_buying_power(self, currency: str, settlement: Settlement) -> BuyingPower:
        pass

    @abstractmethod
    def create_order(self, symbol: str, side: str, quantity: int, price: float, settlement: Settlement):
        pass

```

### Fase 4 — Trading Calendar

El estado del mercado se evalúa dinámicamente según la hora y el instrumento.

```python
session = calendar.get_session(instrument="GGAL", timestamp=now)
# Posibles estados: PRE_OPEN, AUCTION, REGULAR, CLOSING, CLOSED, SLEEP

```

**Regla para estado `SLEEP`:** Se bloquean *Market Data*, estrategia y el envío de órdenes, **pero continúan habilitados los *health checks* y procesos de reconciliación en background**.

### Fase 5 — Market Data Normalizer

Toda fuente de datos debe pasar por una capa normalizadora antes de inyectarse al Event Bus. La Estrategia consume un único formato inmutable.

```python
@dataclass
class Quote:
    symbol: str
    timestamp: datetime
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    last: float
    volume: int

```

### Fase 6 — OMS: Máquina de Estados, Resiliencia y UNKNOWN

Implementar el ciclo de vida estricto para la ejecución.

```text
             ┌─────────────┐
             │   CREATED   │
             └──────┬──────┘
                    ↓
             ┌─────────────┐
             │   SUBMIT    │
             └──────┬──────┘
                    ↓
              ┌──────────┐
              │ PENDING  │
              └────┬─────┘
                   │
          ┌────────┼─────────┐
          ↓        ↓         ↓
       PARTIAL   FILLED   UNKNOWN
          │                  │
          │                  ↓
          │             RECONCILE
          │             ↙        ↘
          │          FILLED    CANCELLED
          ↓
       FILLED

```

**Políticas del Adapter:**

* **Rate Limiter & Circuit Breaker**: Protegen la conexión con la API del Broker.
* **Retry Policy**: Permitido **solo** en peticiones idempotentes (GET balances, GET quotes).
* **Timeout Policy**: Ante un HTTP 504 / Timeout en un POST `create_order()`, **NO SE REINTENTA**. La orden pasa a `UNKNOWN` y dispara `RECONCILE` para buscar su estado real mediante `GET orders`.

### Fase 7 — Aggressive Limit Policy

Cero órdenes de mercado. Se calcula el precio límite dinámicamente según la profundidad y el tick size real.

```python
price = policy.calculate(
    side=BUY,
    book=orderbook,
    max_slippage=0.002
)
# Internamente: Best Ask -> suma max_slippage -> round_to_tick(instrument.tick_size) -> LIMIT

```

### Fase 8 — Safety Layer: Kill Switch

Mecanismo mandatorio de emergencia:

* Acciones: **`STOP NEW ORDERS` + `CANCEL PENDING ORDERS**`.
* Restricción: **No se cierran posiciones automáticamente** (evita liquidaciones por bugs o falta de liquidez; el cierre en pánico requiere intervención humana).

### Fase 9 — Backtesting Nivel 2 (Orderbook)

El simulador de ejecución consumirá el Orderbook profundo. Al comprar 5,000 nominales, no asume `5000 x Best Ask`. Barre la liquidez matemáticamente nivel por nivel (*Ej: 100@1000, 300@1005, 700@1010...*) para encontrar el precio medio de ejecución real.
El P&L aplica: *Comisión Broker + Derechos de Mercado + Impuestos + Slippage.*

### Fase 10 — MVP (Tracer Bullet)

Lograr que una sola operación de papel / Sandbox atraviese el camino crítico completo:

1. Emisión de `Quote` normalizada al Event Bus.
2. Captura por la Estrategia y generación de `SIGNAL`.
3. Validación de *Buying Power* por plazo y aplicación de `AggressiveLimitPolicy`.
4. Ejecución en API, simulando un Timeout (`UNKNOWN`) para validar el ciclo de recuperación mediante `RECONCILE`.
5. Registro final en la base de datos local.

---

### Estructura Final del Proyecto

```text
argbot/
│
├── strategy/
│   └── pure_strategy.py
│
├── core/
│   ├── engine.py
│   ├── event_bus.py          <-- Pub/Sub central (Desacople)
│   └── calendar.py           <-- TradingCalendar
│
├── oms/
│   ├── order_manager.py      <-- Máquina de estados (UNKNOWN)
│   ├── position_manager.py
│   ├── risk_manager.py       <-- Validador de get_buying_power()
│   ├── limit_policy.py       <-- AggressiveLimitPolicy
│   └── reconciliation.py     <-- Recuperador de estados post-timeout / crasheo
│
├── brokers/
│   ├── base.py               <-- Modelos (Settlement, BuyingPower) e interfaces
│   ├── ppi_adapter.py
│   └── resilience.py         <-- Rate Limiter, Circuit Breaker, Retries controlados
│
├── market_data/
│   ├── normalizer.py         <-- Emite objeto Quote estándar
│   ├── ppi_ws.py
│   └── pyobd_source.py       <-- Integración opcional BYMA Open Data
│
├── registry/
│   └── instruments.py        <-- Descarga dinámica de metadatos (ticks, plazos, lotes)
│
├── safety/
│   └── kill_switch.py        <-- STOP NEW / CANCEL PENDING
│
├── backtesting/
│   ├── engine.py
│   ├── costs.py
│   └── l2_simulator.py       <-- Simulador estricto de caja de puntas (Orderbook)
│
├── config/
│   └── config.yaml
│
└── main.py
