# home-tools

Ein Befehl, `home`, hält das Home in der Form, die `~/README.md` beschreibt.
Jeder Unterbefehl erklärt sich mit `home <befehl> --help` samt Beispielen;
dieses README ist die lange Anleitung.

Alle Befehle außer `home next` (siehe dort) teilen sich die Exit-Codes: `0` wenn alles sauber ist, `1` sobald
es einen Fund gibt (das ist kein Fehler — `systemd/home-check.service` trägt
`SuccessExitStatus=1` dafür), `2` wenn die Konfigurationsdatei oder das
Geheimnis-Manifest ungültig ist, `3` wenn ein Lauf an einem unerwarteten Fehler
zerbrach; dann steht eine deutsche Zeile auf stderr. Bei `2` und `3` meldet der
Timer einen fehlgeschlagenen Dienst, zu Recht.

## home next

```
home next            # was jetzt von dir gebraucht wird, je Schritt mit dem genauen Befehl
```

`home next` zeigt nur, was du selbst tun musst: eine nummerierte Liste, der
dringendste Schritt zuerst. Jeder Schritt nennt den Befehl, wo er läuft, und was
danach zu sehen sein soll. Was ein Agent einsortieren kann, zählt eine
Schlusszeile:

```
1. home secrets --setup (eigenes Terminalfenster) — erwartet: geprüft und gespeichert: ~/.secrets/cloudflare/fleet-read (600); geprüft und gespeichert: ~/.secrets/github/fleet-read (600); erzeugt und gespeichert: ~/.secrets/opentofu/state-passphrase (600); zuletzt next: `uv run edge external plan` in ~/git/edge (expected: No changes)
2. https://sonarcloud.io/account/security (von Hand, für sonarcloud/token) — erwartet: home secrets zeigt sonarcloud/token vorhanden
3. home setup --apply (eigenes Terminalfenster) — erwartet: Aktivierung, System: changed; it asks once for sudo
   warum: ohne den Hook prüft kein Commit auf Geheimnisse; without it the tunnel ignores edge's global stop after a reboot, and only root may stop it
Außerdem zum Einsortieren: 4 unbekannte Dateien in ~/.secrets, 25 Home-Einträge, 13 Downloads. Das macht ein Agent: sag „räum home auf“.
```

Ein Schritt ist etwas, das nur du tun kannst: ein geführter Befehl an deinem
Terminal (`home secrets --setup`, `home setup --apply`, `home tidy --apply`),
eine Seite im Browser (ein Token anlegen oder widerrufen) oder ein Befehl, den ein
Fund für deine Ordnerrechte oder deine Git-Konfiguration nennt. Ob ein Fund ein
Schritt ist oder Buchhaltung, sagt die Regel, die ihn findet, nicht `home next`.
Ein Befehl, der ohne Terminal abbricht, steht mit „eigenes Terminalfenster“ da;
das `!` in Claude Code ist kein Terminal. Nur ein Befehl, der auch ohne Terminal
läuft, steht mit „beliebiges Terminal“ da.

Die Schritte kommen aus den Besitzern der Fakten, die `home next` nur liest.
Zuerst steht, was den Assistenten blockiert, der sonst nichts speichert, in der
Reihenfolge des Baums unter `~/.secrets`, also nach Pfad sortiert, gleich
welcher Art der Befund ist: zu offene Rechte (dann das `chmod`),
ein Ordner, den sein Besitzer nicht öffnen kann (dann `chmod 700`), ein Symlink
auf dem Weg zu einem Eintrag (dann der Befehl, der ihn durch den echten Ordner
ersetzt) und eine Datei, wo ein Ordner hingehört (dann das `mv`, das sie
beiseite legt). Dann fehlende Geheimnisse aus der Ansicht von `home secrets`
(der Assistent für die, die er anlegen kann, die Seite aus `recreate` für die
übrigen), ein fehlgeschlagener Login, ein unbestätigter Widerruf und jeder
übrige Fund von `home secrets`, den nur du klären kannst, mit Ort und Empfehlung
von Hand (etwa „~/.ssh/id_ed25519: Rechte des Elternordners prüfen“). Dann
offene Schritte von `home setup` (dann `home setup --apply`), die Korrekturen
der Hook-Prüfung und die clean-Funde von `home tidy` (dann `home tidy --apply`).
Jeder Befehl steht nur einmal da.

