# `.claude/` — Claude Code project config

## Stop hook (goal 0)

`settings.json.example` holds a **Stop hook** that runs the fast tier of the
repo's one arbiter, `scripts/check.sh --fast`, every time a Claude Code turn
ends — so an agent can't finish leaving the tree lint-dirty, format-dirty, or
carrying a secret.

To activate it:

```sh
cp .claude/settings.json.example .claude/settings.json
```

It is shipped as `.example` rather than as a live `settings.json` on purpose:
Claude Code's auto-mode safety classifier blocks an agent from writing its own
`settings.json` (self-modification), which is the correct default. Copying the
file is the one-step human sign-off. The hook only ever runs the **fast** tier
— no docker containers — matching `.githooks/pre-commit`.

## What the fast tier checks

`ruff` lint + format, `shellcheck`, `docker compose config` (schema only, no
containers), `conformance/selftest.py`, and a `gitleaks` secret scan. Missing
tools warn-and-skip; present tools hard-fail on real findings. See
[`scripts/check.sh`](../scripts/check.sh).
