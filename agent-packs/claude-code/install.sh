#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: agent-packs/claude-code/install.sh [options]

Install AQG Claude Code skills.

Options:
  --scope user|project      Install to ~/.claude/skills or <project-root>/.claude/skills. Default: user.
  --project-root PATH       Project root for --scope project.
  --dest PATH               Install into PATH instead of scope default.
  --mode link|copy          Install symlinks or copied skill directories. Default: link.
  --force                   Replace existing target skill directories or symlinks.
  --hooks                   Install/refresh Claude Code lifecycle hooks even with a custom --dest.
  --no-hooks                Skip Claude Code lifecycle hook install/refresh.
  --list                    List Claude Code skills packaged in this agent pack.
  -h, --help                Show this help.
EOF
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
pack_root="$repo_root/agent-packs/claude-code"
skills_root="$pack_root/skills"
version="$(tr -d '[:space:]' < "$repo_root/VERSION" 2>/dev/null || printf 'unknown')"

# Verify the AQG context helper is present so migrated wrapper SKILL.md How
# To Run blocks can source it (PR-3b1 onwards).
if [[ ! -f "$repo_root/scripts/_aqg_context.sh" ]]; then
  echo "ERROR: missing $repo_root/scripts/_aqg_context.sh — refusing to install wrappers that depend on it." >&2
  exit 1
fi
scope="user"
project_root=""
dest=""
mode="link"
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
# THIS pack's skills/ dir (live OR dangling) but whose name is NOT a currently
# packaged skill — residue from renamed / removed skills. Safety, in order:
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
    if [[ ! -d "$skills_root/$skill" ]]; then
      echo "ERROR: missing packaged skill: $skills_root/$skill" >&2
      exit 1
    fi
    echo "$skill"
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --scope requires user or project" >&2
        exit 2
      fi
      scope="$2"
      shift 2
      ;;
    --project-root)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --project-root requires a path" >&2
        exit 2
      fi
      project_root="$2"
      shift 2
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
    --mode)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --mode requires link or copy" >&2
        exit 2
      fi
      mode="$2"
      shift 2
      ;;
    --force)
      force="1"
      shift
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

if [[ "$scope" != "user" && "$scope" != "project" ]]; then
  echo "ERROR: --scope must be user or project" >&2
  exit 2
fi
if [[ "$mode" != "link" && "$mode" != "copy" ]]; then
  echo "ERROR: --mode must be link or copy" >&2
  exit 2
fi
if [[ -z "$dest" ]]; then
  if [[ "$scope" == "user" ]]; then
    dest="$HOME/.claude/skills"
  else
    if [[ -z "$project_root" ]]; then
      echo "ERROR: --scope project requires --project-root unless --dest is provided" >&2
      exit 2
    fi
    dest="$project_root/.claude/skills"
  fi
fi

mkdir -p "$dest"
echo "Installing Agent Quality Gates $version Claude Code skills into $dest"

# Remove residue (renamed/removed skills) before linking.
prune_stale_skill_symlinks "$dest"

for skill in "${skills[@]}"; do
  source_dir="$skills_root/$skill"
  target_dir="$dest/$skill"
  if [[ ! -d "$source_dir" ]]; then
    echo "ERROR: missing packaged skill: $source_dir" >&2
    exit 1
  fi
  if [[ -e "$target_dir" || -L "$target_dir" ]]; then
    if [[ "$force" != "1" ]]; then
      echo "ERROR: target exists: $target_dir (use --force to replace)" >&2
      exit 1
    fi
  fi
  install_args=(
    python3 "$repo_root/scripts/aqg_skill_install.py"
    --source "$source_dir"
    --target "$target_dir"
    --aqg-root "$repo_root"
    --mode "$mode"
  )
  if [[ "$force" == "1" ]]; then
    install_args+=(--force)
  fi
  "${install_args[@]}"
done

echo "Done. Restart Claude Code or open a new session to load updated skills."

# AQG also ships Claude Code HOOKS that make the key gates resident. Install or
# refresh them by default for real scope installs; custom --dest defaults to
# skills-only so tests and nonstandard targets do not silently edit ~/.claude.
# upgrade.sh sets AQG_SKIP_HOOK_PROMPT=1 because it owns a separate hook step.
if [[ "${AQG_SKIP_HOOK_PROMPT:-0}" == "1" || "$hooks_mode" == "skip" ]]; then
  echo "Claude Code lifecycle hooks skipped."
elif [[ "$dest_overridden" == "1" && "$hooks_mode" != "force" ]]; then
  echo "Claude Code lifecycle hooks skipped for custom --dest (pass --hooks to install/refresh the scope settings file)."
else
  hook_target=()
  if [[ "$scope" == "project" ]]; then
    if [[ -z "$project_root" ]]; then
      echo "ERROR: --hooks with --scope project requires --project-root" >&2
      exit 2
    fi
    hook_target=(--target "$project_root/.claude/settings.json")
    AQG_ROOT="$repo_root" python3 "$repo_root/scripts/install_aqg_hooks.py" --apply --aqg-root "$repo_root" "${hook_target[@]}"
  else
    AQG_ROOT="$repo_root" python3 "$repo_root/scripts/install_aqg_hooks.py" --apply --aqg-root "$repo_root"
  fi
  echo "After hook install, restart Claude Code. Ensure AQG_ROOT is exported where Claude Code runs:"
  echo "  export AQG_ROOT=\"$repo_root\""
fi
