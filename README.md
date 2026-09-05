# home-tools

Werkzeuge, die das Home in der Form halten, die `~/README.md` beschreibt.

## tidy-home

```
tidy-home            # Dry-Run: zeigt alle Funde, ändert nichts
tidy-home --apply    # führt die clean-Funde aus, audit-Funde bleiben Bericht
tidy-home --sizes    # zusätzlich die zehn größten Einträge im Home (langsam)
```

Jeder Lauf schreibt seinen Bericht nach `~/.local/state/tidy-home/last-report.md`.

### clean: wird mit `--apply` ausgeführt

| Regel | Was | Grenze |
|---|---|---|
| `pasted-images` | `Eingefügtes Bild*.png` in `~`, `~/git`, `~/Dokumente`, `~/Schreibtisch` | älter als 7 Tage |
| `trash` | GNOME-Papierkorb und der Papierkorb des VS-Code-Snaps | älter als 30 Tage |
| `docker` | gestoppte Container, dangling Images, Build-Cache | 7 Tage, 20 GB Cache bleiben |
| `caches` | `uv cache prune`, `npm cache verify` | |

### audit: nur Bericht, entscheidet ein Mensch

| Regel | Was |
|---|---|
| `unknown-home-entry` | Einträge im Home-Root, die `~/README.md` nicht kennt |
| `git-root-litter` | alles in `~/git`, das weder Repo noch `<repo>-worktrees` ist |
| `stale-worktree` | Worktrees mit geschlossenem Issue oder ohne Registrierung |
| `downloads-inbox` | Downloads älter als 14 Tage |
| `codex-sessions` | Codex-Sessions älter als 90 Tage, als Summe |
| `largest-entries` | nur mit `--sizes` |

Die Liste der bekannten Home-Einträge steht in `KNOWN_HOME_ENTRIES` in
`tidy_home.py`. Wer einen Ordner im Home anlegt, trägt ihn dort und in
`~/README.md` ein.

## Installation

```
ln -s ~/git/home-tools/tidy_home.py ~/.local/bin/tidy-home
systemctl --user link --force ~/git/home-tools/systemd/tidy-home.service ~/git/home-tools/systemd/tidy-home.timer
systemctl --user enable --now tidy-home.timer
```

Der Timer läuft montags 09:00 als Dry-Run und schreibt nur den Bericht.
`--apply` bleibt ein bewusster Handgriff.

## Tests

```
uv run pytest
uv run ruff check .
```
