# home-tools

## Repository layout

Owner of this section: the "Repository layout" section of
overnightworks/marketplace `AGENTS.md`, restated verbatim below; a change there
lands here. In this repository `scripts/check_root_layout.py` is the check, and
CI runs it.

The repository root holds only what a tool must find there — the package
manifest and its lockfile, container files, scanner configuration, the licence —
and the entry documents `README.md`, `AGENTS.md`, `CLAUDE.md` and, where the
repository carries one, `HEART.md`. Everything else lives in the directory of
its owner: a ratchet baseline next to the check that reads it, a fixture next to
its test, a helper under `scripts/`. No catch-all directory (`tooling/`,
`misc/`) and no deeper hierarchy than the owner needs. A root allowlist check in
CI keeps the rule, so nobody has to remember it (operator ruling 06.09.2026).

## Checks

CI runs the `Lint and test` job and, after it, the `quality` job: the shared
gate of overnightworks/quality (`quality/config.toml` names the mandatory
jobs). Its required check is `quality / verdict`; the gate's SonarCloud scan is
the one scanner, and it reads `sonar-project.properties` and the tests job's
`coverage` artifact.

## Commits

Never pass `--no-verify` to `git commit` or `git merge`. The global hook that
`home setup` installs scans `git commit` and merge commits for secrets; the
README section „Was der Hook nicht garantiert“ lists what it does not cover
(rebase, cherry-pick, `--no-verify` and more), and the agent rule is the
backstop for the flag git lets skip it. A refused commit is fixed by
removing the secret, not by bypassing the hook.
