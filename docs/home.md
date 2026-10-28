# home-tools

Ein Befehl, `home`, zeigt, was am PC von dir gebraucht wird, prüft ihn täglich und führt durch die Geheimnisse.
Jeder Unterbefehl erklärt sich mit `home <command> --help` samt Beispielen;
dieses README ist die lange Anleitung.

Alle Befehle außer `home next` (siehe dort) teilen sich die Exit-Codes: `0` wenn alles sauber ist, `1` sobald
es einen Fund gibt (das ist kein Fehler — `systemd/hal-check.service` trägt
`SuccessExitStatus=1` dafür), `2` wenn das
Geheimnis-Manifest ungültig ist (`hal check` kennt zusätzlich die Konfigurationsdatei, siehe „Konfiguration“), `3` wenn ein Lauf an einem unerwarteten Fehler
zerbrach; dann steht eine `error:`-Zeile auf stderr. Bei `2` und `3` meldet der
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
1. home secrets --setup (eigenes Terminalfenster) — erwartet: cloudflare/fleet-read verified ~/.secrets/cloudflare/fleet-read (600); github/fleet-read verified ~/.secrets/github/fleet-read (600); opentofu/state-passphrase generated ~/.secrets/opentofu/state-passphrase (600) · never shown; then hal apply
2. https://sonarcloud.io/account/security (von Hand, für sonarcloud/token) — erwartet: home secrets zeigt sonarcloud/token present
3. hal setup --apply (eigenes Terminalfenster) — erwartet: Aktivierung, System: changed; it asks once for sudo
   warum: without the hook no commit is checked for secrets; without it the tunnel ignores edge's global stop after a reboot, and only root may stop it
Außerdem zum Einsortieren: 4 unknown files in ~/.secrets. Das macht ein Agent: sag „räum home auf“.
```

Ein Schritt ist etwas, das nur du tun kannst: ein geführter Befehl an deinem
Terminal (`home secrets --setup`, `hal setup --apply`),
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
von Hand (etwa „~/.ssh/id_ed25519: check the parent folder's permissions“). Dann
offene Schritte von `hal setup` (dann `hal setup --apply`) und die Korrekturen
der Hook-Prüfung.
Jeder Befehl steht nur einmal da.

Die Schlusszeile zählt die Buchhaltung je Art, in fester Reihenfolge und ohne
Pfad: unknown files in `~/.secrets`. Die sortiert ein Agent ein; jede
Löschentscheidung kommt als eine Liste zu dir. Die einzelnen Funde zeigen
`hal check` und `home secrets`.

Gibt es nichts, steht da „Nichts zu tun.“. Gibt es nur Buchhaltung, steht da
„Nichts für dich zu tun.“ und darunter die Schlusszeile. Kann `home next` eine
Quelle nicht lesen (etwa ein ungültiges Manifest), steht ganz oben ein Schritt
„1. konnte die Geheimnisse nicht lesen; den Fehler zeigt: home secrets“, und die
Schritte der anderen Quellen folgen.
Es ändert nichts, fragt nach keinem Passwort und liest keinen
Geheimniswert (nur `stat`, wie `home secrets`); Agenten dürfen es fahren.
Exit-Code: immer `0`, auch mit Schritten, denn es ist eine Ansicht, keine
Prüfung; `3` nur bei einem unerwarteten Fehler.

## hal check

```
hal check           # alle Prüfungen lesend, ein Bericht, höchstens eine Benachrichtigung und eine Mail
```

`hal check` fährt jede Prüfung, die nichts ändert — die Ansicht von
`home secrets`, die Hook-Prüfung, die Log-Prüfung, die Backup-Prüfung und die Plattform-Prüfung — und schreibt einen gemeinsamen
Bericht nach `~/.local/state/hal/check-report.md`; dort stehen die Zahlen und jeder Fund im Einzelnen.

Von Hand getippt liefert ein nacktes `hal check` Neuigkeiten aus wie der Timer: derselbe Bericht, dieselbe Mail und Benachrichtigung, dasselbe Merken der Funde; einen Probelauf ohne Zustellung gibt es nicht. Exit 0 sauber, 1 Funde, 2 Manifest oder `config.toml` sind ungültig (`error: <Datei>: …`), 3 ein Bereich konnte nicht prüfen, 130 unterbrochen; `--json` gehört nur zu `hal check exposure` und `hal check hosts` (Exit 2).

Auf dem Terminal steht davon eine englische Zeile je Bereich in der Reihenfolge der Mail (`ok`, `N action(s)`,
`N note(s)` mit dem Satz des ersten Funds und `+N more`, oder `not checked`) und ein Urteil, das sagt, ob eine Mail
rausging und wo der Bericht liegt; `next:` nennt den Schritt des ersten Handlungsbedarfs. Ein Bereich, der nicht
prüfen konnte, endet den Lauf mit `error:` und Exit 3.

**Stufen.** Jeder Fund hat genau eine Stufe. *OK*: nichts gefunden. *Note*: kann warten, nichts geht heute
kaputt, nichts liegt offen. *Action needed*: etwas ist jetzt kaputt, ungeschützt oder unsichtbar. Der Stand eines
Bereichs ist die schlimmste Stufe seiner Funde, der des PCs die schlimmste seiner fünf Bereiche (Services, Backup,
Commit protection, Secrets, Server logs). Die Stufe und der Satz je Regel stehen in `RULE_REPORTS` in
`check.py`; ein Test lässt jede Regel der Prüfungen ohne Stufe scheitern.

| Prüfung | Action needed | Note |
|---|---|---|
| `platform` | `container-restarting`, `container-dead`, `container-unhealthy`, `container-exited`, `docker-unreadable`, `disk-low` | `reboot-pending` |
| `backup` | `backup-stale`, `backup-failed`, `backup-status-unreadable`, `backup-crashed` | – |
| `hook` | `hooks-path`, `hook-file`, `quality`, `engines` | `hidden-hook` |
| `secrets` | `loose-mode`, `symlinked-folder`, `revocation-unconfirmed`, `verification-failed`, ein fehlendes Passwort eines Menschen (`SecretKind.HUMAN_PASSWORD`, als Regel `missing-password` gemerkt) | `missing` (sonst), `not-a-folder`, `closed-folder`, `presence-unknown`, `verification-unreachable`, `unknown-file` |
| `logs` | `journal-age`, `journal-unreadable`, `docker-unreadable` | `container-json-file`, `docker-default-driver`, `rsyslog-file` |

Ein zerbrochener Lauf einer Prüfung ist Action needed („The … check did not run“, Schritt `hal check`).

**Wann gemeldet wird.** Eine Desktop-Benachrichtigung (über `notify-send`, falls installiert) und eine Mail kommen nur,
wenn eine Action-needed-Stufe neu auftaucht oder verschwindet, auch wenn danach nichts mehr übrig ist; ein zerbrochener oder
wieder laufender Lauf einer Prüfung ist so einer. Ändern sich nur Notes, kommt nichts. Ein Fund ist Regel plus
Gegenstand: Derselbe Fund, der nur älter oder größer wird, nervt nicht erneut; wechselt ein Container von „startet
neu“ zu „beendet“, ist das ein neuer Punkt.

Betreff und Titel der Benachrichtigung sagen den Stand des PCs in einem Satzteil: „PC check: action needed –
Services, Backup“ (mit den Bereichen im Action needed), „PC check: all well – notes only“ oder „PC check:
all well“. Der Text der Benachrichtigung sind die Sätze der Action-needed-Funde und der Pfad des Berichts. Die
Benachrichtigung trägt den Knopf „Open report“; ein Klick öffnet den
Bericht mit `xdg-open`. Gewartet wird nicht im Check, sondern in einer
losgelösten Einheit (`systemd-run --user --collect`), sodass `hal check` und
der Timer-Dienst sofort enden.

Die zuletzt gemeldete Fundmenge hält `~/.local/state/hal/check-findings.txt`. Der saubere Stand wird ebenfalls
gemerkt, sodass ein wiederkehrender Action-needed-Fund wieder meldet. Scheitert eine fällige Benachrichtigung, bleibt die
zuletzt gemeldete Fundmenge stehen, und der nächste Lauf meldet, was dann noch von ihr abweicht.

**Die Mail.** Sie geht über `msmtp`, als du, mit dem Konto aus `~/.secrets/smtp/account.toml`, und nur, wenn diese Datei
existiert; fehlt sie, schickt `hal check` nichts und meldet keinen Fehler (der Tagesbericht listet die fehlenden
Einträge). Der Text hat oben jeden Fund mit Action needed als Satz mit genau einem nächsten Schritt (neue Punkte mit „(new)“,
höchstens 20, dann „… and N more, see report“), darunter was nicht mehr ansteht, dann eine Zeile je Bereich und
den Pfad des Berichts:

```
Subject: PC check: action needed – Services