Die Schlusszeile zählt die Buchhaltung je Art, in fester Reihenfolge und ohne
Pfad: unbekannte Dateien in `~/.secrets`, „~/README.md fehlt“, Home-Einträge,
die `~/README.md` nicht kennt, Reste in `~/git`, alte Worktrees, alte Downloads und alte
Codex-Sitzungen. Die sortiert ein Agent ein; jede Löschentscheidung kommt als
eine Liste zu dir. Die einzelnen Funde zeigen `home check`, `home tidy` und
`home secrets`.

Gibt es nichts, steht da „Nichts zu tun.“. Gibt es nur Buchhaltung, steht da
„Nichts für dich zu tun.“ und darunter die Schlusszeile. Kann `home next` eine
Quelle nicht lesen (etwa ein ungültiges Manifest), steht ganz oben ein Schritt
„1. konnte die Geheimnisse nicht lesen; den Fehler zeigt: home secrets“, und die
Schritte der anderen Quellen folgen. Eine ungültige Konfigurationsdatei liest nur
`home tidy`; dann steht dort „konnte das Home (home tidy) nicht lesen; den Fehler
zeigt: home tidy“. Es ändert nichts, fragt nach keinem Passwort und liest keinen
Geheimniswert (nur `stat`, wie `home secrets`); Agenten dürfen es fahren.
Exit-Code: immer `0`, auch mit Schritten, denn es ist eine Ansicht, keine
Prüfung; `3` nur bei einem unerwarteten Fehler.

## home check

```
home check           # alle Prüfungen lesend, ein Bericht, höchstens eine Benachrichtigung
```

`home check` fährt jede Prüfung, die nichts ändert — den Dry-Run von
`home tidy`, die Ansicht von `home secrets` und die Hook-Prüfung — und schreibt einen gemeinsamen
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

Die Benachrichtigung trägt den Knopf „Bericht öffnen“; ein Klick öffnet den
Bericht mit `xdg-open`. Gewartet wird nicht im Check, sondern in einer
losgelösten Einheit (`systemd-run --user --collect`), sodass `home check` und
der Timer-Dienst sofort enden.

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

### Die Prüfung `hook`

Git lässt einen Commit ungescannt durch, wenn der Hook-Pfad abdriftet. Die
Prüfung `hook` fragt `setup.py` nach dem Soll und meldet je Fund die
Korrektur:

- **Hook greift nicht.** In jedem Repository unter `~/git` und jedem seiner
  registrierten Worktrees (auch außerhalb von `~/git`) muss das wirksame
  `core.hooksPath` das Hook-Verzeichnis sein und aus der globalen Config selbst
  stammen (`~/.gitconfig` oder `$XDG_CONFIG_HOME/git/config`). Der Ursprung entscheidet,
  nicht der Scope: lokale Config, Worktree-Config, System-Datei, `include` und
  `includeIf` sind Funde, ebenso ein ungesetzter Wert. Die Korrektur nennt den
  passenden Weg (`git config --local --unset`, `--worktree --unset`, die
  genannte Datei bearbeiten oder `home setup --apply`).
- **Hook-Datei falsch.** `pre-commit` oder `pre-merge-commit` fehlt, hat nicht den Modus 755
  oder weicht vom gerenderten Text ab (`home setup --apply`).
- **quality nicht wie festgelegt.** `~/.local/bin/quality` fehlt, ist nicht ausführbar oder uv hat
  nicht `QUALITY_SOURCE` aufgezeichnet (`home setup --apply`).
