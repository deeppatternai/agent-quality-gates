#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/install.sh [--copy] [--force] [--dest PATH] [--hooks|--no-hooks] [--list]

Install Agent Quality Gates Codex skills into ${CODEX_HOME:-$HOME/.codex}/skills.

Options:
  --copy       Copy skill directories instead of link mode.
  --force      Replace existing target skill directories or symlinks.
  --dest PATH  Install into PATH instead of ${CODEX_HOME:-$HOME/.codex}/skills.
  --hooks      Install/refresh Codex lifecycle hooks even with a custom --dest.
  --no-hooks   Skip Codex lifecycle hook install/refresh.
  --list       List skills packaged in this repository.
  -h, --help   Show this help.
EOF
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
skills_root="$repo_root/skills"
version="$(tr -d '[:space:]' < "$repo_root/VERSION" 2>/dev/null || printf 'unknown')"

# Verify the AQG context helper is present so migrated SKILL.md How To Run
# blocks can source it (PR-3b1 onwards). Skills installed via symlink would
# otherwise dangle quietly the first time they tried to source the helper.
if [[ ! -f "$repo_root/scripts/_aqg_context.sh" ]]; then
  echo "ERROR: missing $repo_root/scripts/_aqg_context.sh — refusing to install skills that depend on it." >&2
  exit 1
fi

# PyYAML is a DECLARED runtime dependency of scripts/aqg_skill_validator.py
# (triggers.yaml / openai.yaml parsing). Installing the skill files themselves
# does not need it, but the validator fail-closes without it — warn (do not
# block) so the user can install it before running the validator.
if ! python3 -c 'import yaml' >/dev/null 2>&1; then
  echo "WARNING: PyYAML not found — run 'pip install -r "$repo_root/requirements.txt"' so aqg_skill_validator.py can parse triggers/openai YAML." >&2
fi

default_dest="${CODEX_HOME:-$HOME/.codex}/skills"
dest="$default_dest"
mode="symlink"
force="0"
dest_overridden="0"
hooks_mode="auto"

skills=(
  "aqg-startup-preflight"
  "aqg-code-construction"
  "aqg-systematic-debugging"
  "aqg-audit-adjudication"
  "aqg-evidence-closeout"
  "aqg-skill-validator"
  "aqg-security-review"
  "aqg-automation-audit"
  "aqg-phase-transition"
  "aqg-multi-review"
  "aqg-test-quality-review"
  "aqg-re-anchor"
  "aqg-project-status"
  "aqg-memory-hygiene"
  "aqg-session-handoff"
  "aqg-decision-capture"
)

# Prune stale AQG skill symlinks in $dest: any symlink whose target points into
# THIS repo's skills/ dir (live OR dangling) but whose name is NOT a currently
# packaged skill. Covers residue from renamed / removed skills, incl the 0.3.0
# ai-team-* -> aqg-* rename. Safety, in order:
#   - only symlinks (-L) — real directories are never touched
#   - only targets under $skills_root — third-party / outside-repo links kept
#   - current skills are kept (the link loop re-points them)
prune_stale_skill_symlinks() {
  local dir="$1" entry name target keep skill
  [[ -d "$dir" ]] || return 0
  for entry in "$dir"/*; do
    [[ -L "$entry" ]] || continue
    name="$(basename "$entry")"
    keep="0"
    for skill in "${skills[@]}"; do
      if [[ "$skill" == "$name" ]]; then keep="1"; break; fi
    done
    [[ "$keep" == "1" ]] && continue
    target="$(readlink "$entry" || true)"
    case "$target" in
      "$skills_root"/*)
        # Only our installer's own link shape: an absolute $skills_root/<one
        # segment>. Refuse '..', deeper paths, or a trailing slash (e.g.
        # $skills_root/../scripts, which text-matches but resolves OUTSIDE) —
        # fail-safe: keep anything that is not exactly how we create a link.
        rest="${target#"$skills_root"/}"
        if [[ -n "$rest" && "$rest" != */* && "$rest" != ".." && "$rest" != "." ]]; then
          rm -f "$entry"
          echo "pruned stale AQG symlink $name (was $target)"
        fi
        ;;
    esac
  done
}

list_skills() {
  for skill in "${skills[@]}"; do
    echo "$skill"
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --copy)
      mode="copy"
      shift
      ;;
    --force)
      force="1"
      shift
      ;;
    --dest)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --dest requires a path" >&2
        exit 2
      fi
      dest="$2"
      dest_overridden="1"
      shift 2
      ;;
    --hooks)
      hooks_mode="force"
      shift
      ;;
    --no-hooks)
      hooks_mode="skip"
      shift
      ;;
    --list)
      list_skills
      exit 0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

mkdir -p "$dest"
echo "Installing Agent Quality Gates $version Codex skills into $dest"

# Remove residue (renamed/removed skills, incl 0.3.0 ai-team-*) before linking.
prune_stale_skill_symlinks "$dest"

helper_mode="link"
if [[ "$mode" == "copy" ]]; then
  helper_mode="copy"
fi

install_args=(
  "$repo_root/scripts/aqg_skill_install.py"
  --aqg-root "$repo_root"
  --mode "$helper_mode"
)
if [[ "$force" == "1" ]]; then
  install_args+=(--force)
fi

for skill in "${skills[@]}"; do
  source_dir="$skills_root/$skill"
  target_dir="$dest/$skill"

  if [[ ! -d "$source_dir" ]]; then
    echo "ERROR: missing packaged skill: $source_dir" >&2
    exit 1
  fi

  install_args+=(--item "$source_dir" "$target_dir")
done
python3 "${install_args[@]}"

echo "Done. Restart Codex or open a new session to load updated skills."
# install_aqg.sh / upgrade.sh set AQG_SKIP_HOOK_PROMPT=1 because they own a
# separate Codex hook step; naming --no-hooks there would blame a flag the user
# never passed, on a run that goes on to install 4 blocking policies.
if [[ "${AQG_SKIP_HOOK_PROMPT:-0}" == "1" ]]; then
  echo "Codex lifecycle hooks deferred to the calling installer."
elif [[ "$hooks_mode" == "skip" ]]; then
  echo "Codex lifecycle hooks skipped by --no-hooks."
elif [[ "$dest_overridden" == "1" && "$hooks_mode" != "force" ]]; then
  echo "Codex lifecycle hooks skipped for custom --dest (pass --hooks to install/refresh ${CODEX_HOME:-$HOME/.codex}/hooks.json)."
else
  python3 "$repo_root/scripts/install_aqg_codex_hooks.py" --apply --aqg-root "$repo_root"
  echo "After hook install, restart Codex and use /hooks to review and trust the definitions."
fi