Action needed

1. presentator-live keeps restarting and is therefore unreachable. (new)
   Next step: Tell an agent: "presentator-live keeps restarting, please look".

How the PC stands (2026-10-26 14:54)

Services: Action needed
Backup: OK, last backup today 14:15
Commit protection: OK
Secrets: Note – Credentials for hopin are still missing
Server logs: OK

Notes are not urgent; home next shows what you can do.
Everything in detail: ~/.local/state/hal/check-report.md
```

Keine Zahl, nie ein Wert, Name oder Pfad eines Geheimnisses, nie eine Datei aus dem Home: Genannt werden nur das Compose-Projekt eines
Containers, Apps (aus `read_by` des Manifests, nur einfache Namen wie `hopin`) und der Zeitpunkt der letzten Sicherung; bei Geheimnissen, Commit-Schutz und Logs steht nur, was los ist. Ein
ungültiges Konto schickt nichts und nennt Schlüssel und Regel auf stderr, nie den Wert.

**Welcher Kanal zählt.** Der Desktop zählt nur, solange eine grafische Sitzung läuft (`systemctl --user is-active
graphical-session.target`, nur geprüft, wenn es etwas zu melden gibt), die Mail, solange das Konto existiert. Jeder
eingerichtete Kanal wird versucht; scheitert einer, steht eine Zeile auf stderr, der Exit-Code bleibt, und die gemeldete
Fundmenge rückt nicht vor, sodass der nächste Lauf erneut meldet. Sie rückt vor, wenn jeder eingerichtete Kanal
geliefert hat: ein Lauf ohne Sitzung (Linger, 09:00) mit gelieferter Mail genügt. Ohne einen einzigen eingerichteten Kanal
bleibt sie stehen, ohne Ausgabe. `[notify] enabled = false` schaltet beide ab.

Zerbricht eine Prüfung (etwa ein Ordner unter `~/.secrets`, den `home` nicht
lesen darf), steht an ihrer Stelle eine Zeile „check secrets failed
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
  genannte Datei bearbeiten oder `hal setup --apply`).
- **Hook-Datei falsch.** `pre-commit` oder `pre-merge-commit` fehlt, hat nicht den Modus 755
  oder weicht vom gerenderten Text ab (`hal setup --apply`).
- **quality nicht wie festgelegt.** `~/.local/bin/quality` fehlt, ist nicht ausführbar oder uv hat
  nicht `QUALITY_SOURCE` aufgezeichnet (`hal setup --apply`).
- **Engines.** `quality engines verify` scheitert (`hal setup --apply`).
- **Verdeckter Repository-Hook.** `.git/hooks` enthält eine Datei außer
  `*.sample`, `pre-commit` und `pre-merge-commit`; der globale Pfad verdeckt sie.

Was die Prüfung nicht sieht, weil es flüchtig ist oder außer Reichweite liegt:

- ein einzelner Befehl mit `-c core.hooksPath=…`, `GIT_CONFIG_PARAMETERS`,
  `GIT_CONFIG_COUNT` oder `GIT_CONFIG_GLOBAL`;
- `--no-verify`;
- Repositories außerhalb von `~/git`;
- ein `includeIf "onbranch:…"`, das nur für den Branch gilt, der beim Lauf von
  `hal check` ausgecheckt ist.

Kann die Prüfung nicht laufen (git lässt sich nicht starten), gilt sie als
zerbrochen, nie als sauber.

### Die Prüfungen `backup` und `platform`

Beide lesen nur und ändern nichts. Was sie lesen, ist fest: nie ein Container-Log, nie die Umgebung eines Containers, nie
`~/.secrets` oder `~/backups`. Der Bericht trägt nur Namen, Zustände und Zeiten. Ein Fund ist Regel
plus Gegenstand (Containername, Appname); sein Alter gehört nie dazu. Lässt sich die Quelle nicht lesen, ist das ein
eigener Fund, nie ein sauberer Lauf.

- **`platform`, Container.** Jeder Container mit nicht leerem Compose-Projekt-Label (`docker inspect --format` mit
  benannten Feldern, darunter das Projekt, ein Aufruf für alle; der Fund trägt den Projektnamen): `container-unhealthy`, `container-restarting`, `container-dead` und
  `container-exited` mit Code ungleich 0 (alle Action needed). Exit-Code 0, `starting` und ein Container ohne Healthcheck sind keine Funde,
  ein entfernter Container auch nicht. Gesunde erscheinen nur als Zahl. Ein Container einer App, die mit `hal stop`
  abgeschaltet ist (eine Datei unter `~/.local/state/edge/stopped/`; nur der Name wird gelesen), ist mit jedem Code ungleich 0
  kein Fund, und der Bericht nennt die Schalter; `container-restarting`, `container-dead` und `container-unhealthy` bleiben
  Funde. Eine Einheit von edges eigenem Stack (Projekt `edge`) erscheint nach `hal stop` weiter. `docker-unreadable`, wenn Docker nicht lesbar ist.
- **`platform`, Neustart.** `/var/run/reboot-required`: nur Dasein und Änderungszeit (`reboot-pending`, „seit
  Datum“); ein Fund der Stufe Note, der nie eine Mail auslöst.
- **`platform`, Festplatte.** Nur die Platte, auf der `/` liegt (`shutil.disk_usage`: was ein unprivilegierter Prozess
  noch nutzen kann): `disk-low` (Action needed), wenn weniger als 10 % frei sind; genau 10 % ist kein Fund. Der Fund
  hat den Gegenstand `/`, der freie Platz gehört nicht zur Identität. Die Mail nennt keine Zahl, der Bericht GB frei,
  GB gesamt und den Anteil. Lässt sich die Platte nicht lesen, läuft die Prüfung `platform` nicht.
- **`backup`.** `~/.local/state/edge/backup-status.json` (Schema 2, nur die dokumentierten Felder): `backup-failed`,
  wenn der letzte Lauf `failed` ist; `backup-crashed`, wenn `running` länger als 6 Stunden her ist; `backup-stale` je
  App, wenn `last_successful_backup` älter als 8 Tage ist oder fehlt; `backup-status-unreadable`, wenn die Datei
  fehlt, nicht lesbar ist (ein Symlink an ihrer Stelle wird nicht verfolgt), ein anderes Schema oder einen Wert außerhalb
  seiner dokumentierten Werte hat. Die 8 Tage und 6 Stunden sind Konstanten in `backup_check.py`.

### Die Prüfung `logs`

Auf diesem PC gilt für jedes Server-Log die Grenze von 30 Tagen
(`LOG_RETENTION_DAYS` in `system_step.py`). Die Prüfung `logs` berichtet Alter
und Treiber jedes Speichers, liest nie einen Nachrichtentext und ändert nichts:

- **Container mit `json-file`.** Jeder Container in jedem Zustand, dessen
  Log-Treiber `json-file` ist, mit Namen und Compose-Projekt. `journald` und
  `none` sind keine Funde.
  Jeder Einmal-Container von edge (`keycloak-config-cli`, `realm-observer`,
  `agent-smoke`, die Einmal-Container von `step-ca/init` und der Sicherung, die
  Listener-Probe) startet mit Treiber `none`: Seine Ausgabe gehört allein dem
  Aufrufer und gelangt nicht ein zweites Mal ins Journal.
- **Docker-Standardtreiber** (`docker info`) außer `journald`.
- **Journal zu alt.** Der älteste Eintrag laut `journalctl --list-boots -o json`
  (Zeiten der Boots, nicht der Meldungen) ist älter als `LOG_RETENTION_DAYS`.
- **rsyslog-Dateien.** `syslog`, `auth.log`, `kern.log`, `mail.log`, `mail.err`
  und `ufw.log` samt Archiven unter `/var/log`, nur nach Name und Dasein. Der Fund
  heißt: rsyslog ist zurück (etwa durch ein Paket-Update); der nächste
  `hal setup --apply` entfernt es und die Dateien wieder.

Lässt sich `docker` oder `journalctl` nicht ausführen oder liefert das Journal
kein lesbares Ergebnis, ist das ein eigener Fund (`docker-unreadable`,
`journal-unreadable`), nie ein sauberer Lauf. Das gilt auch für eine
Teilansicht: Ohne Leserecht auf das Systemjournal (`journalctl --system`) sähe
`journalctl` nur das Journal des Benutzers und meldete dennoch Erfolg; die
Prüfung liest deshalb beide Ansichten und nennt die fehlende als Fund. Ein
Container, der zwischen `docker ps` und `docker inspect` verschwindet, gilt als
weg, nicht als Fehler. Der Bericht nennt außerdem je
Speicher den Stand (Journal-Alter, Standardtreiber, Zahl der Container).

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
# home secrets 15.10.2026 09:00 | 2 findings

| Name | Kind | File | Check | Expiry | Place | Recreate |
|---|---|---|---|---|---|---|
| gitlab | token | present | not checked | expiry unknown | ~/.secrets/gitlab | https://code.siemens.com/-/user_settings/personal_access_tokens |
| gh | tool login | present | checked: valid | expiry unknown | ~/.config/gh | `gh auth login --web` |
| secrets/README | documentation | present | not checked | expiry unknown | ~/.secrets/README | ~/.secrets/README anlegen: je Datei eine Zeile, was sie ist (kein Geheimnis) |
| cloudflared | tool login | present | not checked | expiry unknown | ~/.secrets/cloudflared/cert.pem; Tunnel-Config und Zugangsdatei des laufenden Tunnels: /etc/cloudflared (root) | `cloudflared tunnel login` im Browser |
| ssh/id_ed25519.ppk | derived copy | missing (optional) | not checked | expiry unknown | ~/.ssh/id_ed25519.ppk | `puttygen ~/.ssh/id_ed25519 -o ~/.ssh/id_ed25519.ppk` |
| github/token | token | retired | revocation confirmed 15.10.2026 | expiry unknown | ~/.secrets/github/token | revoke: https://github.com/settings/tokens |

## Findings

| Rule | Subject | Finding | Fix |
|---|---|---|---|
| unknown-file | ~/.secrets/cloudflared/old.json | - | add ~/.secrets/cloudflared/old.json to the manifest or delete it |
| loose-mode | ~/.secrets/gmail-cleanup | has mode 775; at most 700 allowed | `chmod 700 ~/.secrets/gmail-cleanup` |

No values: home secrets reads secret files by stat only and never opens them.
```

