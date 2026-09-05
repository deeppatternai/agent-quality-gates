#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/install_from_ref.sh --repo OWNER/REPO --ref REF --dest PATH [options]

Clone or update AQG from a specific ref, then install Codex skills by default.

Options:
  --repo OWNER/REPO    GitHub repo slug or git URL. Default: deeppatternai/agent-quality-gates.
  --ref REF            Branch, tag, or commit SHA to check out.
  --dest PATH          Local checkout directory for the AQG repo.
  --copy               Copy Codex skills instead of linking them.
  --codex-dest PATH    Codex skills destination. Default: ${CODEX_HOME:-$HOME/.codex}/skills.
  --skip-codex-install Clone/checkout only; do not run scripts/install.sh.
  --force              Replace non-git dest or force re-checkout after cleaning tracked files.
  -h, --help           Show this help.
EOF
}

repo="deeppatternai/agent-quality-gates"
ref=""
dest=""
install_mode="link"
codex_dest="${CODEX_HOME:-$HOME/.codex}/skills"
skip_codex_install="0"
force="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --repo requires a value" >&2
        exit 2
      fi
      repo="$2"
      shift 2
      ;;
    --ref)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --ref requires a value" >&2
        exit 2
      fi
      ref="$2"
      shift 2
      ;;
    --dest)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --dest requires a path" >&2
        exit 2
      fi
      dest="$2"
      shift 2
      ;;
    --copy)
      install_mode="copy"
      shift
      ;;
    --codex-dest)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --codex-dest requires a path" >&2
        exit 2
      fi
      codex_dest="$2"
      shift 2
      ;;
    --skip-codex-install)
      skip_codex_install="1"
      shift
      ;;
    --force)
      force="1"
      shift
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

if [[ -z "$ref" ]]; then
  echo "ERROR: --ref is required" >&2
  exit 2
fi
if [[ -z "$dest" ]]; then
  echo "ERROR: --dest is required" >&2
  exit 2
fi

parent="$(dirname "$dest")"
mkdir -p "$parent"

if [[ -e "$dest" && ! -d "$dest/.git" ]]; then
  if [[ "$force" != "1" ]]; then
    echo "ERROR: destination exists but is not a git checkout: $dest" >&2
    exit 1
  fi
  rm -rf "$dest"
fi

if [[ ! -d "$dest/.git" ]]; then
  if [[ "$repo" == *"://"* || "$repo" == git@* ]]; then
    git clone "$repo" "$dest"
  else
    gh repo clone "$repo" "$dest"
  fi
fi

if [[ -n "$(git -C "$dest" status --porcelain)" ]]; then
  if [[ "$force" != "1" ]]; then
    echo "ERROR: destination checkout is dirty: $dest" >&2
    exit 1
  fi
  git -C "$dest" reset --hard HEAD
  git -C "$dest" clean -fd
fi

git -C "$dest" fetch --tags --prune origin
if git -C "$dest" rev-parse --verify --quiet "refs/remotes/origin/$ref^{commit}" >/dev/null; then
  git -C "$dest" checkout --detach "origin/$ref"
else
  git -C "$dest" fetch origin "$ref" || true
  if ! git -C "$dest" cat-file -e "${ref}^{commit}" 2>/dev/null; then
    echo "ERROR: ref is not available as a commit after fetch: $ref" >&2
    exit 1
  fi
  git -C "$dest" checkout --detach "$ref"
fi

checked_out="$(git -C "$dest" rev-parse HEAD)"
echo "AQG checkout ready: $dest"
echo "ref: $ref"
echo "head: $checked_out"

if [[ "$skip_codex_install" != "1" ]]; then
  install_args=(--force --dest "$codex_dest")
  if [[ "$install_mode" == "copy" ]]; then
    install_args=(--force --copy --dest "$codex_dest")
  fi
  "$dest/scripts/install.sh" "${install_args[@]}"
fi
