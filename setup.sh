#!/usr/bin/env bash
# SwarmForge — setup helper for macOS / Linux
# Usage:  ./setup.sh          (check only)
#         ./setup.sh --install (interactive install)
set -euo pipefail

tools=(
  "opencode|opencode|npm i -g opencode-ai|curl -fsSL https://opencode.ai/install | bash"
  "agy (Google Antigravity)|agy|npm i -g @google/gemini-cli|curl -fsSL https://antigravity.google/cli/install.sh | bash"
  "gemini|gemini|npm i -g @google/gemini-cli|"
  "grok (xAI)|grok||curl -fsSL https://x.ai/cli/install.sh | bash"
  "copilot (GitHub)|copilot|npm i -g @github/copilot|"
)

echo ""
echo "SwarmForge - tool check"
echo "------------------------------------------"

missing=()
for entry in "${tools[@]}"; do
  IFS='|' read -r label binary npmcmd curlcmd <<<"$entry"
  if command -v "$binary" >/dev/null 2>&1; then
    echo "  [ok]  $label"
  else
    echo "  [x]   $label missing"
    if command -v npm >/dev/null 2>&1 && [ -n "$npmcmd" ]; then
      missing+=("$label|$npmcmd")
    elif [ -n "$curlcmd" ]; then
      missing+=("$label|$curlcmd")
    fi
  fi
done

if [ "${#missing[@]}" -eq 0 ]; then
  echo ""
  echo "All available. Run: python3 forge.py \"<task>\""
  exit 0
fi

if [ "${1:-}" != "--install" ]; then
  echo ""
  echo "Install ke liye: ./setup.sh --install"
  exit 0
fi

for entry in "${missing[@]}"; do
  IFS='|' read -r label cmd <<<"$entry"
  echo ""
  echo "[$label]"
  echo "  install: $cmd"
  read -rp "  Install? (y/n): " ans
  if [ "$ans" = "y" ]; then
    echo "  Running: $cmd"
    eval "$cmd"
  fi
done

echo ""
echo "Done. Naya terminal kholo aur phir se check karo: ./setup.sh"