So liest sich die Tabelle:

| Spalte | Werte |
|---|---|
| Kind | human password, tool login, token, service password, „token, unverified“, derived copy — genau eine je Eintrag; `configuration` und `documentation` für bekannte Dateien unter `~/.secrets`, die kein Geheimnis sind |
| File | `present`, `missing`, `unknown` (das Dateisystem verweigert die Auskunft), `retired`; ein optionaler Eintrag trägt `(optional)` |
| Check | bei einem zurückgezogenen Eintrag `revocation unconfirmed` oder `revocation confirmed <Datum>` |
| Place | die Pfade im Home; liegen Kopien woanders (etwa `/etc/cloudflared`, root), nennt sie die Zelle dahinter, gelesen werden sie nie |
| Check | nur `gh` wird geprüft (`checked: valid`, `checked: invalid`, `check not possible` bei Zeitüberschreitung oder fehlendem `gh`); alle anderen `not checked` |
| Expiry | heute immer `expiry unknown`: ein Datum erscheint erst mit Herkunft (Antwort des Anbieters oder ein vom Assistenten geschriebenes `rotated_on`); das Dateialter zählt nie |

Funde sind: ein fehlender Eintrag (außer einem optionalen) oder ein unbekannter, ein zurückgezogenes
Geheimnis, dessen Widerruf unbestätigt ist, eine fehlgeschlagene oder nicht
mögliche Prüfung, eine Datei oder ein Ordner unter `~/.secrets`, den kein
Eintrag nennt („unknown“; bleibt unangetastet), und Rechte lockerer als
`600` für Dateien oder `700` für Ordner unter `~/.secrets`, ein Ordner dort,
den sein Besitzer nicht lesen, betreten oder beschreiben kann („closed-folder“;
was darin liegt, ist „unknown“), ein Symlink
auf dem Weg zu einem Eintrag („symlinked-folder“; die Empfehlung ersetzt ihn durch
den Ordner, auf den er zeigt, oder durch einen leeren, wenn das Ziel fehlt) und eine Datei, wo auf dem Weg zu einem Eintrag ein
Ordner stehen muss („not-a-folder“; die Empfehlung schiebt sie mit `mv -n` nach
`<Name>.alt`). Die Ordner auf dem Weg beurteilt dieselbe Regel, mit der
`--setup` das Speichern verweigert. `home` setzt nie Rechte und ersetzt nichts;
`chmod` bleibt ein Handgriff.

