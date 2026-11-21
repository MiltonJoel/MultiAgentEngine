# Security policy

## Supported versions

Only the latest release on `master` receives security fixes.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private vulnerability reporting
(*Security → Report a vulnerability* on the repository page) with:

- the affected version (`mae --version`),
- what an attacker can do, and the steps to reproduce it,
- any suggested fix.

You should get an answer within 7 days. Fixes are released as a patch version and noted
in `CHANGELOG.md`.

## Threat model — read before running the engine

MultiAgentEngine **runs coding agents that execute commands on your machine**. Treat a
run like handing your shell to a junior engineer you cannot watch:

- **The coder can run arbitrary shell commands** in the target repository (that is how it
  runs the tests). The `ollama`/`api` runners confine *file* tools to the repository and the
  `--allow-dir` directories, but `run_command` is a real shell with your user's rights. Run
  the engine on untrusted specs, plans or repositories only inside a container or VM.
- **Gates are commands from your config** (`.mae/config.json`, `--gate`). A repository you
  did not write can ship a config whose gates run anything. Read `.mae/config.json` and
  `.mae/prompts/` of a cloned repo before running `mae` in it.
- **The dashboard (`mae-ui`) can launch runs**, so it is a code-execution surface. It binds
  `127.0.0.1` only, rejects requests whose `Host`/`Origin` is not the loopback address it
  serves (DNS-rebinding protection), and requires a per-launch random token on every API
  call. Do not share the printed URL: it contains the token. Do not put it behind a reverse
  proxy that exposes it to a network.
- **API keys** are read only from the environment variable named in the config
  (`MAE_API_KEY` by default), never from files in the repository, and are never written to
  the run log.
- **Checkpoint commits** exclude common secret files (`.env*`, `*.pem`, `*.key`, SSH keys,
  `.npmrc`, `.pypirc`, `.netrc`, credential JSON files, kubeconfigs) even if the target
  repository's `.gitignore` forgets them. This is a safety net, not a guarantee: keep secrets
  out of the working tree you point the engine at.
