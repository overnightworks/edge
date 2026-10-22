# The people contract: exporting and erasing one person

Reference for an app's head or agent. Question answered: what must my app's admin API offer so that
one person can be exported and erased.

hal is the only caller: `hal export person` and `hal delete person` drive every app through the routes
below. Those two commands are not built yet (#256), and no app admits a bearer token yet (webauth's
bearer admission); this document states the app-side contract as current and every rule about hal as a
requirement on #256 ("hal must …"). Sources: the operator's adoption of "erasure and export are the hal
contract" (presentator#293, 20.10.2026) and the counter-check of songmaker's proposal (songmaker#1714,
22.10.2026).

## Target and caller

- **Target.** `{subject}` is the target person's Keycloak `sub`, compared exactly. hal must resolve a
  name to the `sub` with an exact match before calling any app. A lookup by `{subject}` never creates
  an account in the app.
- **Caller.** A separate identity: the operator, with his own `hal login` access token. The app admits
  it only if the token comes from the realm's issuer, its audience names the app's own client, and its
  `groups` contain `/apps/<app>/admin`; anything else answers `403`. The token authenticates the caller;
  it never names the target. There is no hal service account and no standing secret for these routes.
- An app that admits only browser sessions today says so, and hal cannot call it until it admits the
  token.

## Export

`GET /api/admin/people/{subject}/export` answers a ZIP with `export.json` at its root and the files under
`files/`. The ZIP may stream. `export.json` has these shared keys; everything else is the app's own.

| Key | Content |
|---|---|
| `people_export` | the shape version, the integer `1` |
| `app` | the `<app>` of `/apps/<app>` |
| `subject` | the `sub` |
| `exported_at` | RFC 3339 in UTC with `Z`, whole seconds |
| `data` | the person's data, its structure owned by the app |
| `files` | list of `{path, kind, bytes}`, one per file under `files/`; `path` is relative to the ZIP's root (`files/…`); `[]` when the app has no files |
| `withheld` | list of `{kind, count, reason}`; `reason` is `secret` or `others` |

What the export leaves out (a secret, or data that belongs to other people) is listed in `withheld`, never
dropped silently. An unknown subject answers `200` with empty `data`, `files` and `withheld`, never `404`,
and is never created.

## Erase

`DELETE /api/admin/people/{subject}` erases the person. With `?dry_run=true` it changes nothing and
answers what the erasure would do. Both answer one body:

```json
{"people_erasure": 1, "app": "songmaker", "subject": "3f6c2a7e-…", "dry_run": true,
 "kinds": [{"kind": "song", "count": 12}, {"kind": "generation_audio", "count": 40, "bytes": 412000000}],
 "affects_others": [{"kind": "playlist_entry", "count": 3, "action": "delete"}]}
```

`kinds` counts what belongs to the person (`bytes` where files are involved); `affects_others` counts
what the erasure changes for other people. In a real erasure `dry_run` is `false` and the counts are
what was removed. `people_erasure` is the integer `1`.

- `kinds[].kind` and `withheld[].kind` are the app's own lowercase words; hal only shows them.
- `affects_others[].action` is `delete` (others lose an object that is deleted, a shared one included)
  or `end` (an activity others take part in ends, such as a hosted call).

Rules:

- **Idempotent.** An unknown subject answers `200` with zero counts, never `404`, and the call never
  creates the subject. Running it twice leaves the second run at zero.
- **Files before rows.** Delete the files before the rows that locate them, or keep them findable
  through a per-person directory, so an interrupted run can be repeated and still finds everything.
- **Running work.** The erasure first stops new work for the person from starting, queued and retrying
  work included, and keeps that block through the verification. Work that ends by itself within 60
  seconds may be waited for; otherwise the app answers `409` with `Retry-After` and deletes nothing. The
  `409` body is `{"people_erasure": 1, "app": …, "subject": …, "busy": [{"kind": …, "count": …}]}`. A dry
  run never waits and never changes anything: it lists running work as a kind.
- **Sessions.** The app ends the person's sessions itself; it does not rely on Keycloak's logout.
- **Copies the app cannot trace** (for example lines already delivered to other participants' in-memory
  buffers) are not counted; the app's own documentation names them.
- **Audit rows and logs.** Audit rows in the app's database about the person are exported (withheld with
  reason `others` where they name another person) and erased. Server logs are neither exported nor
  erased by these routes; they expire by edge's log retention (#167).
- **An app without durable data** still answers every route: the export lists what it holds in memory,
  the erasure removes it and answers zeros when nothing is held. A restart is not an erasure.
- Every answer carries `Cache-Control: no-store`.

## hal's order

hal must, in this order:

1. Write the target `sub` into the tombstone register and snapshot it, before any destructive step.
2. Lock the person in Keycloak, with the operator's own admin login and TOTP.
3. Run the dry run in every app, then the erasure in every app. "Every app" is every app that offers
   these routes (architecture §5), whatever the person's groups: group membership is no inventory of
   data.
4. Run a second dry run in every app. Only when all answers are zero, untraceable copies excluded, delete
   the Keycloak user, again with his admin login.
5. Retry a failed step by running the whole command again; idempotency makes that safe.

Recovery after a crash works from the register, without the Keycloak user.

## The tombstone register

Backups and pre-apply snapshots outlive a deletion; their retention is in `docs/backup.md`. A restore,
including the one in the re-setup, can therefore return a person who was erased after that snapshot.
The register is hal's record of deletions, and hal must:

- write the register and snapshot it before destructive work, never only after it;
- restore the register's newest snapshot on every restore path, never the one that matches the app's
  restored snapshot;
- replay the register's deletions before the service opens, through the app's routes on loopback.

## Versioning

Both shapes (`people_export`, `people_erasure`) start at the integer `1`. A new key may be added without a
version change, and readers ignore keys they do not know. Renaming or removing a key, or changing its
meaning, bumps the integer.
