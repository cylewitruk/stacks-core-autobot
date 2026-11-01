# Concurro in this fork

This experimental fork uses the [Concurro](https://github.com/concurro/concurro)
engine to implement enrolled issues end to end. The engine (controller, roles,
skills, schemas and images) lives in that repository; see its
[operations guide](https://github.com/concurro/concurro/blob/main/docs/concurro.md).
Nothing here writes to `stacks-network/stacks-core`.

| Path | Purpose |
| ---- | ---- |
| `.concurro/config.json` | Roles, models, budgets, required checks and editable-path policy |
| `.concurro/charter.json` | Goals, non-goals, constraints and acceptance standards |
| `.github/workflows/concurro.yml` | Triggers, runner, environment, App token and the pinned engine step |
| `.github/workflows/concurro-rust.yml` | The `Concurro Rust` smoke check required before merge |

Candidates cannot edit these files, hidden paths, Cargo manifests, toolchain
files, `tools/`, `contrib/`, `scripts/` or this guide.

## Engine upgrades

The workflow runs `uses: concurro/concurro@<full-commit-sha>`. Upgrade by changing
that SHA in a reviewed change, together with the matching inference image pin.
An engine or charter change invalidates active design approvals.

## GitHub settings

- Environment `concurro`, deployment branches restricted to `main`.
- Variables `CONCURRO_ENABLED`, `CONCURRO_APP_ID`, `CONCURRO_AGENT_IMAGE` and
  `CONCURRO_EXECUTION_IMAGE`.
- Secrets `CONCURRO_APP_PRIVATE_KEY`, `CONCURRO_CODEX_API_KEY`, and exactly one of
  `CONCURRO_CLAUDE_OAUTH_TOKEN` / `CONCURRO_ANTHROPIC_API_KEY`.
- A self-hosted runner labelled `concurro-controller` and an issue label `concurro`.
- Branch protection on `main` requiring `Concurro Rust`.

Keep `merge_enabled` false and `CONCURRO_ENABLED` unset until a supervised pilot
passes.

## Local pilot

Run the engine's pilot from this checkout, with the engine checked out at the
pinned commit:

```bash
python3 /path/to/concurro/engine/pilot.py preflight
```

Set `gh_user` in the private pilot settings to the `gh` login that owns this
repository. `tools/gh-owner` runs other `gh` commands as that owner without
changing the active account.
