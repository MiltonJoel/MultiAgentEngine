# MultiAgentEngine

Un bucle de LangGraph que dirige dos agentes de programación — la CLI `claude` y la
CLI `codex` — contra un repositorio hasta que una especificación de arquitectura
queda implementada, testeada y auditada.

Tú escribes la especificación y la cola de trabajo. El motor ejecuta los agentes, los
valida con un paso de tests determinista, mantiene la cola de trabajo al día y se
detiene cuando no queda nada abierto.

---

## Ejemplo de uso

Una sesión completa, de cero a una rama con el trabajo hecho. Copia y pega:

```bash
# 1. Instalar el motor (una sola vez)
cd ~/Desktop/MultiAgentEngine && pip install -e .

# 2. Preparar el proyecto que quieres construir
mkdir -p ~/code/myproject/.mae && cd ~/code/myproject
git init -q 2>/dev/null; echo ".mae/runs/" >> .gitignore

cat > .mae/spec.md <<'EOF'
# Spec
Un servicio FastAPI con login por email/contraseña y límite de 100 peticiones/minuto
por usuario. Cada endpoint tiene un test de pytest.
EOF

cat > .mae/plan.md <<'EOF'
<!-- ENGINE-STATUS
Login flow: TODO
Rate limiting: TODO
-->
EOF

# 3. Ejecutar
mae

# 4. Revisar el resultado (el comando exacto se imprime al final)
git log --oneline --all --graph | head -20
```

Variantes habituales:

```bash
mae --repo ~/code/myproject                              # desde cualquier carpeta
mae --agent reviewer=claude --agent auditor=claude       # solo claude, sin codex
mae --gate "npm test" --gate "eslint . -f json"          # proyecto JavaScript
mae --gate ''                                            # sin tests ni lint
mae --allow-dir ~/code/reference-lib                     # el coder puede leer otro repo
mae --max-iterations 30                                  # plan con muchas unidades
mae --spec docs/arquitectura.md --plan docs/cola.md      # documentos fuera de .mae/
mae --no-git                                             # sin rama ni commits
```

Para pedir más trabajo después, añade o reabre una línea en `.mae/plan.md` y vuelve a
ejecutar `mae`:

```bash
sed -i 's/^Rate limiting: DONE/Rate limiting: TODO — por usuario, no por IP/' .mae/plan.md
mae
```

---

## Inicio rápido

### 1. Instalar una vez

```bash
cd MultiAgentEngine
pip install -e .          # o: pipx install -e .  → deja `mae` en el PATH
```

Comprueba las herramientas que usa:

```bash
which mae claude codex pytest ruff git
```

