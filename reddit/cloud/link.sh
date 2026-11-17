#!/bin/bash
# Put the phone bookmark on the clipboard, and print it.
#
# Run this yourself in a terminal. It is kept out of any transcript on purpose:
# the previous key was burned by being pasted into a chat window, and a key in a
# URL is a bearer token.
KEY_FILE="$HOME/Library/Application Support/clarence-aeo/cockpit-key"
if [ ! -f "$KEY_FILE" ]; then
  echo "No key at $KEY_FILE. Start the local board once, or rotate a new one." >&2
  exit 1
fi
URL="https://clarence-reddit.<your-subdomain>.workers.dev/m?k=$(tr -d '\n' < "$KEY_FILE")"
printf '%s' "$URL" | pbcopy
echo "$URL"
echo
echo "Copied to the clipboard."
echo
echo "On Android:"
echo "  Send it to yourself however you already do (Messages, Signal, email, or"
echo "  Google Keep), open it in CHROME, then menu > Add to Home screen."
echo "The key moves into a cookie on first load, so every later visit carries no"
echo "key in the URL and the home screen icon keeps working on its own."
