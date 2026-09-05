#!/usr/bin/env bash
# AQG — PreToolUse(Edit|Write|MultiEdit) memory-write guard.
#
# Triggers when Claude is about to Write / Edit / MultiEdit a file under
# ~/.claude/projects/<projhash>/memory/. Inspects the proposed new content for
# "code-shaped" signals (language-tagged fenced code blocks, function/class
# declarations in common languages). Engineering knowledge / bug-fix safeguards
# / cross-machine discipline belong in the repo (code, tests, docs, CI, hooks)
# — NOT in machine-local memory.
#
# Tiers:
#   - strong code signal (language-tagged fenced code block from a code-language
#     allowlist, OR a function/class/method declaration in py/sh/go/java/kotlin/
#     js/ts/rust): BLOCK
#   - everything else (prose preference / how-to-work / project-background /
#     illustrative text-/markdown-/diff-/mermaid-fenced quote): ALLOW
#
# Path hardening: target and memory roots are made absolute and canonicalized
# before matching, so traversal, case variants, and symlinked parents cannot
# turn an in-memory write into an unchecked write elsewhere.
#
# Boundaries (all silent exit 0):
#   - AQG_ROOT unset                          → silent (consistent with all AQG hooks)
#   - python3 unavailable                     → silent (degraded)
#   - empty / malformed stdin JSON            → silent
#   - target file_path absent                 → silent
#   - target NOT under the configured client memory root → silent
#   - AQG_AGENT=human-opt-in                  → silent allow (HUMAN pre-launch
#                                               override only — see footer of
#                                               BLOCK message; model cannot
#                                               self-apply this from a Bash
#                                               tool call)
#
# Exit: 0 allow / 2 block. (PreToolUse denies a tool call with exit 2 — stderr is
# fed back to the model. Exit 1 is a NON-blocking error: the tool proceeds. #329
# empirically confirmed this on Claude Code 2.1.185, so this gate was dormant while
# it used exit 1; it now denies with exit 2 like the sibling secret-scan gate.)

set -uo pipefail

input="$(cat 2>/dev/null || true)"
[ -z "$input" ] && exit 0

if [ -z "${AQG_ROOT:-}" ]; then exit 0; fi
if ! command -v python3 >/dev/null 2>&1; then exit 0; fi

# All parsing / path-matching / classification / verdict in python. Shell variables
# cannot hold NUL bytes, so we never split JSON values out into bash — python owns
# the whole decision and reports via stderr + exit code, bash just propagates.
printf '%s' "$input" | python3 -c '
import json, os, re, sys

def case_key(path):
    # Unconditional, fail-toward-block. f64e704 (WS-8 #441, audit bd5d8c44) made this
    # platform-independent on purpose: `sys.platform == "darwin"` is a proxy for "the
    # volume is case-insensitive", not the property itself. Linux mounts ext4 with the
    # casefold feature, exFAT/NTFS and plenty of network shares case-insensitively, and
    # on any of them a `MEMORY/` spelling reaches the real `memory/` dir while an exact
    # comparison waves it through. Folding on a genuinely case-sensitive volume only
    # over-blocks a write to a differently-cased sibling, which is the safe direction.
    # f8d7e97 narrowed this to darwin without saying so and the change rode in on the
    # public-repo merge 073f78b; test_block_case_variant_memory_dir is the canary that
    # caught it. posixpath.normcase is a no-op, so the casefold is what does the work.
    return os.path.normcase(path).casefold()

def is_within(path, root):
    try:
        return os.path.commonpath([case_key(path), case_key(root)]) == case_key(root)
    except (ValueError, OSError):
        return False

try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
if not isinstance(d, dict):
    sys.exit(0)
ti = d.get("tool_input") or {}
if not isinstance(ti, dict):
    sys.exit(0)
fp = ti.get("file_path") or ""
if not isinstance(fp, str) or not fp:
    sys.exit(0)

# NOT closed here (accepted residuals): this guard is defense-in-depth self-
# discipline, NOT an adversarial boundary — a shell-capable agent can bypass any
# Edit/Write hook via Bash (`tee`/`cp` into memory/) or opt out with
# AQG_AGENT=human-opt-in. So hardlinks INTO memory/ (share an inode, invisible to
# any path check) and TOCTOU (a symlink swapped between this check and the Write
# tool open) are out of scope — both would require changing the Write tool itself.
#
# Normalize both sides so path-traversal cannot bypass / falsely fire
# (audit 8b0a3706 f3): `proj/memory/../notes.md` correctly resolves out of memory;
# `proj/notes/../memory/file.md` correctly resolves INTO memory.
fp_lexical = os.path.abspath(os.path.normpath(fp))
fp_norm = os.path.realpath(fp_lexical)
memory_root = os.environ.get("AQG_MEMORY_ROOT") or ""
if memory_root:
    # Codex stores local generated memories under CODEX_HOME/memories. Resolve
    # both paths before comparing so relative paths and symlinked parents cannot
    # bypass the boundary.
    prefix_lexical = os.path.abspath(os.path.normpath(memory_root))
    prefix = os.path.realpath(prefix_lexical)
    # Compute independently: a realpath on another Windows drive must not erase
    # the evidence that the user-named path was lexically inside memory.
    lexical_inside = is_within(fp_lexical, prefix_lexical)
    inside = is_within(fp_norm, prefix)
    if lexical_inside and not inside:
        sys.stderr.write(
            f"[aqg memory-write-guard] BLOCK: memory path escapes through a symlink: {fp}\n"
        )
        sys.exit(2)
    if not inside or case_key(fp_norm) == case_key(prefix):
        sys.exit(0)
