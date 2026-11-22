---
title: Free models
---

# Free models

Run the loop without a Claude or Codex subscription, using a model on your own machine through Ollama or a
hosted OpenAI-compatible endpoint with a free tier. This page covers the built-in agent behind those runners,
the Ollama setup, the `api` runner, how API keys are handled, and what to expect from smaller models.

## The built-in agent

The `ollama` and `api` runners are one agent, implemented in `engine/llm.py` with the standard library only.
It sends chat requests to `<url>/chat/completions` and gives the model tools, so it can look at the repository
and, for the coder, change it and run the tests.

| Tools | Available to | What they do |
|---|---|---|
| `list_files`, `read_file`, `search` | every role | list a directory, read a file, search files with a Python regular expression |
| `write_file`, `replace_in_file` | the coder only | create or overwrite a file; replace one exact occurrence of a string |
| `run_command` | the coder only | run a shell command in the repository root (`pytest`, a linter, a formatter) |

The reviewer and the auditor run with `write=False`, so they get only the read tools. The coder is the only
role that writes.

### Workspace confinement

File tools only reach paths whose real location (symlinks followed) is inside the repository. For the coder,
a directory given with `--allow-dir`, and the spec's and the plan's own directories, are added automatically;
the reviewer and the auditor are read-only and confined to the repository alone. A path outside that set fails
with `... is outside the repository and the allowed directories`. Directory walks skip `.git`, `node_modules`,
virtual environments and caches, and do not follow a symlink out of the workspace.

!!! warning "run_command is not confined"
    The file tools are confined, but `run_command` is a real shell running with your user's rights. It can
    read or change anything your account can. See the [threat model](../security.md) before you run the
    engine on a repository you did not write.

### Limits

- A model call that keeps using tools without finishing stops after 200 model turns with
  `... did not finish within 200 model turns`.
- Each tool result is kept to 10,000 characters. Older results are shortened as the conversation grows, so a
  small local context does not overflow.
- `run_command` is killed when it exceeds 900 seconds or when the pass runs out of time.
- Temperature is fixed at 0.2.
- If a model writes its tool call as JSON text instead of using the tool-call field, the engine still
  recognises it and runs the call.

## Ollama, step by step

Ollama runs models on your machine. It needs no account and no key.

