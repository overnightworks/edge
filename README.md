# home-tools

Ein Befehl, `home`, hält das Home in der Form, die `~/README.md` beschreibt.
Jeder Unterbefehl erklärt sich mit `home <befehl> --help` samt Beispielen;
dieses README ist die lange Anleitung.

Alle Befehle teilen sich die Exit-Codes: `0` wenn alles sauber ist, `1` sobald
es einen Fund gibt (das ist kein Fehler — `systemd/home-check.service` trägt
`SuccessExitStatus=1` dafür), `2` wenn die Konfigurationsdatei oder das
Geheimnis-Manifest ungültig ist, `3` wenn ein Lauf an einem unerwarteten Fehler
zerbrach; dann steht eine deutsche Zeile auf stderr. Bei `2` und `3` meldet der
Timer einen fehlgeschlagenen Dienst, zu Recht.

## home check

```
home check           # alle Prüfungen lesend, ein Bericht, höchstens eine Benachrichtigung
```

`home check` fährt jede Prüfung, die nichts ändert — den Dry-Run von
`home tidy` und die Ansicht von `home secrets` — und schreibt einen gemeinsamen
Bericht nach `~/.local/state/home/check-report.md`. Eine
Desktop-Benachrichtigung (über `notify-send`, falls installiert) kommt nur,
wenn sich die Menge der Funde seit dem letzten Lauf geändert hat **und** es
Funde gibt oder eine zerbrochene Prüfung wieder läuft. Ein Fund ist dabei Regel plus Gegenstand: Derselbe Fund, der nur
älter oder größer wird, nervt nicht erneut. Zerbricht eine Prüfung oder läuft
sie wieder, meldet das ebenfalls; ihre Funde zählen dabei weder als neu noch
als weg, der Titel nennt die Prüfung. Die Benachrichtigung nennt im
Titel die Zahl der Funde und darunter, wie viele neu und wie viele weg sind,
samt Pfad zum Bericht:

```
home check 15.10.2026 09:00 | 3 Funde
1 neu, 2 weg. Bericht: /home/felix/.local/state/home/check-report.md
```

Die zuletzt gemeldete Fundmenge hält `~/.local/state/home/check-findings.txt`.
Ein Lauf ohne Funde benachrichtigt nur, wenn eine zerbrochene Prüfung wieder
läuft; den sauberen Stand merkt er sich, sodass ein wiederkehrender Fund
wieder meldet. Scheitert eine fällige Benachrichtigung, bleibt die zuletzt
gemeldete Fundmenge stehen — auch bei einem sauberen Lauf nach einer wieder
laufenden Prüfung —, und der nächste Lauf meldet, was dann noch von ihr
abweicht.

Zerbricht eine Prüfung (etwa ein Ordner unter `~/.secrets`, den `home` nicht
lesen darf), steht an ihrer Stelle eine Zeile „Prüfung secrets fehlgeschlagen
(…)“, die Funde der anderen Prüfung bleiben im Bericht, die Benachrichtigung
geht trotzdem raus, und der Lauf endet mit Exit-Code `3` — bei einem
ungültigen Geheimnis-Manifest mit `2`, und stderr nennt Datei und Schlüssel.

## home secrets

```
home secrets                # jedes Geheimnis: Datei, Prüfung, Ablauf; nie ein Wert
home secrets --by-service   # welcher Dienst welches Geheimnis liest (deklariert)
```

`home secrets` zeigt jedes Geheimnis, das das Manifest
`src/home_tools/secrets_manifest.toml` nennt, und ändert nichts außer seinem
Bericht `~/.local/state/home/secrets-report.md`. Es braucht kein Terminal;
Agenten dürfen es fahren.

**Kein Wert, nirgends.** Das Manifest enthält nur Namen, Orte und Links.
`home secrets` liest Geheimnisdateien nur per `stat` und öffnet sie nie. Die
einzige Prüfung ist `gh auth status` (der Keyring-Login von `gh`): `home`
behält davon nur den Exit-Code, verwirft Ausgabe und Fehlerausgabe und bricht
nach 20 Sekunden ab.

Beispiel (gekürzt):

