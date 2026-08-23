---
applyTo: "README.md,docs/**,AGENTS.md,CLAUDE.md,.github/**,.claude/skills/**,scripts/**,docker/**"
---

# Governance Instructions

- Keep commands, file paths, workflow names, config keys, release paths, and directory references aligned with the executable repository state.
- `AGENTS.md` is the canonical AI collaboration document; if its meaning changes, sync `CLAUDE.md`, `.github/copilot-instructions.md`, `.github/instructions/*.instructions.md`, and repository skills as needed.
- Root `SKILL.md` and `docs/openclaw-skill-integration.md` describe product or external integration behavior, not repository governance.
- Explain which pipeline, release path, deployment path, review automation, or governance asset is affected and what the rollback path is.
- Before creating/updating PRs, PR review, or issue analysis, refresh the latest code baseline with `git fetch --all --prune`; only run `git pull --ff-only` when the worktree is clean and the current branch can fast-forward. If not, keep local state intact and record the fetched remote baseline or branch gap before proceeding.
- Keep `README.md` limited to homepage-level content such as positioning, high-level capabilities, quick start, main entrypoints, and sponsorship/cooperation; put detailed behavior, configuration, troubleshooting, field contracts, and edge cases in `docs/*.md`.
- Avoid widening permissions, secret exposure, or destructive automation without a clearly documented need.
- Preserve the repository's opt-in auto-tag behavior (`#patch`, `#minor`, `#major`) unless the change explicitly updates release policy.
- When creating, reviewing, or suggesting PRs, prefer PR titles in `<type>: <change summary>` form and omit tool/agent source prefixes such as `[codex]`, `codex`, `autocode`, or `copilot`; treat this as non-blocking guidance, not a review hard blocker.
- If only one language version of a document is updated, explain why the counterpart was not synchronized.
- Deployment: local `F:\dsa` is development-only (never run servers). Ubuntu 192.168.1.197 runs BOTH environments from the single canonical root `~/dsa-test`: prod (`stock-server`/`stock-analyzer`, port 8000, real data `data/`) and test (`dsa-test-*`, port 8001, isolated `data-test/`). "Deploy to port 8000" = Ubuntu prod, never localhost. Sync: push origin dev (+ direct push to `~/dsa-repo.git` when GitHub unreachable) → `~/dsa-test` fetch + reset → validate 8001 → promote 8000 (`compose restart server analyzer`; `up -d --force-recreate` for env changes). Snapshot container env + `data/runtime.env`/`data/stock_analysis.db` to `~/backup/` before recreating or touching config; the only live runtime config is `~/dsa-test/data/runtime.env`. NEVER run compose from retired `~/daily_stock_analysis`. See `AGENTS.md` "Deployment & Environment" and `DEV_WORKFLOW.md`.