### Einrichten: `hal setup --apply`

`hal setup` hat dafür den Schritt „Geheimnisse“: Er gilt als erledigt, wenn das Manifest
auf diesem PC kein offenes Geheimnis hat (nur `stat`); offen ist auch ein Eintrag, dessen
Vorhandensein sich nicht feststellen lässt. Ohne `--apply` nennt er die offenen in der Zeile
`! secrets  missing  a, b (by hand), c (unknown)` und ändert nichts; `--apply` zeigt vor dem Assistenten `! secrets  N missing  a, b` und führt ihn dann aus; er braucht ein eigenes Terminal. Der
Assistent ist derselbe wie `home secrets --setup`, und `--setup` unten meint ihn. Ein Eintrag mit
`loosest_mode` landet mit diesem Modus (0644) in seinem 0700-Ordner, alle anderen mit 0600.

`--setup` geht die fehlenden Einträge des Manifests der Reihe nach durch,
zuerst die fünf von `edge external`. Gelesen werden davon drei:
`cloudflare/fleet-read` und `opentofu/state-passphrase` für `plan`, danach
`cloudflare/fleet-write` für `apply`. `github/fleet-read` und
`github/fleet-admin` bleiben im Assistenten, aber nichts liest sie, bis
edge#30 X2/X3 die GitHub-Einstellungen als Code bauen. Es läuft nur am
eigenen Terminal (stdin und stdout); ohne Terminal endet es mit Exit-Code `2`,
bevor es etwas anfasst. Agenten fahren es nie.

Ein Token legt nur an, wer mehr Rechte hat als das Token: GitHub hat für
Fine-grained-Tokens keine API, und für Cloudflare bräuchte der Assistent
einen Admin-Schlüssel auf der Platte. Den Login im Browser macht deshalb der
Mensch, alles andere der Assistent. Je Token:

1. Er sagt, was kommt: eine Zeile `›` mit dem Eintrag und `new token`, darunter `why:` (wofür) und die
   Checkliste der Rechte. Ein erzeugter oder von Hand anzulegender Eintrag druckt nur seine Ergebniszeile.
   Vorher prüft er die Ordner auf dem Weg: Ist einer lockerer als `700`, für
   seinen Besitzer verschlossen, ein Symlink („is a symlink“) oder kein
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
Key-ID und API-Token des TURN-Keys, den nur HopIn nutzt (`cloudflare/turn-hopin/key-id` und
`…/api-token`, Art „token, unverified“), nimmt er ebenso unsichtbar entgegen, nennt vorher, wo
der Key entsteht (Cloudflare › Realtime › TURN, beide Werte zeigt Cloudflare nur einmal), und
bietet die Cloudflare-Startseite an. Er speichert sie ohne Prüfung durch einen Anbieter (`stored`, `not verified`), denn der TURN-Token ist kein Benutzer-Token, und kein Anbieter wird gefragt; `home secrets`
zeigt beide als `not checked`. Ob Cloudflare sie annimmt, zeigt erst HopIn beim Start.
Eine vorhandene Datei bleibt unangetastet; ersetzen ist `--rotate` und kommt
später. Ein Eintrag ohne Assistent (etwa `gitlab`) wird mit seiner Seite aus
`recreate` genannt (`by hand`), ein optionaler, der fehlt, übersprungen (`skipped`, `optional`). Strg-C bricht ab, ohne eine halbe Datei zu hinterlassen
(Exit-Code `130`); der nächste Lauf macht beim ersten fehlenden Eintrag weiter.
Der Assistent druckt kein eigenes Ende; den Ausgang nennt `hal setup --apply` mit seiner `secrets`-Zeile und seinem Urteil.

