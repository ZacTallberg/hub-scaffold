# Directory sign-in — a wrong password is a 401, an outage is a 503, and nothing retries a rejection

The Hub reads the host site's authenticated user (`adapters/django/hub/read_auth.py`); many
adopters back that user with a directory bind (LDAP, an identity provider's password grant).
Three failure classes from that seam recur, and each one turns an ordinary event into either a
server 500 or a locked account.

## 1. Map exceptions by what they MEAN, not by the base class you assumed

Directory client libraries often split their exception tree. In one widely used Python LDAP
client, a rejected credential raises an *operation-result* class that does **not** derive from
the library's "error" base — so a handler written as "catch the library's errors" never sees a
mistyped password, and every typo surfaces as an unhandled 500 on the error board.

Name the two outcomes the sign-in view actually needs and map into them explicitly:

```python
class CredentialRejected(Exception): ...   # -> 401, "user name or password is incorrect"
class DirectoryUnavailable(Exception): ... # -> 503, "sign-in is temporarily unavailable"

try:
    conn.bind()                             # and the user search, if you do one
except InvalidCredentialsResult as exc:     # the library's "rejected" class, by NAME
    raise CredentialRejected() from exc
except DirectoryLibraryBaseException as exc:  # the ROOT of the library's tree, both branches
    raise DirectoryUnavailable() from exc
```

Check the library's actual method-resolution order once (`Cls.__mro__`) before trusting a catch
clause; do not infer it from the class name.

## 2. Teardown must never undo the handling

An `unbind()`/`close()` in `finally` that raises while `DirectoryUnavailable` is propagating
*replaces* the handled exception with an unhandled one — the exact 500 the handlers just closed.
Guard teardown and log instead:

```python
finally:
    try:
        conn.unbind()
    except Exception:                      # noqa: BLE001 - teardown never outranks the verdict
        log.warning("directory unbind failed", exc_info=True)
```

## 3. A rejected credential binds EXACTLY once

Any retry wrapper around the bind (added to ride out a flaky directory) must treat
`CredentialRejected` as terminal. Retrying it turns one typo into several bad binds against a
directory with a lockout policy — the person is locked out by the sign-in page itself. Retry only
`DirectoryUnavailable`, and only a bounded number of times.

This is a lockout boundary, so when you change the bind path, prove it once with a transient probe
(per `docs/TESTING.md`): count bind attempts for one rejected credential against a stub that
raises the rejected class, confirm the count is 1 and the response is 401, record the receipt,
and delete the probe.

## Recording

A 401 for a rejected credential is not an operational error; do not send it to the Hub's error
stream. A `DirectoryUnavailable` is: report it (`python -m hub_core.client app-error --app <site>
--kind directory --severity error ...`) so an outage is visible before the first person asks why
they cannot sign in.
