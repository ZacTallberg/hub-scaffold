# Machine push identity — every computer can ship, and the check never cries wolf

When pushing to a protected branch IS the deploy, a machine that cannot push is a machine whose
agents finish work they cannot ship. On the origin system two finished commits sat in worktrees
for a day because the only registered key lived on the person's OTHER, offline laptop and their
only token had expired. This page is the pattern for an enrollment check that gives each machine
its own push credential and reports honestly whether it can push. Implement it in whatever
enrolls your machines; emit its verdict in the gate's report format so `patterns/presence-gate.py`
(`HUB_GATE_ENV_CHECK`) forwards the rows that need a person to the board:

```text
[push identity] OK    the code host authenticates this machine's key as @alice
[push identity] NEEDS A PERSON  register this machine's key on the code host:
    ecdsa-sha2-nistp256 AAAA... alice@alice-laptop
[push identity] UNVERIFIED  the push probe timed out; this machine's own checkouts decide
```

(An indented line continues its row; the gate folds it in, so the key a person must paste reaches
the board instead of "register this:" with nothing after it.)

## The identity is per MACHINE, not per person

A second computer is then not a second wait, and a lost laptop is one key to revoke. Name the key
for the machine (`id_hub_<machine>_<type>`) and wire it for the code host only.

## Ask the real operation, over every transport the machine uses

The question is "can this machine SHIP", not "does ssh authenticate". A working HTTPS credential in
git's own credential helper read as NEEDS A PERSON for thirty hours on a machine that was pushing
the whole time — and an alarm that fires on a working machine teaches everyone that the queue is
decoration.

- Probe with the real command: `git push --dry-run <remote> <ref>:refs/heads/<branch>` over HTTPS
  through git's credential plumbing (no secret is read into the checking process), and over SSH.
  An unauthenticated HTTPS push never reaches a refspec verdict, so "Everything up-to-date" is a
  proof of authentication.
- **Verify the target exists first** (`git ls-remote`). A host that supports push-to-create
  advertises receive-pack to any authenticated user for a path that does not exist, so a probe
  against a wrong path answers "yes".
- Capture the probe's output and decode it as UTF-8 with replacement: a login banner with curly
  quotes crashed a probe decoding with the console code page, losing a "yes".

## Only a positive refusal needs a person

Three verdicts, never two. **OK** on a proven push. **NEEDS A PERSON** only on a positive refusal
(permission denied, 403) from every transport. **UNVERIFIED** for a timeout, a blocked credential
prompt, or anything unreadable — shown on the machine's own report, never paged. A timeout turned
into a page reopened one problem nine times over three days while the machine pushed daily.

## Mint what the host accepts

A key the API stores is not a key the SSH daemon accepts. A host in FIPS mode refuses ed25519 before
looking it up (every ed25519 key on that instance had never been used); mint ECDSA P-256 there.
Read the host's accepted key types (or register two throwaway keys and see which authenticates)
before choosing. Leave an older key in place and report it as unused rather than deleting it.

## A pinned SSH identity is not pinned until you read which key was offered

- `ssh -i <missing or unreadable path>` drops the pin with a warning and loads the default identity
  files anyway; `IdentitiesOnly=yes` does not prevent it, because the defaults count as configured.
  A probe pinned at a key that was never created then authenticates as the PERSON's key and reports
  a capability the machine does not have. Refuse to probe a key that does not exist.
- `-i` ADDS to identities from `~/.ssh/config`; exclusive pinning needs `-F` with a config you
  control as well.
- The proof is the `Offering public key:` lines under `ssh -v`, not the exit code.
- On Windows, `~` for the default identity files resolves from the user profile, not the shell's
  `$HOME`, and a Unix-style temp path may not resolve for the native ssh at all.

## The check must never break enrollment

It is the newest code on the enrollment path and the only part that shells out, so it has the most
ways to be wrong. Wrap it: an exception degrades to one honest row (NEEDS A PERSON naming the
exception and saying nobody has established whether the machine can push) and every other check is
still reported. The failure mode of a check is "report", never "take the system down".
