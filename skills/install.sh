#!/usr/bin/env bash
# classin-toolkit 스킬을 ~/.claude/skills/ 로 심볼릭 링크.
# 레포 변경이 즉시 반영된다. 제거는 unlink 또는 ./uninstall.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"

mkdir -p "$TARGET_DIR"

shopt -s nullglob
linked=0
skipped=0
for src in "$SCRIPT_DIR"/classin-*/; do
  name="$(basename "$src")"
  dest="$TARGET_DIR/$name"
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
echo "linked=$linked  skipped=$skipped"
echo "확인: ls $TARGET_DIR | grep classin-"
