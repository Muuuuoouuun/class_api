#!/usr/bin/env bash
# classin-toolkit 스킬을 Claude/Codex skills 디렉터리로 심볼릭 링크.
# 레포 변경이 즉시 반영된다. 제거는 대상 디렉터리의 symlink 를 unlink.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-claude}"

case "$TARGET" in
  claude)
    TARGET_DIRS=("${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}")
    ;;
  codex)
    TARGET_DIRS=("${CODEX_SKILLS_DIR:-${CODEX_HOME:-$HOME/.codex}/skills}")
    ;;
  both)
    TARGET_DIRS=(
      "${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
      "${CODEX_SKILLS_DIR:-${CODEX_HOME:-$HOME/.codex}/skills}"
    )
    ;;
  *)
    echo "usage: $0 [claude|codex|both]" >&2
    exit 2
    ;;
esac

shopt -s nullglob
for target_dir in "${TARGET_DIRS[@]}"; do
  mkdir -p "$target_dir"
  linked=0
  skipped=0
  for src in "$SCRIPT_DIR"/classin-*/; do
    name="$(basename "$src")"
    dest="$target_dir/$name"
    if [ -L "$dest" ] || [ -e "$dest" ]; then
      echo "skip   $dest (이미 존재)"
      skipped=$((skipped+1))
      continue
    fi
    ln -s "$src" "$dest"
    echo "link   $dest -> $src"
    linked=$((linked+1))
  done

  echo
  echo "target=$target_dir  linked=$linked  skipped=$skipped"
  echo "확인: ls $target_dir | grep classin-"
done
