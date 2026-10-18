#!/usr/bin/env sh
set -eu

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 https://memory.example.com API_TOKEN" >&2
  exit 2
fi

base_url="${1%/}"
token="$2"
suffix="$(date +%s)-$$"
user_id="smoke:user:$suffix"
session_id="smoke:session:$suffix"
request_id="smoke:add:$suffix"
timestamp_ms="$(($(date +%s) * 1000))"

curl --fail --silent --show-error "$base_url/health"
printf '\n'

curl --fail --silent --show-error \
  -X POST "$base_url/add" \
  -H "Authorization: Bearer $token" \
  -H 'Content-Type: application/json' \
  --data "{\"request_id\":\"$request_id\",\"messages\":[{\"role\":\"user\",\"content\":\"My favorite observatory is on Mauna Kea.\",\"timestamp\":$timestamp_ms}],\"user_id\":\"$user_id\",\"session_id\":\"$session_id\"}"
printf '\n'

result="$(curl --fail --silent --show-error \
  -X POST "$base_url/search" \
  -H "Authorization: Bearer $token" \
  -H 'Content-Type: application/json' \
  --data "{\"query\":\"Which observatory location does the user prefer?\",\"options\":[\"A. Mauna Kea\",\"B. Atacama\"],\"user_id\":\"$user_id\",\"top_k\":100}")"

printf '%s\n' "$result"
printf '%s' "$result" | grep -q 'Mauna Kea'
echo 'Smoke passed.'