| Herramienta | Rol | Notas |
|---|---|---|
| CLI `claude`, con sesión iniciada | el programador (coder) | en el `PATH`, ya autenticada |
| CLI `codex`, con sesión iniciada | revisor y auditor | en el `PATH`, ya autenticada — o asigna esos roles a `claude` |
| `pytest` | gate de tests por defecto | se ejecuta dentro del repo **objetivo**; cámbialo con `--gate` |
| `ruff` | gate de lint por defecto | se omite si no está instalado. **No uses la versión de snap** — ver [Solución de problemas](#solución-de-problemas) |
| `git` | checkpoints en el repo objetivo | opcional — sin él no hay punto de restauración |

Aquí no se configuran claves de API. El motor usa CLIs en las que ya iniciaste sesión.

### 2. Pon dos archivos en el repo que quieres construir

```bash
cd ~/code/myproject
mkdir -p .mae
echo ".mae/runs/" >> .gitignore
```

**`.mae/spec.md`** — lo que el sistema debe ser. Cualquier formato, prosa incluida. El
motor nunca lo edita.

```markdown
# Spec
Un servicio FastAPI con login por email/contraseña, recuperación de contraseña con
token enviado por email y límite de 100 peticiones/minuto por usuario. Postgres vía
SQLAlchemy. Cada endpoint tiene un test de pytest.
```

**`.mae/plan.md`** — la cola de trabajo. El bloque del principio es lo que el motor usa
para decidir qué hacer. Una línea por unidad de trabajo:

```markdown
<!-- ENGINE-STATUS
Login flow: TODO
Password reset: TODO
Rate limiting: TODO
-->

# Notas
Todo lo que necesite más de una línea va aquí abajo. El motor nunca lo interpreta,
pero el coder lo lee.
```

#### ¿Solo tienes la tarea, sin lista de TODOs?

Es lo normal en la primera ejecución. Escribe la tarea y **no pongas el bloque**:

```markdown
# Tarea
Implementar el servicio descrito en spec.md: login, recuperación de contraseña y
límite de peticiones por usuario.
```

En la primera pasada el coder lee la especificación y la tarea, crea el bloque
`ENGINE-STATUS` con una línea `TODO` por unidad y las va pasando a `DONE`. Al arrancar
verás `This run: no task list yet — the coder writes it from the spec and plan`.

A partir de la segunda ejecución el bloque ya existe: revísalo, corrige nombres o
divide unidades si no te gusta cómo quedó, y sigue con el flujo normal. Si prefieres
controlar la división desde el principio, escribe tú las líneas `TODO`.

### 3. Ejecútalo

```bash
mae --repo ~/code/myproject        # o entra en la carpeta y ejecuta: mae
```

Antes de arrancar cualquier agente imprime lo que va a hacer — pulsa Ctrl-C si algo
está mal:

```
Plan: TODO=3
This run: Login flow, Password reset, Rate limiting
Log: ~/code/myproject/.mae/runs/20260913-101500.jsonl
Agents: coder=claude, reviewer=codex, auditor=codex
Gates: pytest -q --tb=line --no-header ; ruff check . --output-format json
[git] working on branch engine/20260913-101500
```

Después itera (coder → tests → revisor o auditor → coder …) hasta que todas las líneas
estén en `DONE` o `BLOCKED` y la auditoría salga limpia. Una ejecución puede durar
horas; déjala correr.

### 4. Revisa lo que hizo

Al terminar imprime el comando exacto:

```bash
git -C ~/code/myproject log --oneline <base>..engine/20260913-101500   # un commit por pasada
git -C ~/code/myproject diff <base>                                     # la ejecución completa
```

Haz merge de la rama si te gusta, o `git reset --hard <sha>` a cualquier pasada si no.

### 5. Pide más

Añade o reabre una línea del bloque y vuelve a ejecutar `mae`:

```
Rate limiting: TODO — el límite es por usuario, no por IP
Audit log for logins: TODO
```

Ese es todo el flujo diario: **edita el bloque, ejecuta `mae`, revisa la rama.**

---

## Interfaz web — `mae-ui`

Todo lo anterior también se hace desde el navegador:

```bash
mae-ui --repo ~/code/myproject          # abre http://127.0.0.1:8765/?token=…
mae-ui --port 9000 --no-browser         # otro puerto; imprime la URL sin abrirla
```

- **Mission** — el grafo coder → tester → reviewer/auditor con el nodo activo en vivo,
  pasadas, errores de los gates, resultado de la auditoría y coste; elegir la CLI de cada
  rol, gates, tope de pasadas y `--no-git`; lanzar y detener (detener = Ctrl-C: se
  conservan los checkpoints); consola en vivo filtrable por agente.
- **Queue & spec** — las unidades del bloque `ENGINE-STATUS` en columnas por estado:
  añadir, cambiar de estado o borrar sin tocar el resto del plan; editores de `spec.md`,
  `plan.md` y `.mae/config.json` (<kbd>Ctrl</kbd>+<kbd>S</kbd> guarda).
- **History** — cada log de `.mae/runs/` con estado, pasadas y coste; su línea de tiempo y
  los commits de checkpoint de la rama, con el diff de cada uno.

La UI lanza el mismo `mae` como subproceso, una ejecución a la vez. Escucha solo en
`127.0.0.1` y cada arranque genera un token que la API exige: abre la URL que imprime.

---

## Los dos archivos que son tuyos

Viven **en el repo objetivo**, no en este. El motor no incluye documentos propios, así
que una sola instalación sirve para cualquier número de proyectos.

```
<repo>/.mae/
├── spec.md        --spec (por defecto)
├── plan.md        --plan (por defecto)
├── config.json    ajustes por repo (opcional)
├── prompts/       prompts personalizados por repo (opcional)
└── runs/          logs JSONL de cada ejecución (añádelo a .gitignore)
```

También pueden estar en otro sitio — con `--spec`/`--plan`, o con las claves
`spec`/`plan` en `config.json`.

### `spec.md` — la arquitectura (congelada)

Requisitos, interfaces, modelos de datos, diagramas, invariantes. Solo tú lo editas. Se
envía primero en cada prompt y no cambia entre iteraciones, así que queda en el prefijo
cacheado del modelo y cuesta ~10% después de la primera llamada.

### `plan.md` — la cola de trabajo (cambia constantemente)

Contiene el **bloque de estado**, un comentario HTML que el motor interpreta:

```
<!-- ENGINE-STATUS
Login flow: DONE
Password reset: IN_PROGRESS
Rate limiting: TODO — cambié la ventana a 60s
Payment webhook: BLOCKED faltan las claves sandbox del proveedor
-->
```

Todo lo que está fuera del bloque es tuyo; al coder se le indica que no lo toque.

| Estado | Significado | Comportamiento del motor |
|---|---|---|
| `TODO` | sin empezar, o lo reabriste | sigue trabajando |
| `IN_PROGRESS` | empezado | sigue trabajando |
| `DONE` | el código está escrito | no lo toca |
| `BLOCKED` | necesita a una persona (credenciales, una decisión de producto) | deja de reintentar — escribe el motivo en la línea |

Los estados se escriben siempre en inglés y en mayúsculas; el nombre de cada unidad
puede estar en el idioma que quieras.

Reglas importantes:

- Un elemento por línea, `<nombre>: <ESTADO>`. El texto después del estado es una nota
  libre y se conserva.
- El nombre puede contener dos puntos y guiones (`Paso 6 — Pedidos: máquina de estados: DONE`
  se interpreta bien).
- El orden no importa — el bloque es un índice, no una secuencia.
- Una línea que no se puede interpretar **se avisa al arrancar, no se descarta en silencio**.
- Borrar `<!-- ENGINE-STATUS` o `-->` borra la memoria del motor sobre lo que está hecho.
- Una línea `TODO — audit: …` la reabrió el auditor; la nota describe el problema. No la
  pases a `DONE` a mano sin arreglarlo: la siguiente auditoría la volvería a reabrir.

### Añadir y cambiar trabajo

**La especificación es el contenido; el bloque de estado es el enrutamiento.** El coder
lee toda la especificación, pero solo se ejecuta si el bloque tiene algo abierto. Editar
solo la prosa, con todo en `DONE`, no cambia nada.

| Quieres | Haz esto |
|---|---|
| Un cambio pequeño | Una línea: `Reintentos idempotentes en el job de sync: TODO` |
| Corregir trabajo terminado | Reábrelo con una nota: `Rate limiting: TODO — por usuario, no por IP`. El coder entiende un `TODO` con código ya existente como "reconcilia con la nota nueva". |
| Un subsistema nuevo | Descríbelo en la especificación (o debajo del bloque) y añade su línea. Usa el mismo nombre en ambos sitios para que el agente encuentre el detalle. |
| Un bloque más corto | Borra líneas `DONE` antiguas. Deja al menos una — un bloque vacío se lee como "nunca empezado" y el coder rehace el plan desde cero. |

---

## Flags

| Flag | Significado |
|---|---|
| `--repo DIR` | el repositorio que se construye (por defecto `.`) |
| `--spec FILE` | documento de arquitectura (por defecto `<repo>/.mae/spec.md`) |
| `--plan FILE` | cola de trabajo (por defecto `<repo>/.mae/plan.md`) |
| `--allow-dir DIR` | directorio extra que el coder puede leer; repetible |
| `--gate CMD` | comando de validación tras cada pasada del coder; repetible, reemplaza la lista del config; `--gate ''` desactiva todos los gates |
| `--agent ROLE=CLI` | asigna un rol a una CLI, p. ej. `--agent reviewer=claude`; repetible |
| `--max-iterations N` | tope de pasadas del coder (por defecto 15); debe superar el número de unidades abiertas |
| `--log-dir DIR` | directorio del log JSONL (por defecto `<repo>/.mae/runs`) |
| `--no-git` | omite la rama por ejecución y los commits por iteración |

`mae` sale con código 0 solo si la cola queda cerrada, los gates en verde y la auditoría
limpia; 2 si todo eso se cumple pero hace falta que una persona mire (hay unidades
`BLOCKED`, o no se ejecutó ningún gate y el verde no comprobó nada); 1 en cualquier otro
caso (útil en CI), 130 con Ctrl-C. Antes de arrancar comprueba que las CLIs de cada rol y
las herramientas de los gates configurados existen (un gate relativo como
`scripts/check.sh` se busca dentro del repo), que los prompts de `.mae/prompts/` se
pueden rellenar, y que el repo no está a mitad de un merge/rebase ni con un
`index.lock` huérfano. Un gate configurado cuya herramienta falta es un error, no un
aprobado silencioso, y uno que desaparece o se pasa de tiempo a mitad de la ejecución
cuenta como rojo. Si un commit de checkpoint falla (identidad de git, un hook, la firma)
la ejecución se detiene en vez de seguir sin puntos de rollback.

Después de cada pasada del coder el tester también revisa el bloque de estado: un `-->`
dentro de una nota, un segundo bloque, un estado mal escrito o una unidad que estaba
abierta al empezar y ha desaparecido ponen la iteración en rojo, para que el coder lo
arregle en vez de que la unidad se pierda en silencio.

Una sola ejecución por archivo de plan: una segunda sale inmediatamente en vez de
mezclar sus escrituras en el mismo bloque de estado.

`--allow-dir` importa más de lo que parece: el coder se ejecuta con `--repo` como
directorio de trabajo y **no puede leer fuera de él** sin esto. Los directorios de la
especificación y del plan se añaden automáticamente; cualquier código de referencia lo
añades tú:

```bash
mae --repo ~/code/myproject --allow-dir ~/code/reference-lib
```

---

## Ajustes por repo — `.mae/config.json`

Opcional. Evita escribir flags en cada ejecución. Los flags de la CLI tienen prioridad.

```json
{
  "spec": "docs/architecture.md",
  "plan": "docs/queue.md",
  "gates": [
    "npm test -- --run",
    {"cmd": "tsc --noEmit", "ok_codes": [0, 1]},
    "eslint . -f json"
  ],
  "agents": {"reviewer": "claude", "auditor": "claude"},
  "max_iterations": 20,
  "allow_dirs": ["~/code/reference-lib"]
}
```

- `spec` / `plan` — relativos al repo; ponlos solo para sacarlos de `.mae/`.
- `gates` — se ejecutan en orden. Cada entrada es un comando (código de salida 0 = pasa),
  o `{"cmd", "ok_codes"}` cuando otros códigos también cuentan como pasar. `[]` = sin
  gates. Si se omite = pytest y luego ruff.
- `agents` — los roles son `coder`, `reviewer`, `auditor`; las CLIs son `claude`, `codex`.

Un gate pasado con `--gate` solo acepta el código de salida 0. pytest sale con 5 cuando
no encuentra tests, así que en un repo todavía sin tests usa la forma del config
`{"cmd": "pytest -q --tb=line", "ok_codes": [0, 5]}` (los gates por defecto ya lo hacen).

---

## El bucle

```
 spec + cola → CODER → TESTER → ¿tests pasan? ── no → REVIEWER → CODER
                                     │
                                     sí
                                     ↓
                     ¿quedan elementos abiertos? ── sí → CODER
                                     │
                                     no
                                     ↓
                      ¿última auditoría limpia? ── sí → FIN
                                     │
                                     no
                                     ↓
                                  AUDITOR ── ¿hallazgos? ── no → FIN
                                                  │
                                                  sí → reabre esas unidades (TODO) → CODER
```

Si dos pasadas seguidas del coder no cambian ningún archivo (con git activo), la ejecución
se detiene en vez de seguir pagando por pasadas vacías.

Toda vuelta al coder se detiene en `--max-iterations`. Es un tope, no un objetivo — el
bucle termina en cuanto no queda nada abierto y la auditoría sale limpia.

**CODER** (`claude`, puede editar archivos y ejecutar comandos, 90 min de presupuesto) — implementa los
elementos abiertos, escribe tests para cada uno y ejecuta solo esos tests; pasa la línea a
`DONE` cuando pasan, así una sesión interrumpida no pierde nada. Lo que necesita datos,
red, credenciales o una decisión humana va a `BLOCKED` con el motivo, nunca a `DONE`. La
suite completa la ejecuta el TESTER. Es el único agente que escribe código.

**TESTER** (sin LLM, gratis) — ejecuta los gates en orden y convierte su salida en
errores estructurados, con un máximo de 20:

```json
[{"file": "app/auth/login.py", "line": 84, "description": "assert 0 == 2"}]
```

El parseo prueba primero JSON de ruff/eslint, luego `ruta:línea[:col]: mensaje` (pytest
`--tb=line`, ruff, mypy, tsc, eslint compact, `go vet`, gcc) y por último los 800 últimos
caracteres de la salida en bruto. Una herramienta desconocida da un error poco preciso,
pero nunca rompe el bucle. Una herramienta no instalada se omite. **Hace commit después
de cada pasada**, también de las que fallan.

**REVIEWER** (`codex`, solo lectura) — se ejecuta **solo cuando el build falla**. Ve la
especificación, la cola, los errores y el diff de la pasada que acaba de fallar; devuelve
las causas raíz en JSON:

```json
{"problems": [{"file": "...", "line": 84, "description": "por qué falla",
               "suggested_fix": "qué cambiar"}]}
```

**AUDITOR** (`codex`, solo lectura) — se ejecuta cuando los tests pasan y la cola está
cerrada. El coder se corrige a sí mismo, así que el auditor lee el diff de toda la
ejecución contra la especificación y reporta unidades marcadas `DONE` que están a medias,
contradicen la especificación, no tienen test para un comportamiento requerido o afirman
un criterio de salida imposible de cumplir sin datos. Se limita a las unidades que estaban
abiertas al empezar la ejecución.

Cada hallazgo indica su unidad, y **el motor la reabre**: la línea pasa a
`TODO — audit: <hallazgo>`. El coder la arregla, los tests vuelven a pasar y **la
auditoría se repite** hasta salir limpia. Solo una auditoría sin hallazgos termina la
ejecución. Las unidades `BLOCKED` no se reabren.

Los diffs enviados al revisor y al auditor se limitan a 20 000 caracteres, con `--stat`
primero.

---

## Roles, CLIs y prompts

Qué CLI cumple cada rol es configuración:

```bash
mae --agent reviewer=claude --agent auditor=claude    # solo claude, sin necesidad de codex
```

Todas las funciones que lanzan una CLI aceptan los mismos argumentos (`write`,
`add_dirs`, `schema`, `timeout`), así que añadir una tercera CLI es una función más una
entrada en `RUNNERS` en `engine/cli.py`. Dos limitaciones de las que el motor avisa:
`codex` no tiene `--add-dir`, y `claude` no tiene esquema de salida, así que se le pide
JSON en el prompt.

Los prompts son archivos: `engine/prompts/{coder,reviewer,auditor}.md`. Para cambiar uno
en un solo proyecto, cópialo a `<repo>/.mae/prompts/<rol>.md` y edítalo — el motor avisa
cuando usa uno personalizado. Los marcadores son `{nombre}`; uno desconocido falla al
arrancar con la lista de nombres disponibles; una llave literal debe duplicarse `{{ }}`.

---

## Checkpoints

Antes de la primera pasada el motor crea una rama en el repo objetivo y hace commit de
tu trabajo sin commitear como punto de partida, así nada tuyo se pierde ni se mezcla con
los diffs del agente:

```
[git] working on branch engine/20260913-101500
[git] 37aeaf3 engine: iteration 1 — 3 error(s)
[git] a91c02b engine: iteration 2 — green
```

```bash
git -C <repo> diff HEAD~1          # lo que hizo la última pasada
git -C <repo> reset --hard <sha>   # volver a cualquier pasada
```

Los checkpoints nunca incluyen `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*`,
`__pycache__/` ni `.mae/runs/`, aunque el `.gitignore` del repo los olvide.

¿No es un repo git? La ejecución funciona igual, sin protección, y lo avisa. `--no-git`
lo desactiva a propósito.

---

## El log de ejecución

Un archivo JSONL por ejecución en `<repo>/.mae/runs/`:

```bash
tail -f <repo>/.mae/runs/*.jsonl                                                      # en vivo
jq -r 'select(.event=="agent") | "\(.label) \(.seconds)s $\(.cost_usd)"' <repo>/.mae/runs/*.jsonl
jq 'select(.event=="finish") | .usage' <repo>/.mae/runs/*.jsonl
```

Eventos: `start`, `agent` (por llamada a la CLI: segundos, tokens, coste), `gate`,
`checkpoint`, `limit` (esperas por límite de uso), `finish` / `aborted`. Una ejecución
interrumpida igualmente escribe `aborted` y conserva sus checkpoints.

---

## Coste y límites

El prompt del coder pone primero el contenido estable (especificación + instrucciones,
cacheado a ~10%) y al final el variable (cola + hallazgos, precio completo). Comprueba
que la caché funciona en la línea de progreso — un `cache_read` alto es lo que buscas:

```
[coder] done — in=1204 cache_read=2610 out=880 cost=$0.0431
```

Solo las respuestas finales de los agentes entran en el estado del grafo, nunca sus
transcripciones; el tester no cuesta nada; el revisor solo se ejecuta cuando algo falla
y el auditor solo con la cola cerrada (repitiéndose hasta salir limpio).

Si una CLI se detiene por un límite de uso, el motor espera hasta la hora de reinicio
que aparece en su salida (30 min si no hay ninguna), hasta 4 veces por llamada:

```
[coder] limit reached — sleeping 47 min, resuming ~15:32
```

Las esperas de más de 6 horas se rechazan indicando la fecha de reinicio — vale la pena
esperar un límite de sesión, no un tope mensual. Ctrl-C mata todo el grupo de procesos
del agente, y la siguiente ejecución continúa desde el bloque de estado.

---

## Solución de problemas

Los mensajes del motor están en inglés; aquí aparecen tal cual para que puedas buscarlos.

| Síntoma | Causa y solución |
|---|---|
| `spec file not found: <repo>/.mae/spec.md` | Créalo, o apunta `--spec` / `"spec"` al tuyo. La versión `plan` de este error imprime un bloque inicial para copiar. |
| `has nothing left open — every item is DONE or BLOCKED` | Funciona como debe. Añade una línea `TODO` o reabre una. |
| `WARNING: unreadable status line, it will be ignored` | Un estado inventado (`PENDIENTE`) o falta de dos puntos. Corrige la línea. |
| Escribiste una sección nueva pero no se ejecuta nada | La prosa no participa en el enrutamiento. Añade una línea `<nombre>: TODO` dentro del bloque. |
| `unknown CLI 'x' for coder` / `unknown role` | Error de escritura en `--agent` o `"agents"`. Roles: coder, reviewer, auditor. CLIs: claude, codex. |
| `another run already holds <plan>` | Hay otra ejecución sobre ese plan. Espera o termínala. |
| El lint pasa en todo | `ruff` de **snap** no puede escribir su caché en `/tmp` y devuelve `[]` con código 0. `snap remove ruff && pip install ruff`, o mantén el repo objetivo dentro de `$HOME`. |
| `codex ... failed to refresh available models` | Inofensivo; el motor oculta la línea de log de 325 KB. `npm i -g @openai/codex@latest` lo arregla. |
| El coder no actualizó el plan | No podía acceder al archivo. Los directorios de spec y plan se permiten automáticamente; cualquier otro necesita `--allow-dir`. |
| El coder deja las unidades en `IN_PROGRESS` porque "no puede ejecutar pytest" | Versión anterior del motor: `claude -p` en `acceptEdits` rechaza Bash. Ahora el coder recibe `--allowedTools Bash`. |
| `Stopped early: 2 coder passes in a row changed nothing` | El coder no avanza. Lee su última salida: suele pedir algo (permiso, dato, decisión). |
| El bucle termina con unidades abiertas | Llegó a `--max-iterations`. Súbelo por encima del número de unidades abiertas y vuelve a ejecutar — continúa donde lo dejó. |
| `[auditor] reopened: …` | El auditor encontró problemas en unidades `DONE`; el motor las volvió a `TODO` con el hallazgo como nota. Es lo esperado: el coder las arregla y se audita otra vez. |
| Termina en el tope con `audit_clean=False` | El auditor sigue encontrando problemas. Lee las notas `audit:` del plan y los `problems` impresos al final; corrige la especificación si pide algo imposible, o marca la unidad `BLOCKED`. |

---

## Buenas prácticas

- Haz commit de `<repo>/.mae/` junto con su proyecto; añade `.mae/runs/` al `.gitignore`.
- El bloque de estado es el único estado que sobrevive entre ejecuciones; si un agente lo
  estropea, `git diff` te lo muestra.
- Borra las ramas de ejecuciones antiguas una vez fusionadas o descartadas:
  `git -C <repo> branch -D $(git -C <repo> branch --list 'engine/*')`.

---

## Trabajar en el propio motor

```
MultiAgentEngine/
├── pyproject.toml       dependencias y el comando `mae`
├── conftest.py          añade la raíz del proyecto a sys.path para pytest
├── engine/
│   ├── main.py          parseo de la CLI, config, validación, bloqueo del plan, run()
│   ├── graph.py         parseo del bloque de estado, enrutamiento, grafo de LangGraph
│   ├── state.py         GraphState
│   ├── cli.py           funciones para claude/codex (RUNNERS), esperas por límite de uso
│   ├── git.py           rama por ejecución, checkpoints, diffs
│   ├── prompts.py       carga de prompts con personalización por repo
│   ├── prompts/         coder.md, reviewer.md, auditor.md
│   ├── runlog.py        log JSONL, totales de coste y tokens
│   ├── ui.py            servidor de `mae-ui` (stdlib), API con token
│   ├── ui.html          la página de `mae-ui`, sin dependencias
│   └── nodes/
│       ├── coder.py     agente de implementación
│       ├── tester.py    gates deterministas y parseo de su salida
│       └── reviewer.py  revisor y auditor (esquema compartido)
└── tests/
    ├── test_engine.py   parseo del plan, enrutamiento, parseo de salidas, log
    ├── test_modular.py  rol→CLI, prompts, documentos, gates, diffs, config
    ├── test_ops.py      checkpoints, bloqueo del plan, pasadas vacías
    └── test_ui.py       API de la UI, token, ejecución completa con una CLI simulada
```

```bash
python3 -m pytest -q && ruff check .
```

Nada dentro de `engine/` es específico de un proyecto. Si te ves editándolo para
apuntar a un proyecto, eso pertenece al `.mae/` del repo objetivo.