1. **Install Ollama** from [ollama.com/download](https://ollama.com/download). On Linux you can use
   `curl -fsSL https://ollama.com/install.sh | sh`.

2. **Download a model that handles tools.** The default is `qwen2.5-coder:7b` (about 5 GB). With 16 GB of
   RAM or more, `qwen2.5-coder:14b` is a better choice for the coder.

   ```bash
   ollama pull qwen2.5-coder:7b
   ```

3. **Raise the context length.** Ollama's default context is too short for the prompts the loop sends, and a
   model that cannot see the spec and the plan will not do useful work. Set `OLLAMA_CONTEXT_LENGTH` for the
   server:

   ```bash
   OLLAMA_CONTEXT_LENGTH=32768 ollama serve
   ```

   If Ollama already runs as a background service, set the variable in the service's environment and restart
   the service instead.

4. **Run the engine in your repository.** `--free` binds every role to the local model.

   ```bash
   cd ~/code/myproject
   mae --free
   ```

Before it starts, `mae` checks that Ollama answers and that the model is downloaded. If not, it stops with a
message such as `Ollama is not reachable at http://localhost:11434/v1 (...)`, or, when the model is missing,
`Ollama has no model 'qwen2.5-coder:7b' — run ollama pull qwen2.5-coder:7b`.

### Changing the model

The model is chosen per role. The `ollama:<model>` form on `--agent` wins over everything else.

```bash
mae --free --agent coder=ollama:qwen2.5-coder:14b     # a bigger model for the coder only
export MAE_OLLAMA_MODEL=llama3.1:8b                   # the default model for every ollama role
export MAE_OLLAMA_URL=http://other-machine:11434/v1   # Ollama running on another machine
```

The same settings can be made in `.mae/config.json`:

```json
{
  "llm": {"ollama": {"url": "http://other-machine:11434/v1", "model": "llama3.1:8b"}}
}
```

The precedence is: the model in `--agent`, then `llm.ollama` in the repository config, then `MAE_OLLAMA_*`
in your environment, then the built-in default. `--free` only changes which runner each role uses;
`--agent` and the `agents` key of the config still change single roles.

!!! note
    A repository's config can point the `ollama` runner at another machine, and then the prompts and code go
    there. Read the config before you run `mae` in someone else's repository.

## The `api` runner with OpenRouter

The `api` runner talks to any OpenAI-compatible service: OpenRouter, Groq, Google AI Studio, a LiteLLM proxy,
or your own server. Several of them have a free tier. Free tiers have per-minute limits, which the engine
handles by retrying (see [Retries](#retries)).

This example uses OpenRouter. Put the endpoint and the model in the repository's `.mae/config.json`:

```json
{
  "llm": {
    "api": {
      "url": "https://openrouter.ai/api/v1",
      "model": "qwen/qwen-2.5-coder-32b-instruct:free",
      "key_env": "OPENROUTER_API_KEY"
    }
  }
}
```

Set the key in your shell, not in a file, and run the engine with the `api` runner for each role:

```bash
export OPENROUTER_API_KEY=...        # your key
mae --agent coder=api --agent reviewer=api --agent auditor=api
```

Free model names change. Check the model id on the provider's site before you rely on it.

At startup `mae` prints where the key goes. Because this config chose the endpoint, the line is a warning:

```text
WARNING: api: sends $OPENROUTER_API_KEY to openrouter.ai, an endpoint chosen by this repo's .mae/config.json — make sure you trust it
```

If you set the endpoint yourself (`MAE_API_URL` in your environment) instead of in the repository's config,
the same line is printed without the `WARNING:` prefix.

The model has to be set somewhere. Without one, the runner stops with
`the api runner has no model — use api:<model> or set llm.api.model`.

### Settings

| Setting | Config key | Environment variable | Default |
|---|---|---|---|
| Endpoint | `llm.api.url` | `MAE_API_URL` | none; the runner needs one |
| Model | `llm.api.model` | `MAE_API_MODEL` | none; the runner needs one |
| Key variable name | `llm.api.key_env` | none | `MAE_API_KEY` |

The model can also be given on the command line, as in `--agent coder=api:<model>`. The full list of keys is
in the [configuration reference](../reference/config.md).

## Key handling

The engine follows these rules for model keys. They exist because a repository's `.mae/config.json` can choose
the endpoint, and the key is then sent there.

- **The key is read only from the environment variable named by `key_env`.** It is never read from a file in
  the repository and never written to a file by the engine.
- **`key_env` must be a model key.** It has to be a `MAE_*` name, one of the known provider variables
  (`OPENROUTER_API_KEY`, `GROQ_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `MISTRAL_API_KEY`, and others), or a
  name you allowed in your own environment with `MAE_ALLOWED_KEY_ENVS`. Any other name, such as `GITHUB_TOKEN`,
  stops `mae` with `llm.api.key_env: ... is not a known model API key variable`. A repository cannot set
  `MAE_ALLOWED_KEY_ENVS`.

  ```bash
  export MAE_ALLOWED_KEY_ENVS=MY_PROVIDER_KEY     # comma-separated, read from your environment only
  ```

- **`url` must be `http://` or `https://`.**
- **A key is sent only over `https://`.** The one exception is a loopback address (`localhost`, `127.0.0.1`,
  `::1`), which may use plain `http://`. For any other `http://` URL, `mae` refuses to start with a message
  saying the key needs an `https://` URL.
- **Redirects are not followed.** A model endpoint that answers with a redirect (301, 302, 303, 307, 308) fails
  with `redirects (301) to <url> — model endpoints are not followed through redirects; configure that URL
  instead`. Put the final URL in the config.
- **The key is removed from logs and error text.** Error bodies from the endpoint are scrubbed of the key before
  they are shown or raised. The run log scrubs the key from every string it writes, but only for keys of eight
  or more characters, so that short values do not blank out ordinary words.
- **The startup line says where the key goes.** `mae` prints `<runner>: sends $<VARIABLE> to <host>` before the
  run starts, or the `WARNING:` form when the repository's config chose the host.

!!! warning "Running `mae` still sends the key"
    When you start a run, the key is sent to the endpoint the repository's config names, and the startup line
    tells you so. Read `.mae/config.json` before you run `mae` in a repository you did not write.

### The dashboard does not send the key to a repository's endpoint

[`mae-ui`](dashboard.md) checks every runner as soon as you open a repository, so that the runner list is
accurate. Those automatic checks never send the key to an endpoint chosen by the repository's config. A
`401` or `403` from such an endpoint is therefore not reported as an error at that point. The launch-time check
does send the key, and it prints the host first.

## Retries

The `api` runner retries a request that fails with `429`, `500`, `502`, `503` or `504`, up to four times.

- The wait is the `Retry-After` header when the endpoint sends one, or 5, 10, 20 and 40 seconds when it does
  not. The longer of the two is used, and a single wait is capped at 600 seconds.
- A wait that would run past the time budget of the call fails at once with
  `ran out of its time budget: <url> answered 429 and asked to wait ...`.
- The console shows `[<label>] 429 from the model endpoint — retrying in 5s`, and the run log gets a `limit`
  event.

Other errors fail immediately with `<label>: <url> answered <code>: <body>`. A connection that cannot be made
is not retried either.

## What to expect from small local models

A 7 to 14 billion parameter model is slower and less capable than `claude` or `codex`. It does well on small,
clearly described units. It struggles with large changes, long plans and work that needs several tools in
order.

Some things you will see:

- Tool errors go back to the model as text. A failed `replace_in_file` says that the text it looked for was not
  found exactly once, and the model is expected to try again.
- The coder may leave units `IN_PROGRESS`. A broken status block is different: the tester reports it as a
  `plan: ...` error, which turns the pass red and sends the coder back to fix it.
- A model that gets stuck in a loop of tool calls stops with `did not finish within 200 model turns`.

To get better results:

- Split the plan into smaller units, each with a test that can be run alone.
- Start Ollama with a larger `OLLAMA_CONTEXT_LENGTH`, or use a larger model for the coder.
- Mix roles: a free model for the reviewer and the auditor, a stronger model for the coder.
- Raise `--max-iterations` if the run ends at the ceiling with units still open.

The cost recorded for `ollama` and `api` calls is `0.0`. Token counts are still written to the run log when the
endpoint reports them. See [Cost and limits](../concepts/cost.md).

For the other runners, see [Roles, runners and prompts](../reference/runners.md). For problems with a setup that
will not start, see [Troubleshooting](../troubleshooting.md).