- **Engines.** `quality engines verify` scheitert (`home setup --apply`).
- **Verdeckter Repository-Hook.** `.git/hooks` enthält eine Datei außer
  `*.sample`, `pre-commit` und `pre-merge-commit`; der globale Pfad verdeckt sie.

Was die Prüfung nicht sieht, weil es flüchtig ist oder außer Reichweite liegt:

- ein einzelner Befehl mit `-c core.hooksPath=…`, `GIT_CONFIG_PARAMETERS`,
  `GIT_CONFIG_COUNT` oder `GIT_CONFIG_GLOBAL`;
- `--no-verify`;
- Repositories außerhalb von `~/git`;
- ein `includeIf "onbranch:…"`, das nur für den Branch gilt, der beim Lauf von
  `home check` ausgecheckt ist.

Kann die Prüfung nicht laufen (git lässt sich nicht starten), gilt sie als
zerbrochen, nie als sauber.

## home secrets

```
home secrets                # jedes Geheimnis: Datei, Prüfung, Ablauf; nie ein Wert
home secrets --by-service   # welcher Dienst welches Geheimnis liest (deklariert)
home secrets --setup        # führt am Terminal durch jedes fehlende Geheimnis
```

`home secrets` zeigt jedes Geheimnis, das das Manifest
`src/home_tools/secrets_manifest.toml` nennt, und ändert nichts außer seinem
Bericht `~/.local/state/home/secrets-report.md`. Es braucht kein Terminal;
Agenten dürfen es fahren.

**Kein Wert, nirgends.** Das Manifest enthält nur Namen, Orte und Links.
`home secrets` liest Geheimnisdateien nur per `stat` und öffnet sie nie. Die
einzige Prüfung der Ansicht ist `gh auth status` (der Keyring-Login von `gh`): `home`
behält davon nur den Exit-Code, verwirft Ausgabe und Fehlerausgabe und bricht
nach 20 Sekunden ab.

Beispiel (gekürzt):

```
# home secrets 15.10.2026 09:00 | 2 Funde

| Name | Art | Datei | Prüfung | Ablauf | Ort | Neu anlegen |
|---|---|---|---|---|---|---|
| gitlab | Token | vorhanden | nicht geprüft | Ablauf unbekannt | ~/.secrets/gitlab | https://code.siemens.com/-/user_settings/personal_access_tokens |
| gh | Tool-Login | vorhanden | geprüft: gültig | Ablauf unbekannt | ~/.config/gh | `gh auth login --web` |
| secrets/README | Dokumentation | vorhanden | nicht geprüft | Ablauf unbekannt | ~/.secrets/README | ~/.secrets/README anlegen: je Datei eine Zeile, was sie ist (kein Geheimnis) |
| cloudflared | Tool-Login | vorhanden | nicht geprüft | Ablauf unbekannt | ~/.secrets/cloudflared/cert.pem; Tunnel-Config und Zugangsdatei des laufenden Tunnels: /etc/cloudflared (root) | `cloudflared tunnel login` im Browser |
| ssh/id_ed25519.ppk | abgeleitete Kopie | fehlt (optional) | nicht geprüft | Ablauf unbekannt | ~/.ssh/id_ed25519.ppk | `puttygen ~/.ssh/id_ed25519 -o ~/.ssh/id_ed25519.ppk` |
| github/token | Token | zurückgezogen | Widerruf bestätigt 15.10.2026 | Ablauf unbekannt | ~/.secrets/github/token | Widerruf: https://github.com/settings/tokens |

## Funde

| Regel | Gegenstand | Befund | Empfehlung |
|---|---|---|---|
| unknown-file | ~/.secrets/cloudflared/old.json | - | ~/.secrets/cloudflared/old.json ins Manifest eintragen oder löschen |
| loose-mode | ~/.secrets/gmail-cleanup | hat Rechte 775, höchstens 700 | `chmod 700 ~/.secrets/gmail-cleanup` |

Keine Werte: home secrets liest Geheimnisdateien nur per stat und öffnet sie nie.
```

