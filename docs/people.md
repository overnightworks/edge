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
- **Caller.** A separate identity: the operator, with his own access token, from the login hal
  must offer (#256); no such login exists yet. The app checks the token in this order:
  1. the signature verifies with the realm's keys and the token is within its lifetime (`exp`,
     and `nbf` when present);
  2. the issuer is the realm's;
  3. the JWT header `typ` is `at+jwt` or `application/at+jwt` (RFC 9068);
  4. `aud` names exactly the app's own client, and no other app or Keycloak's `account`;
  5. `azp` is a client id the app's configuration allows, in practice hal's client.

  A missing token or any failure among these makes the token invalid and answers `401`. A valid
  token whose `groups` hold neither `/apps/<app>/admin` nor a group below it (subgroups count, as
  in the browser login) answers `403`. The token
  authenticates the caller; it never names the target. There is no hal service account and no
  standing secret for these routes.
- A request to these routes never creates or provisions an account, neither the caller's nor the
  target's. Given a valid token, a caller without an account in the app is authorised by the
  token alone or refused with `403`.
- An app that admits only browser sessions today says so, and hal cannot call it until it admits the
  token.
- Every answer carries `Cache-Control: no-store`.

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

`kinds` counts what belongs to the person. `bytes` is optional: an app gives it where files are
involved and can measure it cheaply, and hal shows it when present. hal's order, step 4, judges
completion by the counts. `affects_others` counts what the erasure changes for other people. In a
real erasure `dry_run` is `false` and the counts are what was removed. `people_erasure` is the
integer `1`.

- `kinds[].kind` and `withheld[].kind` are the app's own lowercase words; hal only shows them.
- `affects_others[].action` is `delete` (others lose an object that is deleted, a shared one included)
  or `end` (an activity others take part in ends, such as a hosted call).

Rules:

- **Idempotent.** An unknown subject answers `200` with zero counts, never `404`, and the call never
  creates the subject. Running it twice leaves the second run at zero.
- **Files before rows.** Delete the files before the rows that locate them, or keep them findable
  through a per-person directory, so an interrupted run can be repeated and still finds everything.
- **Running work.** The erasure first stops new work for the person from starting and cancels queued and
  retrying work for the person (cancelled, not paused). The block holds until the erasure has answered;
  a `409` lifts it. After that, hal's Keycloak lock (its step 2) keeps the person from starting new work until hal deletes the Keycloak user, so the app's block only has to bridge the erasure itself. Work that ends by itself within 60
  seconds may be waited for; otherwise the app answers `409` with `Retry-After` and deletes nothing. The
  `409` body is `{"people_erasure": 1, "app": …, "subject": …, "busy": [{"kind": …, "count": …}]}`. A dry
  run never waits and never changes anything: it lists running work as a kind.
- **Sessions.** The app ends the person's sessions itself; it does not rely on Keycloak's logout.
- **Copies the app cannot trace** (for example lines already delivered to other participants' in-memory
  buffers) are not counted; the app's own documentation names them.
- **Audit rows and logs.** Audit rows in the app's database about the person are exported (withheld with
  reason `others` where they name another person) and erased. Server logs are neither exported nor
  erased by these routes; deleting them by age is #167, not built yet.
- **An app without durable data** still answers every route: the export lists what it holds in memory,
  the erasure removes it and answers zeros when nothing is held. A restart is not an erasure.

## hal's order

hal must, in this order:

1. Write the target `sub` into the tombstone register and snapshot it, before any destructive step.
2. Lock the person in Keycloak, with the operator's own admin login and TOTP.
3. Run the dry run in every app, then the erasure in every app. "Every app" is every app that offers
   these routes (architecture §5), whatever the person's groups: group membership is no inventory of
   data.
4. Run a second dry run in every app. Only when every count is zero (`bytes` is not considered),
   untraceable copies excluded, delete the Keycloak user, again with his admin login.
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
