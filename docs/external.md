# Externe Dienste als Code

Referenz fuer den Betreiber und fuer Entwickler von `external/` und `src/edge/external/`. Sie nennt die
Befehle und was sie garantieren; `edge external --help` und der Code halten den Rest. Wo diese Schicht in
der Plattform liegt: [architecture.md](architecture.md). Der lokale Teil von `edge apply` und die
Publish-Phase: [stack.md](stack.md).

Cloudflare wird mit OpenTofu als Code verwaltet (`external/`; GitHub folgt mit edge#30 X2/X3). Der State
liegt nie im Checkout, sondern verschluesselt in `~/.secrets/opentofu/fleet.tfstate`.

## `edge external plan`

Zeigt, was von den Live-Diensten abweicht. Exit 0 keine Aenderung, 1 Aenderungen offen, 2 nicht moeglich.
Es laeuft aus jedem Checkout und verweigert ein `tofu`, dessen Version nicht dem Pin in
`external/main.tf` entspricht. Es liest zwei Dateien (erwartet wird je 0600, geprueft wird das hier nicht)
und gibt sie nur an das Kind `tofu` weiter, dessen Umgebung eine Allowlist ist (kein `TF_LOG*`,
`TF_CLI_ARGS*`, `TF_ENCRYPTION`, `GITHUB_*`, `GH_*`, Proxy aus der Shell):

| Datei | Variable im Kind |
|---|---|
| `~/.secrets/cloudflare/fleet-read` | `CLOUDFLARE_API_TOKEN` |
| `~/.secrets/opentofu/state-passphrase` | `TF_VAR_state_passphrase` |

Die GitHub-Tokens (`~/.secrets/github/fleet-read`, `fleet-admin`) werden nicht gelesen und nicht
weitergegeben, bis edge#30 X2/X3 die GitHub-Einstellungen als Code bauen; die weiteren Token-Dateien
folgen mit X5b (home-tools#10).

## `edge external check --hosts [--json]`

Liest nur `~/.secrets/cloudflare/fleet-read` und sendet es im `Authorization`-Header an
`api.cloudflare.com`: kein `tofu`, kein GitHub-Token, keine State-Passphrase, kein Schreib-Token, kein
Terminal, keine Abfrage. Das Token steht in keiner URL, keiner Ausgabe und keinem Fehlertext;
Weiterleitungen und der Proxy aus der Shell werden nicht befolgt.

Es liest Zone, DNS-Records, Access-Apps, Access-Gruppen und den Tunnel und vergleicht sie mit den Hosts
aus `fleet.yaml` des laufenden Checkouts: fehlende oder falsche Records und Apps, falsche Policies,
unerwartete Apps an oeffentlichen Hosts sowie Records und Apps, die niemand deklariert hat (`apply`
loescht sie nie; er endet auf demselben Check). Ohne Access auf dem Konto ist das nur dann ein Befund,
wenn ein Host `bypass` oder `protected` ist; widerspricht sich die Abfrage ("Access aus" erst nach den
Apps), ist es Exit 2. Exit 0 keine Befunde, 1 Befunde, 2 nicht pruefbar, 130 unterbrochen (nur vor der
Ausgabe). Mit `--json` ist stdout genau eine Liste von `{problem, subject, detail, next}` (bei Exit 2
leer, die Ursache auf stderr).

Zusaetzlich fragt der Check jeden deklarierten Host mit Record einmal mit `GET /` ueber HTTPS, ohne
Anmeldedaten, Cookies und Weiterleitungen (10 s je blockierender Operation; die Wildcard unter
`edge-host-check.<zone>`, nur solange Access an ist; der Name ist im Katalog reserviert).
`answer-wrong`: die Antwort hat kein `cf-ray`, ein oeffentlicher oder `bypass`-Host leitet auf Access
um, oder die geschuetzte Wildcard leitet nicht auf Access um (oder ist eine Challenge). `unreachable`:
die Anfrage scheiterte. Eine Challenge auf einem oeffentlichen oder `bypass`-Host besteht, ein 5xx mit
`cf-ray` auch: ob der Dienst lebt, sagt `ready`. Als Access-Umleitung zaehlt nur Cloudflares kanonische
Access-URL. Kein Befund zitiert Header, Ziel oder Fehlertext.

## `edge external apply`

Schreibt die Aenderungen auf die Live-Dienste, mit `~/.secrets/cloudflare/fleet-write` (dieselbe
Allowlist-Umgebung, dieselbe State-Passphrase).

- **Wo und wie.** Nur aus `~/git/edge` als Haupt-Worktree auf einem sauberen `main` (sonst Exit 2 mit
  dem Grund) und nur an einem Terminal (stdin und stdout; sonst Exit 1 vor `tofu`). Es plant in eine
  Datei, zeigt den Plan und wendet genau diese Datei erst nach der Eingabe `yes` an (alles andere:
  Abbruch, Exit 1). Ein Plan ohne Aenderungen endet mit Exit 0 ohne Frage. Das schuetzt gegen
  versehentliche und nicht-interaktive Laeufe (CI, Skript, `-auto-approve`); es ist keine
  Identitaetspruefung: ein Agent kann ein pty oeffnen und `yes` tippen, erst D14 macht daraus "nur der
  Betreiber".
- **`ready`-Schutz.** Hat der Plan Aenderungen, fragt `apply` vor dem `yes` jeden aktivierten Host mit
  `ready` (`fleet.yaml`) nach diesem Pfad auf seinem `target` (`GET`, `Host` = FQDN, kein Redirect,
  10 s, kein Retry); nur 2xx zaehlt. Antwortet einer nicht, endet es mit Exit 1 vor dem `yes`: jeder
  nicht antwortende Host steht mit Pfad, Ziel und Status auf stderr, `tofu apply` laeuft nie, die
  Plan-Datei wird geloescht. Das gilt auch fuer eine ganz andere Aenderung (Host gestoppt, abgestuerzt,
  startet noch); `enabled: false` nimmt den Host aus der Pruefung.
- **Die Plan-Datei** liegt verschluesselt (X1a) in `~/.secrets/opentofu/plans/` (0700) und wird nach
  Erfolg, Abbruch, Fehler und SIGINT/SIGTERM geloescht. Eine nach SIGKILL uebrig gebliebene loescht und
  nennt der naechste `plan`- oder `apply`-Aufruf (`check` raeumt nicht auf).
- **Unterbrechung.** Strg-C oder SIGTERM geht genau einmal als SIGINT an `tofu`; der Wrapper wartet, bis
  `tofu` sauber beendet ist, weitere Strg-C werden ignoriert, und die Meldung verweist auf
  `edge external check --hosts`. Die Pruefung laeuft dann nicht von selbst: nichts Ungeprueftes wird
  behauptet.
- **Der Host-Check am Ende.** Jeder `apply`, der etwas geaendert haben kann oder nichts zu tun fand,
  endet auf demselben Check wie `check --hosts` (ohne `--json`; er liest `fleet-read`, das `apply` vor
  dem Plan prueft: fehlt es oder ist es leer, Exit 2 ohne `tofu`; das Schreib-Token erreicht den Check
  nie). Er vergleicht den tatsaechlichen Zustand mit der Deklaration, nicht mit dem Plan: ein geplanter
  Fix entschuldigt nichts, und ein undeklarierter Record laesst auch einen sonst fertigen `apply` mit
  Exit 1 enden.
- **Exit.** 0 angewendet (oder nichts zu tun) und der Check meldet nichts; 1 verweigert, abgebrochen,
  fehlgeschlagen oder der Host-Check meldet etwas oder laeuft nicht; 2 nicht planbar (nie nach einem
  Apply).

## Was `apply` ausserdem setzt

**Die Tunnel-Zeilen.** Eine je Host aus dem Katalog, die 404-Regel zuletzt. Die Konfiguration des
Tunnels gehoert ganz dem Katalog: eine Zeile von anderswo zeigt `plan` als entfernt, und sie
verschwindet nach `yes` (anders als undeklarierte Records und Apps). Das gilt, **solange Cloudflare den
Tunnel verwaltet (`config_src = cloudflare`)**. Solange er `local` ist, bedient cloudflared
`/etc/cloudflared/config.yml`, und die Zeilen, die `apply` schreibt, bedienen nichts. Der Wechsel von
`local` auf `cloudflare` ist einseitig (laut cloudflared-Maintainern), macht der Betreiber
(overnightworks/edge#168), und `apply` loest ihn nicht aus.

**HSTS der Zone** (`cloudflare_zone_setting.hsts`, `security_header`): `max-age=31536000;
includeSubDomains; preload`, auf dem Apex und jeder Subdomain. Die Einstellung allein veroeffentlicht
nichts; die Preload-Liste der Browser kennt die Domain erst nach dem Absenden des Formulars (nicht
umkehrbar fuer Monate). Reihenfolge:

0. Der Betreiber gibt im Cloudflare-Dashboard beiden Tokens das Recht (`fleet-read`:
   `Zone › Zone Settings › Read`; `fleet-write`: `Zone › Zone Settings › Edit`), vor `edge apply`. Der
   Wert des Tokens bleibt dabei erhalten, an den Dateien aendert sich nichts.
1. `edge apply`; die Publish-Phase wendet die Einstellung an, danach meldet `edge external plan`
   "No changes".
2. Der Header `strict-transport-security` erscheint genau einmal auf Apex, `www` und `auth`. Sind es
   zwei, verwerfen zuerst die Ursprungsdienste ihre eigene (in den Items von webauth und Keycloak).
3. `https://hstspreload.org/api/v2/preloadable?domain=hallucinai.de` meldet keinen Fehler.
4. Der Betreiber sendet das Formular auf hstspreload.org ab.

Zuruecknehmen vor Schritt 4: `enabled = false` setzen und anwenden (den Block zu loeschen ist nicht
belegt als Zuruecksetzen bei Cloudflare). Nach dem Absenden reicht das nicht, denn die Domain bleibt auf
den Preload-Listen, bis sie entfernt ist: den Header ohne `preload` ausliefern, das Formular auf
https://hstspreload.org/removal/ absenden und bis zur Entfernung gueltiges HTTPS auf jeder Subdomain
halten; Browser, die den Header sahen, behalten ihn bis zum Ablauf seines max-age.

**Die Client-Adresse** (`cloudflare_ruleset.client_address`, Phase `http_request_late_transform`):
`Forwarded` ist die Adresse, die Cloudflare sah (`for=<IPv4>;proto=https`, bei IPv6
`for="[<IPv6>]";proto=https`); ein vom Besucher gesendetes `Forwarded` wird ueberschrieben. Keycloak
liest nur diesen Header (`KC_PROXY_HEADERS=forwarded`) und speichert in seinen Ereignissen (Anmeldungen
und fehlgeschlagene Anmeldungen, 90 Tage) deshalb die Adresse des Besuchers, nie einen gefaelschten
`X-Forwarded-For`; bei IPv6 mit den Klammern des Headers. Apps, die `X-Forwarded-For` loggen, bleiben
faelschbar; jede liest `Forwarded` oder `Cf-Connecting-IP` selbst.

**Der oeffentliche Ausschluss** (`cloudflare_ruleset.deny_public`, Phase `http_request_firewall_custom`):
Pfade aus `deny_public` eines Hosts (songmaker: `/auth/backchannel-logout`) sind von aussen nie
erreichbar. Eine Block-Regel je Host weist sie bei Cloudflare ab, vor dem Tunnel. Cloudflare
normalisiert die URL vorher ([Doku](https://developers.cloudflare.com/rules/normalization/how-it-works/)),
laesst `%2F` aber kodiert und fuehrt Schraegstriche nur im optionalen Typ "Cloudflare" zusammen, den
die Zone nicht erzwingt. Die Regel dekodiert den Pfad deshalb selbst (`url_decode(..., "r")`) und prueft ihn
mit `wildcard` ([Operatoren](https://developers.cloudflare.com/ruleset-engine/rules-language/operators/)):
case-insensitiv und im Free-Plan verfuegbar, anders als `matches` (ab Business). Jeder `/` des Pfads
wird `/*`: `/auth/`, `//auth/...`, `%2F`, Gross-/Kleinschreibung, `/auth//...`, Punktsegmente und
ein angehaengter Rest werden abgewiesen, auch ein laengerer Pfad mit denselben Segmenten (der
Ausschluss schlaegt eher zu viel zu). Der Free-Plan erlaubt 5 Custom-Regeln, eine je Host. Das
Token-Recht `Zone › Zone WAF` (Lesen fuer `fleet-read`, Edit fuer `fleet-write`) ist nicht gemessen: es kommt vor `hal apply` dazu, wenn ein 403 es nennt.

Die Reihenfolge der Umstellung: erst `hal apply`, dann Keycloak. `hal apply` erstellt Keycloak bei einer
geaenderten Umgebung nie neu; der Betreiber startet es erst, wenn die Regel bei Cloudflare steht
(`docker compose --profile keycloak up -d --no-deps keycloak` in `~/git/edge`), und nur wenn
`hal apply` sauber endete **und** `edge external plan` "No changes" meldet. So gibt es kein Fenster: ein
Keycloak auf `forwarded` ohne Regel davor wuerde ein vom Besucher gesendetes `Forwarded` vertrauen.

Die Token-Rechte, vor `hal apply` im Dashboard: `Zone › Transform Rules › Read` (`fleet-read`) und
`› Edit` (`fleet-write`). `Account › Account Rulesets › Read` ist nicht gemessen: es kommt nur dazu,
wenn ein 403 es nennt; ob es noetig war, steht dann auf #190. Scheitert die Publish-Phase, bleibt
Keycloak auf `xforwarded`: nicht neu erstellen, das Ergebnis auf #190 melden. Bekannte Ursachen:
Cloudflare lehnt den Header ab (Option (b) oder hinnehmen, der Betreiber entscheidet auf #190);
Cloudflare lehnt die `/0`-Listen ab (genauer Fehlertext auf #190); in der Phase besteht schon ein
Ruleset (importieren und seine Regeln vorher in `cloudflare_ruleset.client_address` uebernehmen, der
Plan darf keine Regel entfernen); 403 (ein fehlendes Recht).

Danach prueft der Betreiber (L1): eine fehlgeschlagene Anmeldung durch den Tunnel
(`https://auth.hallucinai.de/realms/hallucinai/protocol/openid-connect/token`, Passwort-Grant mit
falschem Passwort) mit gefaelschtem `X-Forwarded-For` **und** gefaelschtem `Forwarded`. Das Ereignis in
der Admin-Konsole (Admin-Eingang, Events) traegt seine echte Adresse.
