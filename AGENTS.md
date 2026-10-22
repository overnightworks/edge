# edge

## Checks

CI runs the `check` job (lint, the root layout check `scripts/check_root_layout.py`, tests, wheel smoke test) and, after it, the `quality` job: the shared
gate of overnightworks/quality (`quality/config.toml` names the mandatory
jobs). Its required check is `quality / verdict`; the gate's SonarCloud scan is
the one scanner, and it reads `sonar-project.properties` and the tests job's
`coverage` artifact.

## Commits

Never pass `--no-verify` to `git commit` or `git merge`. The global hook that
`hal setup` installs scans `git commit` and merge commits for secrets; the
section „Was der Hook nicht garantiert“ of `docs/home.md` lists what it does not cover
(rebase, cherry-pick, `--no-verify` and more), and the agent rule is the
backstop for the flag git lets skip it. A refused commit is fixed by
removing the secret, not by bypassing the hook.