So liest sich die Tabelle:

| Spalte | Werte |
|---|---|
| Art | Passwort (Mensch), Tool-Login, Token, Dienstpasswort, abgeleitete Kopie — genau eine je Eintrag; Konfiguration und Dokumentation für bekannte Dateien unter `~/.secrets`, die kein Geheimnis sind |
| Datei | `vorhanden`, `fehlt`, `unbekannt` (das Dateisystem verweigert die Auskunft), `zurückgezogen`; ein optionaler Eintrag trägt `(optional)` |
| Prüfung | bei einem zurückgezogenen Eintrag `Widerruf unbestätigt` oder `Widerruf bestätigt <Datum>` |
| Ort | die Pfade im Home; liegen Kopien woanders (etwa `/etc/cloudflared`, root), nennt sie die Zelle dahinter, gelesen werden sie nie |
| Prüfung | nur `gh` wird geprüft (`geprüft: gültig`, `geprüft: ungültig`, `Prüfung nicht möglich` bei Zeitüberschreitung oder fehlendem `gh`); alle anderen `nicht geprüft` |
| Ablauf | heute immer `Ablauf unbekannt`: ein Datum erscheint erst mit Herkunft (Antwort des Anbieters oder ein vom Assistenten geschriebenes `rotated_on`); das Dateialter zählt nie |

Funde sind: ein fehlender Eintrag (außer einem optionalen) oder ein unbekannter, ein zurückgezogenes
Geheimnis, dessen Widerruf unbestätigt ist, eine fehlgeschlagene oder nicht
mögliche Prüfung, eine Datei oder ein Ordner unter `~/.secrets`, den kein
Eintrag nennt („unbekannt“; bleibt unangetastet), und Rechte lockerer als
`600` für Dateien oder `700` für Ordner unter `~/.secrets`, ein Ordner dort,
den sein Besitzer nicht lesen, betreten oder beschreiben kann („closed-folder“;
was darin liegt, ist „unbekannt“), ein Symlink
auf dem Weg zu einem Eintrag („symlinked-folder“; die Empfehlung ersetzt ihn durch
den Ordner, auf den er zeigt, oder durch einen leeren, wenn das Ziel fehlt) und eine Datei, wo auf dem Weg zu einem Eintrag ein
Ordner stehen muss („not-a-folder“; die Empfehlung schiebt sie mit `mv -n` nach
`<Name>.alt`). Die Ordner auf dem Weg beurteilt dieselbe Regel, mit der
`--setup` das Speichern verweigert. `home` setzt nie Rechte und ersetzt nichts;
`chmod` bleibt ein Handgriff.

### Einrichten: `home secrets --setup`

`--setup` geht die fehlenden Einträge des Manifests der Reihe nach durch,
zuerst die fünf, die `edge external` liest: `cloudflare/fleet-read`,
`github/fleet-read` und `opentofu/state-passphrase` für `plan`, danach
`cloudflare/fleet-write` und `github/fleet-admin` für `apply`. Es läuft nur am
eigenen Terminal (stdin und stdout); ohne Terminal endet es mit Exit-Code `2`,
bevor es etwas anfasst. Agenten fahren es nie.

Ein Token legt nur an, wer mehr Rechte hat als das Token: GitHub hat für
Fine-grained-Tokens keine API, und für Cloudflare bräuchte der Assistent
einen Admin-Schlüssel auf der Platte. Den Login im Browser macht deshalb der
Mensch, alles andere der Assistent. Je Token:

1. Er sagt, was kommt: welche Seite, welcher Login, welche Rechte, wofür.
   Vorher prüft er die Ordner auf dem Weg: Ist einer lockerer als `700`, für
   seinen Besitzer verschlossen, ein Symlink („ist ein Symlink“) oder kein
   Ordner, nennt er das mit dem Befehl, der es behebt, und öffnet keine Seite,
   damit kein frisch angelegtes Token verloren geht.
