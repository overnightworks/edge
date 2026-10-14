# home-tools

Ein Befehl, `home`, hält das Home in der Form, die `~/README.md` beschreibt.
Jeder Unterbefehl erklärt sich mit `home <befehl> --help` samt Beispielen;
dieses README ist die lange Anleitung.

Alle Befehle teilen sich die Exit-Codes: `0` wenn alles sauber ist, `1` sobald
es einen Fund gibt (das ist kein Fehler — `systemd/home-check.service` trägt
`SuccessExitStatus=1` dafür), `2` wenn die Konfigurationsdatei selbst ungültig
ist (der Timer meldet das dann als fehlgeschlagenen Dienst, zu Recht).

## home check

```
home check           # alle Prüfungen lesend, ein Bericht, höchstens eine Benachrichtigung
```

`home check` fährt jede Prüfung, die nichts ändert — heute den Dry-Run von
`home tidy` — und schreibt einen gemeinsamen Bericht nach
`~/.local/state/home/check-report.md`. Eine Desktop-Benachrichtigung (über
`notify-send`, falls installiert) kommt nur, wenn es Funde gibt **und** sich
die Menge der Funde seit dem letzten Lauf geändert hat. Ein Fund ist dabei
Regel plus Gegenstand: Derselbe Fund, der nur älter oder größer wird, nervt
nicht erneut. Den Fingerabdruck der letzten Fundmenge hält
`~/.local/state/home/check-findings.sha256`. Ein Lauf ohne Funde
benachrichtigt nie, merkt sich aber den sauberen Stand, sodass ein
wiederkehrender Fund wieder meldet.

## home tidy

```
home tidy            # Dry-Run: zeigt alle Funde, ändert nichts
home tidy --apply    # führt die clean-Funde aus, audit-Funde bleiben Bericht
home tidy --sizes    # zusätzlich die zehn größten Einträge im Home (langsam)
```

Jeder Lauf schreibt seinen Bericht nach `~/.local/state/home/tidy-report.md`.
`home tidy` benachrichtigt nicht: Wer es von Hand startet, sieht den Bericht
im Terminal; die Benachrichtigung gehört allein `home check`.

Eine Regel meldet nur, wenn `--apply` wirklich etwas ändern würde: `docker`
meldet gestoppte Container nur, wenn welche über dem Alterslimit liegen,
Images nur, wenn es dangling Images gibt (genau die löscht `docker image
prune -f`), den Build-Cache nur, wenn er über seinem Limit liegt *und*
reklamierbaren Speicher hat.

### clean: wird mit `--apply` ausgeführt

| Regel | Was | Grenze |
|---|---|---|
| `pasted-images` | `Eingefügtes Bild*.png` in `~`, `~/git`, `~/Dokumente`, `~/Schreibtisch` | älter als 7 Tage |
| `trash` | GNOME-Papierkorb und der Papierkorb des VS-Code-Snaps | älter als 30 Tage |
| `docker` | gestoppte Container, dangling Images, Build-Cache | 7 Tage, 20 GB Cache bleiben |

`uv cache prune` und `npm cache verify` laufen ebenfalls unter `--apply`, sind
aber keine Funde: sie zählen nie mit, blockieren nie, und ihr Ergebnis steht
nur in der `Wartung:`-Zeile am Ende des Berichts (im Dry-Run steht dort, dass
`--apply` sie ausführen würde).

### audit: nur Bericht, entscheidet ein Mensch

| Regel | Was |
|---|---|
| `unknown-home-entry` | Einträge im Home-Root, die `~/README.md` nicht kennt |
| `git-root-litter` | alles in `~/git`, das weder Repo noch `<repo>-worktrees` ist |
| `stale-worktree` | Worktrees mit geschlossenem Issue oder ohne Registrierung |
| `downloads-inbox` | Downloads älter als 14 Tage |
| `codex-sessions` | Codex-Sessions älter als 90 Tage, als Summe |
| `largest-entries` | nur mit `--sizes`, nur Einträge ab 5 GiB, höchstens 10 |