else:
    home = os.environ.get("HOME") or ""
    if not home:
        sys.exit(0)
    prefix_lexical = os.path.abspath(os.path.normpath(home + "/.claude/projects"))
    prefix = os.path.realpath(prefix_lexical)
    lexical_inside = is_within(fp_lexical, prefix_lexical)
    inside = is_within(fp_norm, prefix)
    if lexical_inside and not inside:
        sys.stderr.write(
            f"[aqg memory-write-guard] BLOCK: memory path escapes through a symlink: {fp}\n"
        )
        sys.exit(2)
    if not inside or case_key(fp_norm) == case_key(prefix):
        sys.exit(0)
    rest = os.path.relpath(fp_norm, prefix).split(os.sep, 2)
    # rest must be [<projhash>, "memory", "<file...>"]
    if len(rest) < 3 or case_key(rest[1]) != case_key("memory"):
        sys.exit(0)

# Owner override — HUMAN pre-launch only (model cannot self-apply; see BLOCK msg).
if os.environ.get("AQG_AGENT") == "human-opt-in":
    sys.stderr.write(
        f"[aqg memory-write-guard] AQG_AGENT=human-opt-in; allowing memory write to {fp}\n"
    )
    sys.exit(0)

# Aggregate the NEW content the model is writing.
chunks = []
c = ti.get("content")
if isinstance(c, str):
    chunks.append(c)
ns = ti.get("new_string")
if isinstance(ns, str):
    chunks.append(ns)
edits = ti.get("edits") or []
if isinstance(edits, list):
    for e in edits:
        if isinstance(e, dict):
            v = e.get("new_string")
            if isinstance(v, str):
                chunks.append(v)
body = "\n".join(chunks)

if ti.get("aqg_content_unavailable") is True:
    sys.stderr.write(
        f"[aqg memory-write-guard] BLOCK: cannot inspect content moved into memory: {fp}\n"
    )
    sys.exit(2)

# Allowlist of language tags that mean "real code" (audit 8b0a3706 f2: text /
# markdown / diff / mermaid etc. are quote/illustration tags — legitimate in
# memory, do NOT block on them).
CODE_LANGS = {
    "python", "py", "bash", "sh", "shell", "zsh", "fish",
    "javascript", "js", "jsx", "typescript", "ts", "tsx",
    "go", "golang", "rust", "rs", "java", "kotlin", "kt",
    "c", "cpp", "cxx", "cc", "h", "hpp",
    "ruby", "rb", "php", "perl", "pl", "swift", "scala",
    "sql", "yaml", "yml", "json", "toml", "ini",
    "html", "css", "scss",
    "dockerfile", "docker", "make", "makefile", "cmake",
}

signals = []
# (1) Language-tagged fenced code block from the code-language allowlist.
for m in re.finditer(r"(?m)^```([A-Za-z0-9_+\-]+)\s*$", body):
    if m.group(1).lower() in CODE_LANGS:
        signals.append("fenced-code-block")
        break

# (2) Function / class / method declaration in common languages (audit 8b0a3706
# f1: expand beyond Python def/class to cover shell / Go / Java / Kotlin / JS).
decl_patterns = (
    r"(?m)^\s*(def |class |function |async def |fn )[A-Za-z_]",            # Python, JS function, Rust fn
    r"(?m)^\s*[A-Za-z_][A-Za-z_0-9]*\s*\(\s*\)\s*\{",                       # shell function: name() {
    r"(?m)^\s*func\s+[A-Za-z_]",                                            # Go: func Name(
    r"(?m)^\s*(public\s+|private\s+|protected\s+)?(static\s+|abstract\s+|final\s+)?(class|interface|enum)\s+[A-Za-z_]",  # Java/Kotlin/C#
    r"(?m)^\s*(public\s+|private\s+|internal\s+)?fun\s+[A-Za-z_]",          # Kotlin
    r"(?m)^\s*(export\s+)?(default\s+)?function\s+[A-Za-z_]",               # JS/TS function decl
    r"(?m)^\s*(export\s+)?const\s+[A-Za-z_][A-Za-z_0-9]*\s*=\s*(async\s+)?\([^)]*\)\s*=>",  # JS/TS arrow assignment
)
for p in decl_patterns:
    if re.search(p, body):
        signals.append("function-or-class-decl")
        break

if not signals:
    sys.exit(0)

joined = ",".join(signals)
w = sys.stderr.write
w(f"[aqg memory-write-guard] BLOCK: writing to {fp}\n")
w(f"[aqg memory-write-guard] content has code-shape signals: {joined}\n")
w("[aqg memory-write-guard] machine-local memory is NOT the home for engineering knowledge:\n")
w("[aqg memory-write-guard]   - bug fix / regression guard       -> repo code + tests\n")
w("[aqg memory-write-guard]   - architecture / why explanation   -> repo docs / ADR / source comment\n")
w("[aqg memory-write-guard]   - cross-machine discipline / gate  -> repo script / CI / hook\n")
w("[aqg memory-write-guard] memory is for: user preferences / how-to-work corrections / non-derivable project background.\n")
w("[aqg memory-write-guard] (Model: do NOT try to self-bypass via Bash `export AQG_AGENT=...` — env vars\n")
w("[aqg memory-write-guard]  set inside a Bash tool call do NOT persist across tool invocations. Either\n")
client_name = "Codex" if memory_root else "Claude Code"
w(f"[aqg memory-write-guard]  redirect this content to the repo, or ask the human to restart {client_name}\n")
w("[aqg memory-write-guard]  with AQG_AGENT=human-opt-in pre-set in the shell.)\n")
# Deny: PreToolUse uses exit 2 (exit 1 is non-blocking; the tool would proceed). See #329.
sys.exit(2)
'
rc=$?
exit "$rc"