2. Nach Enter öffnet er die Seite. GitHubs Formular ist über die dokumentierten
   URL-Parameter vorausgefüllt (Name, Resource owner `overnightworks`, Ablauf,
   Beschreibung, Rechte); „Repository access: All repositories“ kann kein Link
   setzen und steht als Haken in der Liste. Cloudflare füllt nichts vor; die
   Liste nennt jede Permission, die Zone und das TTL-Enddatum; das Startdatum
   bleibt leer.
3. Er nimmt den Wert unsichtbar entgegen. Leer lassen überspringt den Eintrag.
4. Er fragt den Anbieter, bevor er speichert: Cloudflare
   `GET /user/tokens/verify` muss `active` melden und darf kein Startdatum
   (`not_before`) in der Zukunft haben, GitHub muss
   `GET /orgs/overnightworks/actions/secrets` beantworten (das braucht die
   Org und das Secrets-Recht und liefert nur Namen). Ein abgelehnter Wert wird
   mit einer Zeile abgewiesen und nicht geschrieben; danach darf man noch
   einmal einfügen.
5. Er schreibt die Datei mit `600` in einen Ordner mit `700` und geht zum
   nächsten Eintrag. Die Ordner prüft er dabei noch einmal; `home` setzt keine
   Rechte.

Die State-Passphrase erzeugt er zufällig und schreibt sie, ohne sie zu zeigen.
Eine vorhandene Datei bleibt unangetastet; ersetzen ist `--rotate` und kommt
später. Ein Eintrag ohne Assistent (etwa `gitlab`) wird mit seiner Seite aus
`recreate` genannt, ein optionaler, der fehlt, übersprungen. Strg-C bricht ab, ohne eine halbe Datei zu hinterlassen
(Exit-Code `130`); der nächste Lauf macht beim ersten fehlenden Eintrag weiter.
Die letzte Zeile nennt den nächsten Schritt.

Beispiel (gekürzt, ein abgelehnter Versuch):

```
== github/fleet-read (Token, fehlt)
Wofür: edge external plan und check lesen damit Repo-Einstellungen, …
Seite: GitHub, neues Fine-grained-Token. Login: dein GitHub-Konto, Inhaber der Org overnightworks.
Der Link füllt vor: Name github/fleet-read, Resource owner overnightworks, Ablauf 366 Tage, die Beschreibung und diese Rechte:
  - metadata: read
  - organization_secrets: read
Von Hand, das kann kein Link vorgeben:
  [ ] Repository access: All repositories
  [ ] die Rechte im Formular mit der Liste oben vergleichen
  [ ] Generate token, dann den Wert kopieren
Enter öffnet die Seite im Browser.
Geöffnet: https://github.com/settings/personal-access-tokens/new?name=github%2Ffleet-read&…
Token einfügen (unsichtbar; leer überspringt):
abgelehnt: GitHub antwortet 401; nichts gespeichert. Noch einmal einfügen oder leer lassen.
Token einfügen (unsichtbar; leer überspringt):
geprüft und gespeichert: ~/.secrets/github/fleet-read (600)

== opentofu/state-passphrase (Dienstpasswort, fehlt)
Wofür: OpenTofu verschlüsselt damit State und Plan unter ~/.secrets/opentofu.
Wird zufällig erzeugt und nie gezeigt.
erzeugt und gespeichert: ~/.secrets/opentofu/state-passphrase (600)

next: `uv run edge external plan` in ~/git/edge (expected: No changes)
```

