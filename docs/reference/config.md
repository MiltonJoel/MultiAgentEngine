---
title: Configuration
---

# Configuration

`.mae/config.json` holds optional per-repository settings: document paths, gates, role bindings, free-model
endpoints, the iteration ceiling and extra readable directories. Command-line flags take precedence over it,
and it takes precedence over the built-in defaults.

## Where it lives and how it is read

The file is `<repo>/.mae/config.json`. If it is absent the engine uses `{}` and every default. If it is
present it must contain a JSON object; the engine prints `Config: <path>` so you can see it was picked up.

| Problem | Message |
|---|---|
| Invalid JSON | `<path>: <json error>` |
| Not a JSON object | `<path>: must hold a JSON object` |
| Wrong type for a known key | `<path>: "<key>" must be a <type>` |
| `allow_dirs` is not a list of strings | `<path>: "allow_dirs" must be a list of path strings` |
| `agents` maps a role to a non-string | `<path>: "agents" maps role names to CLI names, both strings` |

Unknown keys are ignored, so an older or newer file does not break a run. `max_iterations` is not
type-checked while the file is read; it is checked when the value is used, and a value that is not a
positive integer (including `true` or `false`) fails at startup.

## Keys

| Key | Type | Default | Effect |
|---|---|---|---|
| `spec` | string path | `<repo>/.mae/spec.md` | Architecture document. Relative paths are resolved from the repository. |
| `plan` | string path | `<repo>/.mae/plan.md` | Work queue with the `ENGINE-STATUS` block. Relative paths are resolved from the repository. |
| `gates` | array | pytest then ruff | Commands run after every coder pass. See [Gates](#gates). |
| `agents` | object | coder=`claude`, reviewer=`codex`, auditor=`codex` | Role-to-runner bindings, same syntax as `--agent`. |
| `llm` | object | built-in Ollama and API settings | Endpoints, models and key variables for the free-model runners. See [The `llm` section](#the-llm-section). |
| `max_iterations` | integer | `15` | Ceiling on coder passes. Must be at least 1. |
| `allow_dirs` | array of strings | `[]` | Extra directories the coder may read. |

There is no config key for the run-log directory; use `--log-dir`.

## Precedence

| Setting | Order, highest first |
|---|---|
| `spec`, `plan` | `--spec` / `--plan` → config `spec` / `plan` → `<repo>/.mae/<name>.md` |
| `gates` | any `--gate` → config `gates` → default gates |
| `agents` | `--agent` for that role → config `agents` → `--free` base → `DEFAULT_AGENTS` |
| `llm` model | `RUNNER:model` in a binding → config `llm.<runner>.model` → the runner's `MAE_*_MODEL` variable → built-in default |
| `llm` URL | config `llm.<runner>.url` → the runner's `MAE_*_URL` variable → built-in default |
| `max_iterations` | `--max-iterations` → config `max_iterations` → `15` |
| `allow_dirs` | `--allow-dir` **and** config `allow_dirs` are combined, not overridden |
| log directory | `--log-dir` → `<repo>/.mae/runs` |

`--free` is a base, not an override: it starts every role on `ollama`, then config `agents` and `--agent`
change individual roles. `--agent` flags are applied in order, so the last flag for a role wins.

`allow_dirs` entries are expanded (`~`) and resolved from the directory you launch `mae` in, not from the
repository. Each one must already be a directory or startup fails with
`allow-dir is not a directory: <path>`. The spec's and plan's directories are added automatically; you do
not need to list them.

## Gates

A gate is a command run inside the repository after every coder pass. Its output becomes structured errors
for the coder. Gates run in the order listed.

The default list is:

```json
[
  {"cmd": "pytest -q --tb=line --no-header", "ok_codes": [0, 5]},
  {"cmd": "ruff check . --output-format json"}
]
```

Each entry is either a command string or an object:

| Form | Meaning |
|---|---|
| `"npm test -- --run"` | The command passes only with exit code `0`. |
| `{"cmd": "tsc --noEmit", "ok_codes": [0, 1]}` | The command passes with any code in `ok_codes`. |

Rules:

- `cmd` is split with `shlex.split`, so quoting works.
- `ok_codes` must be a list of integers. Booleans are rejected. The default is `[0]`; an empty list also
  becomes `[0]`.
- `pytest` exits `5` when it collects no tests. That is why the default pytest gate allows `[0, 5]`: a
  repository without tests is not a failing build. If you replace the pytest gate, use the config form
  rather than `--gate` to keep exit code 5 acceptable.
- `--gate` only expresses the string form, so a command passed that way accepts exit code `0` only.
  Repeating `--gate` replaces the entire config list. `--gate ''` disables all gates.
- A relative tool containing a slash (`scripts/check.sh`) is looked up inside the repository at startup;
  anything else is looked up on `PATH`.
- A **configured** gate whose tool is missing is a startup error. A **default** gate whose tool is missing
  is skipped with a message. A tool that disappears or times out during the run counts as red.
- Each gate has a 900-second timeout. Its process group is killed on timeout.
- Gate output is parsed as ruff/eslint JSON first, then as `path:line[:col]: message`, then as the last 800
  characters of the raw output. At most 20 errors are passed to the coder.
- The tester also turns problems in the `ENGINE-STATUS` block into errors, so a broken plan makes the pass
  red instead of silently losing units.

If the gate list is empty and the run otherwise finishes, the exit code is `2` with the caveat
`no gate ran — green checked nothing`.

## The `llm` section

The `llm` object configures the two HTTP runners. Each key is a runner name; each value accepts `url`,
`model` and `key_env`, all strings.

| Runner | `url` default | `model` default | `key_env` default |
|---|---|---|---|
| `ollama` | `http://localhost:11434/v1` | `qwen2.5-coder:7b` | none |
| `api` | none — you must set it | none — you must set it | `MAE_API_KEY` |

Environment variables supply the same endpoint and model settings, and are overridden by the config file:

| Variable | Effect |
|---|---|
| `MAE_OLLAMA_URL` | Ollama endpoint |
| `MAE_OLLAMA_MODEL` | Ollama model |
| `MAE_API_URL` | API endpoint |
| `MAE_API_MODEL` | API model |
| `MAE_API_KEY` | API key, because it is the default `api` `key_env` |
| `MAE_ALLOWED_KEY_ENVS` | Comma-separated extra variable names a config may name as `key_env` |

`key_env` is the **name** of an environment variable, never the key itself. The key is read from your
environment at run time. If you change `key_env`, `MAE_API_KEY` is no longer read.

### What `key_env` may name

A repository's config also chooses the endpoint, so the engine default-denies ambient credentials. A
`key_env` value must be:

- a valid environment variable name (`[A-Za-z_][A-Za-z0-9_]*`), and
- one of:
    - any name starting with `MAE_`;
    - a known model-provider key;
    - a name you listed in `MAE_ALLOWED_KEY_ENVS` in your own environment.

The known model-provider names are:

```text
OPENROUTER_API_KEY  GROQ_API_KEY       OPENAI_API_KEY      GEMINI_API_KEY
GOOGLE_AI_API_KEY   MISTRAL_API_KEY    DEEPSEEK_API_KEY    TOGETHER_API_KEY
FIREWORKS_API_KEY   CEREBRAS_API_KEY   XAI_API_KEY         PERPLEXITY_API_KEY
DEEPINFRA_API_KEY   NVIDIA_API_KEY     SAMBANOVA_API_KEY   MOONSHOT_API_KEY
DASHSCOPE_API_KEY   ZHIPUAI_API_KEY    OLLAMA_API_KEY      LITELLM_API_KEY
```

`MAE_ALLOWED_KEY_ENVS` is read from your environment, not from the repository, so a cloned repo cannot
allowlist a variable such as `GITHUB_TOKEN` or `AWS_SECRET_ACCESS_KEY` by itself.

### Validation and safety

At startup the engine checks that:

- every runner is `ollama` or `api`;
- every setting is one of `url`, `model` and `key_env`, and is a string;
- `url`, when set, is an `http://` or `https://` URL;
- `key_env` passes the allow-list above.

At run time a key is sent only over HTTPS, except to `localhost`, `127.0.0.1` or `::1`. Model endpoints are
never followed through HTTP redirects. Keys are scrubbed from error messages and from every run-log field.
The engine prints which host each key goes to before starting; if the host came from the repository's own
config, the line starts with `WARNING`.

!!! warning "Read a cloned repository's config first"
    Running `mae` in someone else's repository sends your key to the endpoint that repository's
    `.mae/config.json` names. Read the file before you run. The dashboard's automatic runner check never
    sends the key to a repository-chosen endpoint.

## Complete example

```json
{
  "spec": "docs/architecture.md",
  "plan": "docs/queue.md",
  "gates": [
    "npm test -- --run",
    {"cmd": "tsc --noEmit", "ok_codes": [0, 1]},
    {"cmd": "pytest -q --tb=line", "ok_codes": [0, 5]}
  ],
  "agents": {
    "coder": "claude:opus",
    "reviewer": "codex:high",
    "auditor": "api:qwen/qwen-2.5-coder-32b-instruct:free"
  },
  "llm": {
    "ollama": {
      "url": "http://localhost:11434/v1",
      "model": "qwen2.5-coder:14b"
    },
    "api": {
      "url": "https://openrouter.ai/api/v1",
      "model": "qwen/qwen-2.5-coder-32b-instruct:free",
      "key_env": "OPENROUTER_API_KEY"
    }
  },
  "max_iterations": 20,
  "allow_dirs": ["~/code/reference-lib", "../shared-lib"]
}
```

A minimal file that only changes the gates and the iteration ceiling:

```json
{
  "gates": ["npm test", "eslint . -f json"],
  "max_iterations": 30
}
```

## Related pages

- [Command line](cli.md) — the flags that override these keys.
- [Roles, runners and prompts](runners.md) — runner knobs, defaults and timeouts.
- [Free models](../guide/free-models.md) — Ollama and hosted OpenAI-compatible endpoints.
- [Security](../security.md) — the full threat model for configs, gates and keys.
