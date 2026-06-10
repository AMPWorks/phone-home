#!/usr/bin/env bash
#
# make-claude-shortcut.sh
#
# Generates and signs an Apple Shortcut (.shortcut) that deep-links into the
# Claude iOS app. Assign the result to the Action Button (Settings -> Action
# Button -> Shortcut) for one-press access to a remote Claude Code session.
#
# Two modes:
#   resume  (default)  -> claude://code/{session-id}
#   new                -> claude://code/new?repo=owner%2Fname&branch=...&q=...
#
# Requirements: macOS with the `shortcuts` CLI (macOS 12+). Signing cannot be
# done on Linux. After signing, `open` the file to import it (it iCloud-syncs
# to the iPhone), then bind it to the Action Button there.
#
# Examples:
#   ./make-claude-shortcut.sh -n amp-agent -s d6e0d8ac-a5e3-4048-8c39-89fb9884d835
#   ./make-claude-shortcut.sh -n amp-agent --new -r ampworksstudio/amp-agent -b main
#   ./make-claude-shortcut.sh -n amp-agent --new -r ampworksstudio/amp-agent -q "resume the build"

set -euo pipefail

NAME="amp-agent"
SESSION_ID=""
MODE="resume"
REPO=""
BRANCH=""
PROMPT=""
OUTDIR="."

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# minimal RFC-3986 encoder for query values (encodes / and spaces, etc.)
urlencode() {
  local s="$1" out="" c i
  for (( i=0; i<${#s}; i++ )); do
    c="${s:i:1}"
    case "$c" in
      [a-zA-Z0-9.~_-]) out+="$c" ;;
      *) printf -v c '%%%02X' "'$c"; out+="$c" ;;
    esac
  done
  printf '%s' "$out"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--name)    NAME="$2"; shift 2 ;;
    -s|--session) SESSION_ID="$2"; shift 2 ;;
    --new)        MODE="new"; shift ;;
    -r|--repo)    REPO="$2"; shift 2 ;;
    -b|--branch)  BRANCH="$2"; shift 2 ;;
    -q|--prompt)  PROMPT="$2"; shift 2 ;;
    -o|--out)     OUTDIR="$2"; shift 2 ;;
    -h|--help)    usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done

# Build the claude:// URL
if [[ "$MODE" == "resume" ]]; then
  [[ -n "$SESSION_ID" ]] || { echo "resume mode needs -s <session-id>" >&2; exit 1; }
  URL="claude://code/${SESSION_ID}"
else
  URL="claude://code/new"
  q=()
  [[ -n "$REPO"   ]] && q+=("repo=$(urlencode "$REPO")")
  [[ -n "$BRANCH" ]] && q+=("branch=$(urlencode "$BRANCH")")
  [[ -n "$PROMPT" ]] && q+=("q=$(urlencode "$PROMPT")")
  [[ ${#q[@]} -gt 0 ]] && URL+="?$(IFS='&'; echo "${q[*]}")"
fi

echo "URL:  $URL"
echo "Name: $NAME"

UNSIGNED="$(mktemp -t shortcut.XXXXXX).plist"
SIGNED="${OUTDIR%/}/${NAME}.shortcut"
trap 'rm -f "$UNSIGNED"' EXIT

# WFWorkflow plist: URL action -> Open URLs (implicit input chaining)
cat > "$UNSIGNED" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>WFWorkflowActions</key>
    <array>
        <dict>
            <key>WFWorkflowActionIdentifier</key>
            <string>is.workflow.actions.url</string>
            <key>WFWorkflowActionParameters</key>
            <dict>
                <key>WFURLActionURL</key>
                <string>${URL}</string>
            </dict>
        </dict>
        <dict>
            <key>WFWorkflowActionIdentifier</key>
            <string>is.workflow.actions.openurls</string>
            <key>WFWorkflowActionParameters</key>
            <dict/>
        </dict>
    </array>
    <key>WFWorkflowClientVersion</key>
    <string>2607.0.3</string>
    <key>WFWorkflowMinimumClientVersion</key>
    <integer>900</integer>
    <key>WFWorkflowMinimumClientVersionString</key>
    <string>900</string>
    <key>WFWorkflowIcon</key>
    <dict>
        <key>WFWorkflowIconStartColor</key>
        <integer>4282601983</integer>
        <key>WFWorkflowIconGlyphNumber</key>
        <integer>61440</integer>
    </dict>
    <key>WFWorkflowImportQuestions</key>
    <array/>
    <key>WFWorkflowInputContentItemClasses</key>
    <array>
        <string>WFStringContentItem</string>
        <string>WFURLContentItem</string>
    </array>
    <key>WFWorkflowTypes</key>
    <array>
        <string>ActionExtension</string>
        <string>NCWidget</string>
    </array>
</dict>
</plist>
PLIST

command -v shortcuts >/dev/null || {
  echo "error: 'shortcuts' CLI not found (need macOS 12+). Unsigned plist left at: $UNSIGNED" >&2
  trap - EXIT
  exit 1
}

shortcuts sign --mode anyone --input "$UNSIGNED" --output "$SIGNED"
echo "Signed: $SIGNED"
echo "Import with:  open \"$SIGNED\""
echo "Then: Settings -> Action Button -> Shortcut -> ${NAME}"
