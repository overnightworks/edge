# The people contract: exporting and erasing one person

Reference for an app's head or agent. Question answered: what must my app's admin API offer so that
one person can be exported and erased. hal (`hal export person`, `hal delete person`) is the only
caller; it drives every app through the routes below (operator adoption of "erasure and export are the
hal contract", 20.10.2026).

## Key and authorisation

- The person's key is the token's `sub` (the Keycloak user id). hal resolves a name to the `sub` with an
  exact match before calling any app.
- Every route requires the caller to be in the group `/apps/<app>/admin`. The caller is the operator
  at a terminal, holding his own `hal login` token. There is no hal service account and no standing
  secret for these routes.
- A bearer request never creates an account in the app.

## Export

`GET /api/admin/people/{subject}/export` answers a ZIP with `export.json` at its root and the files under
`files/`. `export.json` has these shared keys; everything else is the app's own.

| Key | Content |
|---|---|
| `people_export` | the shape version, an integer |
| `app` | the app's name |
| `subject` | the `sub` |
| `exported_at` | timestamp |
| `data` | the person's data, its structure owned by the app |
| `files` | list of `{path, kind, bytes}`, one per file under `files/` |
| `withheld` | list of `{kind, count, reason}`; `reason` is `secret` or `others` |

What the export leaves out (a secret, or data that belongs to other people) is listed in `withheld`, never
dropped silently.

## Erase

`DELETE /api/admin/people/{subject}` erases the person. With `?dry_run=true` it changes nothing and
answers what the erasure would do. Both answer one body:

```json
{"people_erasure": 1, "app": "songmaker", "subject": "3f6c2a7e-…", "dry_run": true,
 "kinds": [{"kind": "song", "count": 12}, {"kind": "generation_audio", "count": 40, "bytes": 412000000}],
 "affects_others": [{"kind": "playlist_entry", "count": 3, "action": "delete"}]}
```

`kinds` counts what belongs to the person (`bytes` where files are involved); `affects_others` counts
what the erasure changes for other people, with the `action` taken. In a real erasure `dry_run` is `false`
and the counts are what was removed.

Rules:

- **Idempotent.** An unknown subject answers `200` with zero counts, never `404`, and the call never
  creates the subject. Running it twice leaves the second run at zero.
- **Files before rows.** Delete the files before the rows that locate them, or keep them findable
  through a per-person directory, so an interrupted run can be repeated and still finds everything.
- **Running work.** The erasure first stops the person's running work (jobs) or waits for it; if it
  can do neither it answers `409` and deletes nothing.
- **Sessions.** The app ends the person's sessions itself; it does not rely on Keycloak's logout.

## hal's order

1. Lock the user in Keycloak.
2. For every app of the user's groups: the dry run, then the erasure.
3. A dry run that answers zero in every app is required before hal deletes the Keycloak user.
4. A failed step is retried by running the whole command again; idempotency makes that safe.

## The tombstone register

hal keeps a register of the deletions it has made. Backups would otherwise bring a deleted person
back: the weekly retention keeps four weekly and three monthly snapshots per app (about three months,
`docs/backup.md` timer table, `KEEP_WEEKLY` and `KEEP_MONTHLY` in `src/edge_backup/restic.py`), and the
last 10 `pre-apply` snapshots of an app are kept besides, which the weekly retention never deletes
(`docs/backup.md`, "One app, and the pre-apply snapshot"). A restore, including the one in the
re-setup, can therefore return a person who was erased after that snapshot.

The rule:

- Snapshot the register after every deletion.
- Every restore path restores the register's newest snapshot, never the one that matches the app's
  restored snapshot.
- Every restore path replays the register's deletions before the service opens.

## Versioning

Each shape (`people_export`, `people_erasure`) carries an integer. It changes only when a shared key is
renamed, removed or changes its meaning; adding a key does not change it.
