#!/usr/bin/env bash
# AQG — SessionStart: trigger the managed update check, detached.
#
# A TRIGGER, not the update. Everything expensive happens in the background
# process this starts, because a hook that waited for a network round trip would
# put one in front of every session.
#
# Three rules, all of them learned the hard way:
#   - NOTHING on stdout. A SessionStart hook's stdout is a JSON channel; plain
#     text there is read as a malformed event and aborts a headless session.
#   - ALWAYS exit 0. SessionStart must not block.
#   - NO `setsid`. It is util-linux and absent on macOS, so the command failed,
#     `|| true` swallowed it, and the check never ran — on the platform this
#     repository is developed on, with every test green. `nohup` is POSIX.
#
# The environment is REDUCED, not isolated, and the difference matters. What is
# removed is the interpreter-steering set — PYTHONPATH, PYTHONHOME, LD_PRELOAD —
# which would otherwise let anything that can set a variable hijack the process
# that checks signatures, around a tamper guard that watches the checkout and
# nothing that watches the environment. `-E` and `-s` close the same door from
# the other side.
#
# What is deliberately KEPT is PATH, which selects the python3 that does the
# verifying and the git and openssl it calls, and HOME, where the state
# directory lives and where git finds ~/.gitconfig. Neither can be dropped and
# have this work at all, so this is a smaller perimeter than "isolated" — an
# audit was right to say the `env -i` reads as more than it delivers.
#
# AQG_UPDATE_INTERVAL_SECONDS is kept for a different reason: this launcher is
# the only path on which the interval is ever consulted, so a variable the
# runner reads but this list drops is an override that works in tests and
# nowhere else.
#
# What makes forwarding it acceptable is not a claim about who can set it — an
# audit was right to reject that as the justification. It is that the value
# cannot reach anything but one integer: the runner matches it against
# `[+-]?[0-9]+` before parsing, discards anything else, and clamps what is left
# between MIN_ and MAX_CHECK_INTERVAL_SECONDS, so it can neither steer the
# interpreter nor turn the check off forever. It never reaches a shell either:
# the expansion below is the *result* of an expansion, not shell syntax, so a
# value full of quotes and `$( )` arrives as one literal argv element — which
# the launcher's own tests exercise with a payload rather than assert in prose.
set -uo pipefail

if [ -n "${AQG_NO_UPDATE_CHECK:-}" ]; then exit 0; fi
if [ -z "${AQG_ROOT:-}" ]; then exit 0; fi
if ! command -v python3 >/dev/null 2>&1; then exit 0; fi
if [ ! -f "$AQG_ROOT/scripts/aqg_update/run.py" ]; then exit 0; fi

# `cd` into AQG_ROOT, and it is load-bearing. `-m` resolves against the CURRENT
# WORKING DIRECTORY, and `-E` makes PYTHONPATH inert — so without this the
# trigger ran whatever `scripts/aqg_update` happened to sit under the user's
# project, or nothing at all. The first end-to-end rehearsal caught it reading a
# development checkout's keyring while checking an install somewhere else.
nohup env -i \
  HOME="${HOME:-}" PATH="${PATH:-}" LANG="${LANG:-}" \
  USERPROFILE="${USERPROFILE:-}" \
  AQG_ROOT="$AQG_ROOT" AQG_STATE_ROOT="${AQG_STATE_ROOT:-}" \
  AQG_UPDATE_INTERVAL_SECONDS="${AQG_UPDATE_INTERVAL_SECONDS:-}" \
  sh -c 'cd "$AQG_ROOT" && exec python3 -E -s -m scripts.aqg_update.run' \
  </dev/null >/dev/null 2>&1 &
exit 0
