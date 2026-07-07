#!/usr/bin/env bash
# pre-receive-gate.sh — server-side push gate (out-of-process enforcement).
#
# Reads ref lines on stdin (standard pre-receive protocol: "<old> <new> <ref>") and rejects any
# push that adds credential-shaped files or live-key literals. Deliberately tiny: only
# certain-bad patterns, so failing closed can never cause a false outage. Client-side hooks and
# agent discipline are advisory; this runs where nobody can skip it.
#
# INSTALL
#   Bare repo (self-hosted git):
#     cp pre-receive-gate.sh /path/to/repo.git/hooks/pre-receive && chmod +x .../hooks/pre-receive
#     If the repo (or your git-based PaaS) already HAS a pre-receive hook, chain instead of
#     replacing: rename the existing hook, then make pre-receive a 2-liner that tees stdin to
#     this gate first and to the original second — both must exit 0.
#       exec 3<&0
#       tmp=$(mktemp); cat >"$tmp"
#       /path/to/pre-receive-gate.sh <"$tmp" || exit 1
#       exec /path/to/original-pre-receive <"$tmp"
#   GitLab (self-managed): drop into the repo's custom_hooks/pre-receive.d/ (or the global
#     server hooks dir) — GitLab runs every executable in the .d directory and any non-zero
#     exit rejects the push.
#   GitHub / cloud-hosted: you cannot install server hooks; enforce the same patterns with
#     push protection / secret scanning plus a required CI check that runs this script against
#     the pushed range (weaker: post-receive-time, but still out-of-process).
set -u
EMPTY=4b825dc642cb6eb9a060e54bf8d69288fbee4904   # git's well-known empty-tree object
block=0
while read -r old new ref; do
  case "$new" in *[!0]*) ;; *) continue;; esac                # ref deletion -> nothing to scan
  case "$old" in *[!0]*) base="$old";; *) base="$EMPTY";; esac # first push -> diff vs empty tree

  files=$(git diff --name-only --diff-filter=AM "$base" "$new" 2>/dev/null || true)
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    case "$f" in
      # Sanctioned templates are fine — only live-credential SHAPES are blocked.
      *.env.example|*/.env.example|*.pem.example|*example.pem) continue;;
    esac
    case "$f" in
      .env|*/.env|.env.*|*/.env.*|\
      id_rsa|*/id_rsa|id_ed25519|*/id_ed25519|id_ecdsa|*/id_ecdsa|id_dsa|*/id_dsa|\
      *.pem|\
      credentials.json|*/credentials.json|*credentials*.local*|\
      creds*|*/creds*|*.local.txt|*/*.local.txt|\
      .netrc|*/.netrc|.npmrc|*/.npmrc|.pgpass|*/.pgpass)
        echo "GATE REJECT: credential-shaped file in push: $f" >&2; block=1;;
    esac
    # NOTE on *.pem: if your org legitimately commits public certs as .pem, narrow this to
    # *key*.pem / *private*.pem — the content scan below still catches any real private key.
  done <<EOF2
$files
EOF2

  # Content scan: added lines that match live-credential literals. Extend with your org's own
  # token prefixes — keep each pattern certain-bad (near-zero false positives).
  if git diff "$base" "$new" 2>/dev/null | grep -qE '^\+.*(BEGIN (RSA|OPENSSH|EC|DSA|PGP) PRIVATE KEY|sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{36}|glpat-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})'; then
    echo "GATE REJECT: an added line matches a live-credential pattern (private key block / provider API token)" >&2
    block=1
  fi
done
[ "$block" = 1 ] && echo "GATE: push rejected server-side by pre-receive-gate. Rewrite the commit to remove the secret (history rewrite, not a follow-up delete), then push again." >&2
exit $block