Bekannt ist ein Home-Eintrag, wenn `~/README.md` ihn vor der Überschrift „Was
hier nicht mehr liegt“ in Backticks als Pfad nennt — `~/`, `.` oder `/` im
Token, der erste Pfadabschnitt zählt, `.secrets/gitlab` macht `.secrets`
bekannt — oder wenn es `README.md` selbst ist. Tokens danach oder ohne
Pfadform (`uv`, `pip`, ...) zählen nicht. Fehlt `~/README.md` ganz, meldet
`home tidy` genau einen Fund: „README.md fehlt“. Wer einen Ordner im Home
anlegt, trägt ihn in `~/README.md` ein — eine zweite Liste gibt es nicht.

Der Bericht listet außerdem, welche Regeln liefen und nichts fanden
(„Geprüft, nichts gefunden: …“) und welche gar nicht laufen konnten, etwa
weil `docker` fehlt oder `du` fehlschlug („Nicht geprüft: docker (nicht
installiert)“) — das eine ist kein Fund, das andere ist keine Prüfung.

## Konfiguration

Eine Datei für alle Befehle: `~/.config/home/config.toml`, eine Tabelle je
Befehl mit Einstellungen. Ohne die Datei gelten die Defaults. Eine unbekannte
Tabelle, ein unbekannter Schlüssel, ein negativer Tageswert oder ein falscher
Werttyp ist ein Konfigurationsfehler: `home` meldet ihn auf stderr und beendet
sich mit Exit-Code 2, statt mit falschen Grenzen weiterzulaufen.

Liegt noch die alte `~/.config/tidy-home/config.toml` und die neue fehlt,
bricht `home` ebenso mit Exit-Code 2 ab und nennt den genauen Befehl, der die
alte Datei einmalig unter `[tidy]` in die neue umzieht — es gibt keinen
stillen Rückfall auf die Defaults.

### `[tidy]`

| Schlüssel | Default | Bedeutung |
|---|---|---|
| `pasted_image_days` | `7` | Alter, ab dem `pasted-images` löscht |
| `trash_days` | `30` | Alter, ab dem `trash` löscht |
| `downloads_days` | `14` | Alter, ab dem `downloads-inbox` meldet |
| `codex_session_days` | `90` | Alter, ab dem `codex-sessions` meldet |
| `docker_stopped_container_days` | `7` | Alter, ab dem gestoppte Container geprunt werden |
| `docker_build_cache_keep` | `"20GB"` | Wert für `docker builder prune --keep-storage` |
| `downloads_ignore` | `[]` | Dateinamen in `Downloads/`, die nie gemeldet werden |

### `[notify]`

| Schlüssel | Default | Bedeutung |
|---|---|---|
| `enabled` | `true` | `false` schaltet die Benachrichtigung von `home check` ab |

Beispiel:

```toml
[tidy]
trash_days = 60
downloads_ignore = ["keepme.iso"]

[notify]
enabled = true
```

## Installation

```
uv tool install --force --python 3.12 --editable ~/git/home-tools
systemctl --user link --force ~/git/home-tools/systemd/home-check.service ~/git/home-tools/systemd/home-check.timer
systemctl --user enable --now home-check.timer
```

`uv tool install` legt den Befehl nach `~/.local/bin/home`, wo die
systemd-Unit ihn aufruft. `--python 3.12` ist dieselbe Version, die CI prüft.
`--editable` lässt den Befehl aus dem Checkout laufen: eine Änderung unter
`src/` wirkt ohne Neuinstallation.

Der Timer läuft täglich 09:00 (`Persistent=true` holt einen verpassten Lauf
nach) und fährt nur `home check`. `home tidy --apply` bleibt ein bewusster
Handgriff.

## Tests

```
uv run pytest          # nur die Tests
uv run pytest --cov    # zusätzlich die 100-%-Grenze aus pyproject.toml
uv run ruff check .
uv run python scripts/check_root_layout.py   # was im Wurzelverzeichnis liegen darf
```

`src/home_tools/` und `scripts/check_root_layout.py` halten 100 %
Zeilen- und Branch-Abdeckung; `fail_under = 100` in `pyproject.toml` lässt
jeden Coverage-Lauf rot werden, der darunter bleibt. Die Layout-Prüfung hält
das Wurzelverzeichnis auf der Allowlist, die `AGENTS.md` beschreibt.
`.github/workflows/ci.yml` fährt alles bei jedem Push und Pull Request.