```
# home secrets 15.10.2026 09:00 | 4 Funde

| Name | Art | Datei | Prüfung | Ablauf | Ort | Neu anlegen |
|---|---|---|---|---|---|---|
| gitlab | Token | vorhanden | nicht geprüft | Ablauf unbekannt | ~/.secrets/gitlab | https://code.siemens.com/-/user_settings/personal_access_tokens |
| gh | Tool-Login | vorhanden | geprüft: gültig | Ablauf unbekannt | ~/.config/gh | `gh auth login --web` |
| ssh/id_ed25519.ppk | abgeleitete Kopie | fehlt | nicht geprüft | Ablauf unbekannt | ~/.ssh/id_ed25519.ppk | `puttygen ~/.ssh/id_ed25519 -o ~/.ssh/id_ed25519.ppk` |
| github/token | Token | zurückgezogen | Widerruf unbestätigt | Ablauf unbekannt | ~/.secrets/github/token | Widerruf: https://github.com/settings/tokens |

## Funde

| Regel | Gegenstand | Befund | Empfehlung |
|---|---|---|---|
| missing | ssh/id_ed25519.ppk | - | neu anlegen, siehe Tabelle |
| revocation-unconfirmed | github/token | https://github.com/settings/tokens | beim Anbieter widerrufen, siehe Tabelle |
| unknown-file | ~/.secrets/README | - | unbekannt: ins Manifest eintragen oder von Hand löschen |
| loose-mode | ~/.secrets/gmail-cleanup | 775, höchstens 700 | von Hand enger setzen; home ändert keine Rechte |

Keine Werte: home secrets liest Geheimnisdateien nur per stat und öffnet sie nie.
```

So liest sich die Tabelle:

| Spalte | Werte |
|---|---|
| Art | Passwort (Mensch), Tool-Login, Token, Dienstpasswort, abgeleitete Kopie — genau eine je Eintrag |
| Datei | `vorhanden`, `fehlt`, `unbekannt` (das Dateisystem verweigert die Auskunft), `zurückgezogen` |
| Prüfung | nur `gh` wird geprüft (`geprüft: gültig`, `geprüft: ungültig`, `Prüfung nicht möglich` bei Zeitüberschreitung oder fehlendem `gh`); alle anderen `nicht geprüft` |
| Ablauf | heute immer `Ablauf unbekannt`: ein Datum erscheint erst mit Herkunft (Antwort des Anbieters oder ein vom Assistenten geschriebenes `rotated_on`); das Dateialter zählt nie |

Funde sind: ein fehlender oder unbekannter Eintrag, ein zurückgezogenes
Geheimnis, dessen Widerruf unbestätigt ist, eine fehlgeschlagene oder nicht
mögliche Prüfung, eine Datei oder ein Ordner unter `~/.secrets`, den kein
Eintrag nennt („unbekannt“; bleibt unangetastet), und Rechte lockerer als
`600` für Dateien oder `700` für Ordner unter `~/.secrets`. `home` setzt nie
Rechte; `chmod` bleibt ein Handgriff.

`--by-service` dreht `read_by` des Manifests um: je Dienst die Geheimnisse,
die er laut Manifest liest, dazu die abgeleiteten Kopien und ihre Quelle. Die
Überschrift sagt „deklarierte Abhängigkeiten“: Das ist, was das Manifest
behauptet, kein Beweis vollständiger Wirkung.

### Einen Eintrag hinzufügen

Ein neues Geheimnis ist ein Block im Manifest:

```toml
[[secret]]
name = "grafana-admin"
kind = "service-password"
paths = ["~/.secrets/grafana/admin"]
read_by = ["grafana.service"]
recreate = "wird beim Einrichten erzeugt"
```

`paths` beginnt mit `~/`, nur der letzte Teil darf einen Platzhalter tragen
(`~/.secrets/cloudflared/*.json`). Ein zurückgezogener Eintrag trägt
`retired_on = 2026-10-14` und `revoke_url`, eine abgeleitete Kopie
`mirror_of`. Ein unbekannter Schlüssel (auch ein Versuch, einen Wert
abzulegen), ein falscher Wert oder ein doppelter Name lässt `home secrets`
mit Exit-Code `2` abbrechen; die Meldung nennt Datei und Schlüssel.

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
