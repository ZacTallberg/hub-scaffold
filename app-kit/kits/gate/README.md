# gate — deny by default, then a roster

A request-path identity gate for a Django app. Every path is refused unless the caller is signed
in and on this app's roster, or the path is named public **one at a time**.

## The order of authority

1. **Owners** (`APP_GATE_SUPERADMINS`, a list) are admitted and are superadmin by NAME, whatever the
   roster says. A roster sync must never be able to lock out the people the app belongs to.
2. **The roster** (`AccessUser`) is the authority the moment it has any row. The migrations seed
   the owners, and only the owners — a migration cannot know who else should open an app.
3. **The allowlist** (`APP_GATE_ALLOWLIST`) is a bootstrap fallback consulted only while the roster
   is empty. A wildcard is refused: an emptied roster must never fail open.

People are added deliberately: `manage.py roster --add alice --role member`. `--sync` and
`--deactivate` refuse to leave the app without an owner, `--sync` refuses an empty allowlist, and
nothing is ever deleted. Every change lands in the append-only `GateEvent` log.

## A refused caller gets what it asked for

A machine caller (JSON `Accept`, `X-Requested-With`, an `/api/` path) gets `401` JSON naming the
login URL; an htmx request gets `401` with `HX-Redirect`; a browser gets a redirect carrying `next`.
Handing a `fetch()` poller an HTML login page makes it report `Unexpected token '<'` instead of the
truth, which is that the session ended.

## Guards at boot

| check | refuses |
|---|---|
| `app_gate.E001` | an owner list that is a string (iterating it iterates characters) |
| `app_gate.E003` | a public prefix of `/` or `""` (one character would publish the app) |
| `app_gate.E006` | gate tables on the shared default names — the tables are named from `APP_GATE_TABLE_PREFIX` so two apps on one database never share a roster |
| `app_gate.E007` | every gate migration already recorded (by another app on a shared database) while this app's table is absent — `migrate` would do nothing and the first sign-in would fail. `kits.py add` prevents it: this kit's migrations are renamed from the app's slug (`0001_<slug>_initial`) when it is vendored |

## Rungs

`visitor < member < contributor < admin < superadmin`, one definition in `AccessUser.ROLE_ORDER`.
`@require_role("admin")` on a view; with the gate deliberately OFF (local development) surfaces
above `member` refuse unless `APP_GATE_UNGATED_DEV` is set, so an unarmed gate in production never
exposes an admin surface. Stack traces and roster edits are `admin`.

## How a password is checked is a seam

`APP_GATE_AUTHENTICATOR = "module:callable"` taking `(username, password)` and returning an
`Actor`, raising `CredentialRejected` (the directory said no) or `DirectoryUnavailable` (it could
not be asked — never shown as "wrong password"). The kit ships `app_gate.backends:django_password`;
a directory bind is an adapter you write against the same signature, with a timeout on every call.