Jeder Fall druckt eine Zeile `Zeichen Name Ereignis Tatsache` (Ereignisse: `verified`,
`generated`, `stored`, `rejected`, `skipped`, `not checked`, `by hand`, `not stored`); ein Eintrag, der
schon da ist, druckt keine. Nie steht ein Wert in einer Zeile, nur Name, Pfad und Rechte. Ein TURN-Wert
endet mit `✓ cloudflare/turn-hopin/key-id  stored  not verified · ~/.secrets/cloudflare/turn-hopin/key-id (644)`.

Beispiel (gekürzt, ein abgelehnter Versuch):

```
  › github/fleet-read  new token  the link fills name, owner, expiry and rights
    why: Nothing reads it today; edge external needs it again once edge#30 X2/X3 builds the GitHub settings as code (repo settings, rulesets, Actions rules, org secret names, never their values). The file stays, nothing is revoked.
    prefilled: Metadata read, Organization secrets read
    [ ] Repository access: All repositories
    [ ] compare the rights in the form with the line above
    [ ] Generate token, copy the value
    Enter opens the page in your browser:
    opened github.com/settings/personal-access-tokens/new?…
    paste the token (hidden, empty skips):
  ✗ github/fleet-read  rejected  GitHub answers 401 · paste again or leave empty
    paste the token (hidden, empty skips):
  ✓ github/fleet-read  verified  ~/.secrets/github/fleet-read (600)
  ✓ opentofu/state-passphrase  generated  ~/.secrets/opentofu/state-passphrase (600) · never shown
```

`opened` zeigt Host und Pfad der Seite, bei einer Abfrage mit `?…`; öffnet kein Browser die
Seite, steht die ganze Adresse in der gelben Zeile `no browser opened`.

Ist alles gespeichert, folgt dem Urteil von `hal setup --apply` die Zeile
`next: hal apply`; es zeigt den Plan vor seinem Ja. Allein gefahren endet
der Assistent mit der letzten Eintragszeile und dem Exit-Code. Exit-Code: `0` nichts fehlt mehr,
`1` es fehlt noch etwas, `2` kein Terminal, eines, das die Eingabe nicht
verbergen kann, Manifest ungültig oder ein unerwarteter Fehler bei einem
Eintrag, `130` abgebrochen. Strg-C und das Ende der Eingabe nennen den Eintrag, an dem sie trafen.
Ein unerwarteter Fehler nennt nur seine Art und den Eintrag, nie seine
Meldung, denn die könnte einen Wert zitieren, dazu, ob dieser
Eintrag danach gespeichert ist, und `hal setup --apply` zum Weitermachen;
vorher gespeicherte Einträge bleiben.

`--by-service` dreht `read_by` des Manifests um: je Dienst die Geheimnisse,
die er laut Manifest liest, dazu die abgeleiteten Kopien und ihre Quelle. Die
Überschrift sagt „declared dependencies“: Das ist, was das Manifest
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
Spalte „Place“. `purpose` sagt in einem Satz, wofür es da ist. `loosest_mode`
(ein TOML-Oktalwert wie `0o644`) erlaubt den Dateien eines Eintrags ein lockereres
Höchstmaß als 0600, weil ein Container-Nutzer sie lesen muss; `loosest_mode_reason`
sagt in einer Zeile, wer sie liest, und gehört dazu. Mehr als 0644 lehnt das
Laden des Manifests ab. Die Prüfung misst die Datei dann an diesem Maß und
empfiehlt nie ein engeres. Ein Token, durch
das `--setup` führt, nennt dazu `provider` (`github` oder `cloudflare`),
`resource` (GitHub: der Resource owner; Cloudflare: die Zone), `scope` (die
Rechte; bei GitHub als `<URL-Parameter>:read|write|admin`) und
`expires_in_days` (1 bis 366), alle vier zusammen; ein `unverified-token` nennt keinen davon und hat
genau einen Pfad: `--setup` fragt seinen Wert unsichtbar ab und speichert ihn ungeprüft; ein `service-password`
hat genau einen Pfad ohne Platzhalter und wird erzeugt, außer er ist `optional`:
dann schreibt ihn die Einheit aus `recreate`. Ein unbekannter Schlüssel (auch ein Versuch, einen Wert
abzulegen), ein falscher Wert oder ein doppelter Name lässt `home secrets`
mit Exit-Code `2` abbrechen; die Meldung nennt Datei und Schlüssel.

## hal setup