Fehlt danach noch etwas, nennen die Zeilen vor der letzten die offenen
Einträge: die für einen neuen Lauf („noch offen: cloudflare/fleet-read“) und die
von Hand („von Hand offen, siehe oben: gitlab, sonarcloud/token“). Genau eine
Zeile beginnt mit `next:`, die letzte: Ist noch etwas offen, nennt sie zuerst den
Befehl, der eine Weigerung aufhebt, dann den neuen Lauf („next: `chmod 700
~/.secrets`, dann `home secrets --setup` noch einmal“); sonst den Plan-Schritt,
oder, fehlt von Hand noch ein Eintrag, den `edge external plan` liest, diesen
(„next: erst <Eintrag> anlegen; …“). Exit-Code: `0` nichts fehlt mehr,
`1` es fehlt noch etwas, `2` kein Terminal, eines, das die Eingabe nicht
verbergen kann, Manifest ungültig oder ein unerwarteter Fehler bei einem
Eintrag, `130` abgebrochen. Ein unerwarteter Fehler nennt nur seine Art und den
Eintrag, nie seine Meldung, denn die könnte einen Wert zitieren, dazu, ob dieser
Eintrag danach gespeichert ist, und `home secrets --setup` zum Weitermachen;
vorher gespeicherte Einträge bleiben.

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
`retired_on = 2026-10-14` und `revoke_url`, nach geprüftem Widerruf auch
`revocation_confirmed = 2026-10-15`; eine abgeleitete Kopie trägt
`mirror_of`. `optional = true` heißt: Fehlen ist kein Fund, `--setup`
überspringt den Eintrag; so stehen auch die bekannten Dateien ohne Geheimnis
(`~/.secrets/README`, `claudebot-runtime.env`) im Manifest. `elsewhere` nennt Kopien außerhalb des Homes für die
Spalte „Ort“. `purpose` sagt in einem Satz, wofür es da ist. Ein Token, durch
das `--setup` führt, nennt dazu `provider` (`github` oder `cloudflare`),
`resource` (GitHub: der Resource owner; Cloudflare: die Zone), `scope` (die
Rechte; bei GitHub als `<URL-Parameter>:read|write|admin`) und
`expires_in_days` (1 bis 366), alle vier zusammen; ein `service-password`
hat genau einen Pfad ohne Platzhalter und wird erzeugt. Ein unbekannter Schlüssel (auch ein Versuch, einen Wert
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
| `missing-readme` | `~/README.md` fehlt |
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
`home tidy` genau einen Fund, `missing-readme`: „README.md fehlt“;
`unknown-home-entry` läuft dann nicht. Wer einen Ordner im Home anlegt, trägt ihn in `~/README.md` ein — eine zweite Liste gibt es nicht.

Der Bericht listet außerdem, welche Regeln liefen und nichts fanden
(„Geprüft, nichts gefunden: …“) und welche gar nicht laufen konnten, etwa
weil `docker` fehlt oder `du` fehlschlug („Nicht geprüft: docker (nicht
installiert)“) — das eine ist kein Fund, das andere ist keine Prüfung.

## home setup

Richtet den globalen Commit-Hook gegen Geheimnisse ein.
Then it runs the system step.
`home setup` zeigt nur, was es ändern würde; `home setup --apply` führt es der
Reihe nach aus und hält beim ersten Fehler an, mit dem Namen des Schritts.
The activation comes before the system step, so a wrong sudo password never
keeps the commit hook from being active:

1. **quality.** `uv tool install --force` mit `QUALITY_SOURCE` aus
   `src/home_tools/setup.py`, einem Commit-SHA von overnightworks/quality (ein
   Tag gibt es noch nicht; eine neue Version ist eine Zeile). Der Schritt
   läuft auch, wenn `~/.local/bin/quality` fehlt oder nicht ausführbar ist.
2. **Engines.** `quality engines install` nach
   `~/.local/share/quality/engines`, wenn `quality engines verify` es verlangt;
   danach gilt der Schritt erst, wenn `verify` besteht.
3. **Hook-Dateien.** `pre-commit` und `pre-merge-commit` in
   `~/.local/share/home/git-hooks/`, atomar geschrieben, Modus 755. Ein
   Inhalt, der abweicht, ein verlorenes Ausführungsrecht oder ein anderer Modus als 755 wird
   repariert.
4. **Aktivierung.** `git config --global core.hooksPath` auf dieses
   Verzeichnis. It comes after the three steps above, so a failure in one of
   them never activates a hook whose scanner is missing.
5. **Ansible.** `uv tool install --force` with `ANSIBLE_SOURCE` from
   `src/home_tools/setup.py`, a pinned `ansible-core`; like quality, it runs
   again when `~/.local/bin/ansible-playbook` is missing or uv recorded
   another version.
6. **System.** The playbook `src/home_tools/system/playbook.yml` installs
   edge's files from `~/git/edge/tunnel/`, see below. It asks once for sudo.

Each step reports `changed` or `unchanged` with `--apply`; a dry run reports
`would change` or `unchanged`, and the system step also `not checked (needs
sudo at a terminal)`, or `not checked (needs Ansible, …)` before the Ansible
step has installed it. A second `--apply` changes nothing and downloads nothing. Der Hook ist die Vorlage `src/home_tools/hooks/commit-hook`
(POSIX sh). Er ruft `quality scan-staged`, führt danach den eigenen
`.git/hooks/<name>` des Repositorys aus (auch im Worktree, über
`--git-common-dir`) und scannt danach noch einmal, weil ein Repository-Hook
Dateien stagen kann. Exit `0` heißt sauber, `1` ein Geheimnis gefunden, `2` nicht
beurteilbar; jeder Wert ungleich `0` bricht den Commit ab. Fehlt `quality`,
bleibt der Commit gesperrt, bis `home setup --apply` es wieder einrichtet. Das
ist gewollt.

### The system step

edge ships the tunnel's files in `tunnel/`; the system step copies them, with
their placeholders filled, to:

- `/etc/systemd/system/cloudflared.service`, the clean base unit;
- `/etc/systemd/system/cloudflared.service.d/20-nonroot.conf` and
  `30-edge-global-stop.conf`, so the tunnel stays down after a reboot while
  `~/.local/state/edge/global-stop` exists;
- `/etc/polkit-1/rules.d/10-edge-cloudflared-stop.rules`, so you may *stop*
  `cloudflared.service` without a password, and only stop.

All four are root-owned, mode 644. `@HOME@` and `@OPERATOR_USER@` are your
home and your user; `@TUNNEL_ID@` is `tunnel:` in `/etc/cloudflared/config.yml`,
and without it the step fails before writing anything. Once the new base unit
is in place, `10-root-binary.conf` is deleted. A change ends in one
`systemctl daemon-reload`; cloudflared is never started, stopped or restarted.
Whatever is replaced or deleted is copied first to
`/var/backups/home-setup/<UTC time>/`, outside `/etc/systemd/system`.

- **sudo.** At a terminal, both `home setup` (Ansible's `--check --diff`) and
  `home setup --apply` ask once for the sudo password. Without a terminal,
  the dry run says `System: not checked (needs sudo at a terminal)` and
  `--apply` refuses the step and names the command; neither waits for a prompt.
- **home next.** After a verified run (an `--apply`, or a dry run at a
  terminal that says `unchanged`), the step records a digest of edge's files
  and the filled-in values in `~/.local/state/home/system-step.sha256`.
  `home next` compares only that record, so it never asks for sudo: it shows
  the step until a verified run, and again when edge's files or the values
  change. A hand edit in `/etc`, or a new tunnel id, only `home setup` at a
  terminal sees.
- **Rollback.** Restore the previous `cloudflared.service`,
  `10-root-binary.conf` and `20-nonroot.conf` from the backup (their content
  before the first run is in the inventory of #9). Restore
  `30-edge-global-stop.conf` from the backup too, or remove it when the backup
  has none, because the step created it; until then the global-stop condition
  stays in force. Then run `sudo systemctl daemon-reload`, and remove
  `/etc/polkit-1/rules.d/10-edge-cloudflared-stop.rules`.
- **A failed `--apply`.** Files are copied in the order above, so some may already be in place, and
  `10-root-binary.conf` is deleted only after all four are. The `daemon-reload` handler does not run
  after a failure, so systemd still runs its old configuration. Two cases:
  - A failure while copying leaves some files in place and `10-root-binary.conf` still there. Run
    `home setup --apply` again: it copies the rest, deletes the drop-in and reloads systemd.
  - A failure in the reload itself leaves every file in place, `10-root-binary.conf` already deleted
    and systemd not reloaded. A retry then finds nothing to change, reports unchanged and does not
    reload. Run `sudo systemctl daemon-reload` once and check that it succeeds.

  Or roll back as described.
- **Proof.** `scripts/system-proof/run.sh <edge checkout> <evidence dir>` drives
  the step in a throwaway systemd container, never on this PC.

### Was der Hook nicht garantiert

Die Zusage ist enger als „jeder Commit“:

- Nicht erfasst: `--no-verify` bei `git commit` und `git merge`; Commits aus
  rebase, cherry-pick, revert und `git am`; Plumbing wie `git commit-tree`; ein
  einzelner Befehl mit `-c core.hooksPath=…` oder `GIT_CONFIG_*` (songmakers
  Deploy-Skript tut das absichtlich); ein anderer OS-Benutzer oder ein anderes
  HOME.
- Rückhalt: die Agentenregel „nie `--no-verify`“ (`AGENTS.md`), Push Protection
  auf den öffentlichen Repositories und das CI-History-Gate dort, wo es
  eingeführt ist.
- Eine Allowance im selben Commit gilt. Kontrolle ist das Review dieses Diffs:
  ein Allow-Eintrag steht dort als sichtbare Zeile mit Hash und Grund.
- Der Framework-Hook von hopin (`.pre-commit-config.yaml`) läuft hier nicht mit.

## Konfiguration

Eine Datei für alle Befehle: `~/.config/home/config.toml`, eine Tabelle je
Befehl mit Einstellungen. Ohne die Datei gelten die Defaults. Eine unbekannte
Tabelle, ein unbekannter Schlüssel, ein negativer Tageswert oder ein falscher
Werttyp ist ein Konfigurationsfehler: `home` meldet ihn auf stderr und beendet
sich mit Exit-Code 2, statt mit falschen Grenzen weiterzulaufen (`home next`
zeigt ihn stattdessen als Schritt und endet mit `0`, siehe dort).

Liegt noch die alte `~/.config/tidy-home/config.toml` und die neue fehlt,
bricht `home` ebenso mit Exit-Code 2 ab (`home next` auch hier als Schritt,
siehe dort) und nennt den genauen Befehl, der die alte Datei einmalig unter
`[tidy]` in die neue umzieht — es gibt keinen stillen Rückfall auf die Defaults.

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
cd ~/git/edge
uv sync --locked
ln -s ~/git/edge/bin/home ~/.local/bin/home
systemctl --user link --force ~/git/edge/systemd/home-check.service ~/git/edge/systemd/home-check.timer
systemctl --user enable --now home-check.timer
```

`uv sync --locked` baut die `.venv` des Checkouts. `bin/home` ist ein Link auf
den Starter `bin/edge`, der `.venv/bin/home` des Checkouts ausführt; der Link
unter `~/.local/bin` muss deshalb `home` heißen. Die systemd-Unit ruft
`~/git/edge/.venv/bin/home` direkt auf und hängt so an keinem Link. `home`
läuft mit dem Python der `.venv` (heute 3.14; `requires-python >= 3.12`). Das
Paket ist editierbar installiert: eine Änderung unter `src/` wirkt ohne
Neuinstallation; nach einem Pull, der Abhängigkeiten ändert, frischt
`uv sync --locked` die `.venv` auf, bevor der Timer sie sieht.

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
