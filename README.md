# home-tools

Werkzeuge, die das Home in der Form halten, die `~/README.md` beschreibt.

## tidy-home

```
tidy-home            # Dry-Run: zeigt alle Funde, ändert nichts
tidy-home --apply    # führt die clean-Funde aus, audit-Funde bleiben Bericht
tidy-home --sizes    # zusätzlich die zehn größten Einträge im Home (langsam)
```

Jeder Lauf schreibt seinen Bericht nach `~/.local/state/tidy-home/last-report.md`
und gibt Exit-Code 1 zurück, sobald es einen Fund gibt (0, wenn das Home
sauber ist). `systemd/tidy-home.service` trägt `SuccessExitStatus=1`, damit
das kein fehlgeschlagener Dienst ist. Gibt es Funde und ist `notify-send`
installiert, schickt tidy-home zusätzlich eine Desktop-Benachrichtigung mit
der Kopfzeile des Berichts.

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

Bekannt ist ein Home-Eintrag, wenn `~/README.md` ihn in Backticks nennt (der
erste Pfadabschnitt zählt, `.secrets/gitlab` macht `.secrets` bekannt) oder
wenn es `README.md` selbst ist. Fehlt `~/README.md` ganz, meldet tidy-home
genau einen Fund: „README.md fehlt“. Wer einen Ordner im Home anlegt, trägt
ihn in `~/README.md` ein — eine zweite Liste gibt es nicht.

## Konfiguration

`~/.config/tidy-home/config.toml` überschreibt einzelne Grenzwerte; ohne die
Datei gelten die Defaults. Ein unbekannter Schlüssel ist ein Fehler.

| Schlüssel | Default | Bedeutung |
|---|---|---|
| `pasted_image_days` | `7` | Alter, ab dem `pasted-images` löscht |
| `trash_days` | `30` | Alter, ab dem `trash` löscht |
| `downloads_days` | `14` | Alter, ab dem `downloads-inbox` meldet |
| `codex_session_days` | `90` | Alter, ab dem `codex-sessions` meldet |
| `docker_stopped_container_days` | `7` | Alter, ab dem gestoppte Container geprunt werden |
| `docker_build_cache_keep` | `"20GB"` | Wert für `docker builder prune --keep-storage` |
| `downloads_ignore` | `[]` | Dateinamen in `Downloads/`, die nie gemeldet werden |

Beispiel:

```toml
trash_days = 60
downloads_ignore = ["keepme.iso"]
```

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