Richtet den globalen Commit-Hook gegen Geheimnisse ein.
Then it links the checkout's user units and runs the system step.
Exit: 0 when nothing failed, 3 for a failed step (`home setup` keeps 1).
`hal setup` zeigt nur, was es ändern würde; `hal setup --apply` führt es der
Reihe nach aus und hält beim ersten Fehler an. Both print four rows (below) and a verdict.
The activation comes before the user units and the system step, so a wrong
sudo password never keeps the commit hook from being active; the user units
come before the system step, which is the one that asks for sudo:

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
5. **User units.** Every unit file in `~/git/edge/systemd/` is linked into
   `~/.config/systemd/user/`, systemd reloads once after a change (also when only a
   unit file changed after a pull), and each timer is enabled and started. A link
   into `systemd/` whose file is gone (a renamed or removed unit) is stopped while
   systemd still has it loaded and unlinked, at the top level and in the
   `*.wants/` and `*.requires/` directories; timers stop before services. A same-name
   entry that is not this checkout's link (a link elsewhere, a mask to `/dev/null`, a
   file) is never replaced, and a retired link whose name the top level now gives to
   such an entry is never stopped: the step fails before it changes anything, names
   the file and leaves the decision to you. A timer that is not active after
   enabling fails the step too. A
   missing `systemd/` directory fails the step as well. A changed `timers` row says
   `N timers` (every timer of the checkout's `systemd/`) and ` · systemd reloaded` when a
   reload ran; an unchanged row stays bare. The step is
   verified when every unit is linked, nothing needs a reload, every timer is
   enabled and active, and no link dangles.
6. **Ansible.** `uv tool install --force` with `ANSIBLE_SOURCE` from
   `src/home_tools/setup.py`, a pinned `ansible-core`; like quality, it runs
   again when `~/.local/bin/ansible-playbook` is missing or uv recorded
   another version.
7. **System.** The playbook `src/home_tools/system/playbook.yml` installs
   edge's files from `~/git/edge/tunnel/` and the journal retention timer, see below.
   It also installs `msmtp` and `ca-certificates` for the mail of `hal check`, purges `rsyslog`
   with its six log files, and makes the journal Docker's default log driver in `/etc/docker/daemon.json`,
   restarting Docker while it does not report `journald`.
   It asks once for sudo.

**Who runs it.** `hal setup` resolves a role from the running user. Exactly the account `agent`
(`AGENT_USER` in `src/home_tools/system_step.py`) is the agent role: quality, Engines, Hook-Dateien and
Aktivierung run, and User units, Ansible, System and Geheimnisse are skipped (`timers  skipped  the operator's step`; the agents hold no secrets), with exit 0 and no sudo; `home next` lists none of them, names no sudo and no own window for the
apply, and lists no secrets. Everyone else, root
and a sudoers-only admin included, is the operator role, and a system step that cannot run still exits 1.
If the running uid has more than one passwd name and one of them is `agent`, `hal setup` does not guess: it exits 1 with a line saying so.

**The rows.** The eight steps print as four rows, in the form of
[docs/design/hal-output.html](design/hal-output.html): `commit hook` (quality, Engines,
Hook-Dateien, Aktivierung), `timers` (User units), `system` (Ansible, System) and `secrets`. A row's
event is its worst step's: failed or not started, then needs you, then changed, then unchanged,
then skipped. With `--apply` a row says `changed` or `unchanged` (`stored` or `complete` for the
secrets), `skipped` for the agent role; a dry run says `will change`, `unchanged`, `not checked`
(`needs sudo at a terminal`, or `needs Ansible, …` before the Ansible step has installed it) or, for the
secrets, `missing` with the names (`(by hand)` for an entry only a person can create, `(unknown)` for one whose
presence `stat` cannot tell). The last line is the verdict, counted over the four rows:
`✓ setup done · 1 changed, 3 unchanged · 47s`, `~ setup plan · 2 open, 2 unchanged · 3s · run hal setup --apply`
or `✓ setup up to date · 4 unchanged · 21s`. The run time is whole seconds rounded down (`<1s` under one second, `1m05s` from
a minute, `1h02m` from an hour) and stands after the counts and before the hint; a failed run ends with the error pair and shows none. A step that fails prints its row as `failed` with its own
sentence, every later row as `not run`, and then `error: setup stopped at <row>` and `next: <command>`:
the command that failed (quality, Engines, Aktivierung, Ansible) or `hal setup --apply`. For the
system row the error says what /etc is left as: `/etc unchanged` after `not started` (no terminal for
sudo) or a failed check, `/etc may be partial` after an Ansible apply that did not finish. The secrets row does not fail when the wizard leaves a secret open: it prints `! secrets  N open  a, b (by hand)` from what the disk shows after the walk, and the error says `setup stopped at secrets · N secret(s) still open`; another wizard exit keeps the `failed` row. With `--apply` at a terminal the `system` row is preceded by `› system  needs sudo` and what the play does, right before Ansible asks for the password; it is not counted in the verdict and is not printed when sudo cannot be asked (no terminal, the agent role) or in a dry run. A second
`--apply` changes nothing and downloads nothing. On a terminal the glyphs, the verdict and the `error:`/`next:`
labels are coloured with the 16 base colours, each stream on its own; `NO_COLOR` (non-empty), `TERM=dumb` and a pipe print the plain bytes. Der Hook ist die Vorlage `src/home_tools/hooks/commit-hook`
(POSIX sh). Er ruft `quality scan-staged`, führt danach den eigenen
`.git/hooks/<name>` des Repositorys aus (auch im Worktree, über
`--git-common-dir`) und scannt danach noch einmal, weil ein Repository-Hook
Dateien stagen kann. Exit `0` heißt sauber, `1` ein Geheimnis gefunden, `2` nicht
beurteilbar; jeder Wert ungleich `0` bricht den Commit ab. Fehlt `quality`,
bleibt der Commit gesperrt, bis `hal setup --apply` es wieder einrichtet. Das
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
is in place, `10-root-binary.conf` is deleted. Every `--apply` reloads systemd; a dry run does not.
An extra reload at the end follows only when an edge file or the retired drop-in changed;
cloudflared is never started, stopped or restarted.
Whatever is replaced or deleted of these files is copied first to
`/var/backups/home-setup/<UTC time>/`, outside `/etc/systemd/system`. The retention units
below and rsyslog's conffiles are home's own and are not backed up. `/etc/docker/daemon.json` is backed up there too, when it changes.

- **Journal retention.** The same run writes `hal-journal-retention.service` and
  `hal-journal-retention.timer` to `/etc/systemd/system` (root, mode 644), enables
  and starts the timer, and reloads systemd on every `--apply`, so an interrupted run
  converges on the next. The timer is `OnCalendar=daily` with `Persistent=true`;
  the service runs `journalctl --rotate --vacuum-time=28d`. Nothing restarts
  journald. `LOG_RETENTION_DAYS` (30) in `src/home_tools/system_step.py` is the
  one owner of the value; 28 is two days less, one for the span of a journal
  file and one of slack.
  - **The bound.** While the PC runs, no journal line is older than 29 days,
    1 hour and 1 minute (the autumn clock change makes one gap 25 hours, and the
    timer's default accuracy is one minute). After an
    outage, `Persistent=true` runs the missed deletion within a minute of
    boot; the lines that came due meanwhile are gone then, which is the ruled
    promise („ok a“, 22.10.2026). Nothing promises a minimum age: the journal's
    own size limits can delete younger lines.
  - **The first run** deletes every journal file older than 28 days.
  - **Rollback.** Reverting the change removes the units from the playbook but
    deletes nothing: the timer keeps running with 28 days. To retire it, make a
    reviewed change to the playbook, in this order: a `systemd_service` task with
    `enabled: false` and `state: stopped` for the timer; an `ansible.builtin.file`
    task with `state: absent` for each of the two units; a reload. Then
    `hal setup --apply` stops the timer, deletes both units and reloads. Deleted
    journal lines do not come back.
- **rsyslog is removed.** After the refusals below pass, the play purges the package `rsyslog` (apt, no autoremove) and then
  deletes `syslog`, `auth.log`, `kern.log`, `mail.log`, `mail.err` and `ufw.log` with every rotation (`name` and `name.*`
  directly under `/var/log`, files only) **without a copy**, not even to `/var/backups/home-setup`: a copy would outlive the
  30 days. `RSYSLOG_FILE_NAMES` in `src/home_tools/system_step.py` is the one owner of the six names; `hal check` reads the
  same list. The purge comes first, so a running rsyslog cannot recreate a deleted file. Both tasks work by state, so a run
  interrupted between them is finished by the next `--apply`, and a second one reports `changed=0`. The dry run lists the
  purge and each file it would delete, and changes nothing. `cloud-init.log`, `dpkg.log` and `alternatives.log` stay.
  journald is not changed: it keeps forwarding to a syslog socket nobody listens on, which is harmless.
  - **Where the lines went.** The journal holds the same lines (journald forwards, it does not move them), minus what the
    retention timer already deleted. `auth.log` held the facilities `auth` and `authpriv` (sudo, polkit, logind, cron):
    `journalctl SYSLOG_FACILITY=4 SYSLOG_FACILITY=10`. `mail.log` and `mail.err` held the facility `mail`:
    `journalctl SYSLOG_FACILITY=2`. `kern.log` held the kernel: `journalctl -k -b all`. Narrow any of them with `--since` or
    `-t <identifier>`, for example `journalctl -t sshd --since yesterday`.
  - **Rollback.** Revert the change, or the next `--apply` purges again; then `sudo apt install rsyslog`. The deleted files do
    not come back. A purge discards a conffile of rsyslog that was modified, and the playbook never copies one first (the
    backup covers edge's files only): `dpkg --verify rsyslog` before the apply names each such file, and a reinstall brings
    back the packaged one.
- **The mail packages.** After the refusals below pass, the play installs `msmtp` and `ca-certificates` with apt, without
  Recommends, so `msmtp-mta` (which would replace the system mailer) stays out; a second `--apply` reports `changed=0`.
  Postfix is not touched. **Rollback.** `sudo apt-get remove msmtp`; `hal check` then reports "mail failed: msmtp could not deliver via" while
  `~/.secrets/smtp/account.toml` exists.
- **The agents' account.** The play creates `agent` and nothing runs as it yet. It has its own group only, a
  locked password, the shell `/usr/sbin/nologin`, a home with mode 0700, no subuid or subgid range
  (`groupadd --gid N agent`, then `useradd --uid N --gid N -K SUB_UID_COUNT=0 -K SUB_GID_COUNT=0`, with N the lowest
  id from 1000 up that no user, group or subordinate id entry has, checked after creation), no linger, and a line in
  `/etc/cron.deny`: an existing one keeps its owner, mode and entries, a new one is `root:root 0644`, and every
  other user keeps today's cron. An `agent` that already exists with an extra group, a subuid or subgid entry
  (by name or by its uid), another shell, a usable password, linger, a home other than `/home/agent`, or a
  home that is a link or not a directory owned by it, a uid another user shares, a gid another group shares or
  a primary group other than the group `agent`, or ids that are not one uid and gid from 1000 up, is refused before
  anything is written, in the dry run too; so is a group `agent` without the account (`sudo groupdel agent` removes it;
  a failed `useradd` removes the group it just made), a new account's home path that is taken, and an
  `agent` listed in `/etc/cron.allow`. A `cron.allow` without it denies it already, and `cron.deny` stays as it is.
  The proof checks these file states; that `crontab` itself refuses the agent is not proven, the proof image has no cron.
  A revert keeps the user and its home.
- **sudo.** At a terminal, both `hal setup` (Ansible's `--check --diff`) and
  `hal setup --apply` ask once for the sudo password. Without a terminal,
  the dry run says `! system  not checked  needs sudo at a terminal` and
  `--apply` ends the row `✗ system  not started` with `error: setup stopped at system · /etc unchanged`
  and `next: hal setup --apply`; neither waits for a prompt.
- **home next.** After a verified run (an `--apply`, or a dry run at a
  terminal that says `unchanged`), the step records a digest of edge's files,
  the playbook and the filled-in values in `~/.local/state/home/system-step.sha256`.
  `home next` compares only that record, so it never asks for sudo: it shows
  the step until a verified run, and again when edge's files, the playbook or the values
  change. A hand edit in `/etc`, or a new tunnel id, only `hal setup` at a
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
  after a failure, so systemd may still run its old configuration. Two cases:
  - A failure while copying leaves some files in place and `10-root-binary.conf` still there. Run
    `hal setup --apply` again: it copies the rest, deletes the drop-in and reloads systemd.
  - A failure in the reload itself leaves every file in place, `10-root-binary.conf` already deleted
    and systemd not reloaded. Run `hal setup --apply` again: it reloads systemd on every `--apply`,
    whatever else it finds to change.

  Or roll back as described.
- **Docker's log default.** The same run merges `"log-driver": "journald"` into `/etc/docker/daemon.json` (`DOCKER_LOG_DRIVER`
  in `src/home_tools/system_step.py`, which `hal check` reads too). Every other key stays, such as `runtimes.nvidia`; the file is
  written root-owned, mode 644, with sorted keys, so the diff of a file that only holds the runtime is one new line, and it is
  checked with `dockerd --validate` before it replaces the old one. A file that does not parse, one that is not an object, a
  `log-opts` key, another `log-driver`, and a Docker that does not answer `docker info` are refused before anything is
  written, edge's files included; the dry run shows the diff and the restart as changes and writes nothing.
  Docker restarts whenever `docker info` does not say `journald`, never while it does, so a run interrupted after the write
  restarts on the next. `live-restore` is off: every container stops with Docker, and one with `unless-stopped` or `always`
  starts again, one without a restart policy or stopped by hand stays down; the apps are unreachable meanwhile and running work
  is cut. `hal setup` does not wait for it, so run `--apply` at a quiet time. Existing containers keep `json-file` until they are recreated.
  **Rescue.** When the restart fails or Docker does not report `journald`, the run writes `daemon.json` back without `log-driver`
  (`{}` when nothing else remains; the same JSON as before the run, unless the file already carried `journald`), clears the
  unit's start limit with `systemctl reset-failed docker`, restarts Docker again and fails, saying so. No backup is read and
  nothing is deleted. **Rollback.** Revert the change, land a reviewed playbook change that removes `log-driver` and restarts
  Docker, and run `hal setup --apply`; a plain revert removes nothing. **Residual risks:** Docker stays down, and every app
  with it, when the rescue's restart fails too; and a run interrupted between the write and the rescue with Docker down
  (a hanging restart skips the rescue on Ctrl-C) makes the next apply refuse, because Docker does not answer. Then remove
  `log-driver` from `/etc/docker/daemon.json` by hand; the backup holds the old bytes.
- **Proof.** `scripts/system-proof/run.sh <edge checkout> <evidence dir>` drives
  the step in a throwaway systemd container, never on this PC. Its cells D1-D8 also drive the mail of `hal check` there:
  the real `msmtp` against a throwaway SMTP sink and a test CA, with STARTTLS, implicit TLS, no TLS, an untrusted certificate,
  a rejected login and a stalled server. Its cells R0-R7 drive rsyslog's removal there: the image carries rsyslog, an
  unconfigured postfix and kerneloops like the PC, the six files with archives are planted before the first refused run, and
  the cells prove that a refused run and the dry run delete nothing, that the purge and the deletion are complete without
  touching the installer's logs, that mail lines stay in the journal, that a retry converges, that kerneloops restarts without
  `kern.log`, and that `apt install rsyslog` restores a working package. The image is Ubuntu 24.04, so the proof does not run
  the removal's maintainer scripts of the release on the PC nor its systemd; the PC's own facts cover that.
  The container runs a real Docker engine (its images and snapshots live in anonymous volumes that `run.sh` removes with the container);
  cell DK1 proves, before the first apply, that it answers with the default `json-file` log driver, runs an imported rootfs
  offline and starts without `/etc/docker/daemon.json`. DK2 proves the merge (no file, and the PC's 128-byte file with its
  backup), DK3 the refusals and a Docker that does not answer, DK4 the dry run and the restart on need only, DK5 that a
  running `unless-stopped` container comes back, one stopped by hand stays down and both keep `json-file`, DK6 that a
  container created afterwards is read by `docker logs`, `docker compose logs` and `journalctl CONTAINER_NAME=`, and DK7 the
  rescue, against a proof-only `docker.service` drop-in whose flag contradicts the file. The proof runs the 24.04 package's
  Docker without a GPU, not the PC's. The agents' refusal cells A1-8 and A1-9 run against the engine's own `docker.socket`.

### Das Mail-Konto von `hal check`

Zwei Dateien, beide von Hand angelegt, beide Modus 0600, beide im Geheimnis-Manifest:

- `~/.secrets/smtp/account.toml` mit genau den Schlüsseln `host`, `port`, `user` (zugleich Absender) und `to`; nie das
  Passwort.
- `~/.secrets/smtp/password`: ein App-Passwort nur dafür, sonst nichts.

`msmtp` läuft als du und liest das Passwort beim Senden selbst (`--passwordeval`); `home` öffnet die Datei nie, und das
Passwort steht in keinem Terminal, Log, keiner Mail, keinem Argument und keiner Zustandsdatei. Rotieren heißt die Datei
ersetzen (neues App-Passwort anlegen, das alte widerrufen); die Kopie in songmakers `.env` ziehst du von Hand nach, bis
songmaker die Datei selbst liest. Akzeptiertes Risiko: Jeder Prozess, der als du läuft, Agenten eingeschlossen, kann über
dieses Konto senden; darum ein App-Passwort nur dafür, widerrufbar.

Die Mail geht nur über TLS (STARTTLS auf 587, implizites TLS auf 465), das Serverzertifikat wird gegen den System-Trust-Store
geprüft; ein Server ohne TLS oder mit unbekanntem Zertifikat bekommt weder Mail noch Passwort. Nichts schaltet das ab: keine
Konfigurationsdatei, kein Schalter.

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

Nur `hal check` liest `~/.config/home/config.toml`, eine Tabelle je
Prüfung mit Einstellungen; kein `home`-Befehl liest sie. Ohne die Datei gelten die Defaults. Eine unbekannte
Tabelle, ein unbekannter Schlüssel oder ein falscher Werttyp ist ein
Konfigurationsfehler: `hal check` meldet ihn auf stderr und beendet sich mit
Exit-Code 2, statt mit falschen Einstellungen weiterzulaufen. Eine Tabelle
`[tidy]` wird nicht mehr gelesen und beim Namen abgelehnt: sie ist aus der Datei
zu löschen.

### `[notify]`

| Schlüssel | Default | Bedeutung |
|---|---|---|
| `enabled` | `true` | `false` schaltet Benachrichtigung und Mail von `hal check` ab |

Beispiel:

```toml
[notify]
enabled = true
```

## Installation

```
cd ~/git/edge
uv sync --locked
ln -s ~/git/edge/bin/home ~/.local/bin/home
~/git/edge/bin/hal setup --apply
```

Der Schritt „User units“ von `hal setup` (siehe [hal setup](#hal-setup)) verlinkt die Units aus
`systemd/` und startet die Timer, darunter `hal-check.timer`. `hal` liegt nicht im PATH; der
Starter des Checkouts ruft es auf.

`uv sync --locked` baut die `.venv` des Checkouts. `bin/home` ist ein Link auf
den Starter `bin/edge`, der `.venv/bin/home` des Checkouts ausführt; der Link
unter `~/.local/bin` muss deshalb `home` heißen. Die systemd-Unit ruft
`~/git/edge/.venv/bin/home` direkt auf und hängt so an keinem Link. `home`
läuft mit dem Python der `.venv` (`requires-python >= 3.12`). Das
Paket ist editierbar installiert: eine Änderung unter `src/` wirkt ohne
Neuinstallation. Nach einem Pull, der Abhängigkeiten ändert, `uv sync --locked`
ausführen (oder `home` über `bin/home` aufrufen), bevor der Timer läuft.

Der Timer läuft täglich 09:00 (`Persistent=true` holt einen verpassten Lauf
nach) und fährt nur `hal check`.

## Tests

```
uv run pytest          # nur die Tests
uv run pytest --cov    # zusätzlich die 100-%-Grenze aus pyproject.toml
uv run ruff check .
```

`src/home_tools/` hält 100 %
Zeilen- und Branch-Abdeckung; `fail_under = 100` in `pyproject.toml` lässt
jeden Coverage-Lauf rot werden, der darunter bleibt. Die Layout-Prüfung des
Wurzelverzeichnisses gehört der Qualitätsschranke (`quality / verdict`); was dort liegt und nicht auf
ihrer Basisliste steht, deklariert `quality/config.toml` mit Grund.
`.github/workflows/ci.yml` fährt alles bei jedem Push und Pull Request.
