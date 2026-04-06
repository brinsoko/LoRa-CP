#!/usr/bin/env bash
set -euo pipefail

# Hard-code your target here.
BASE_URL="127.0.0.1:5001"

# Hard-code a real admin or judge account here.
USERNAME="admin"
PASSWORD="change-me-now"

# Hard-code IDs that exist on the target instance.
COMPETITION_ID="1"
TEAM_ID="7"
CHECKPOINT_ID="1"

USER_AGENT="LoRa-KT-Int-Smoke/1.0"
COOKIE_JAR="${TMPDIR:-/tmp}/lora-kt-int-smoke.cookies"
HEADER_FILE=""

PASS_COUNT=0
FAIL_COUNT=0


cleanup() {
  rm -f "$COOKIE_JAR"
  [[ -n "${BODY_FILE:-}" ]] && rm -f "${BODY_FILE}" || true
  [[ -n "${HEADER_FILE:-}" ]] && rm -f "${HEADER_FILE}" || true
}


curl_common() {
  curl -sS \
    -A "$USER_AGENT" \
    -H "Accept-Language: en" \
    -b "$COOKIE_JAR" \
    -c "$COOKIE_JAR" \
    "$@"
}


print_section() {
  printf '\n== %s ==\n' "$1"
}


pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
  printf 'PASS: %s\n' "$1"
}


fail() {
  FAIL_COUNT=$((FAIL_COUNT + 1))
  printf 'FAIL: %s\n' "$1"
}


require_value() {
  local name="$1"
  local value="$2"
  if [[ -z "$value" ]]; then
    printf 'Missing required config: %s\n' "$name" >&2
    exit 1
  fi
}


assert_status() {
  local label="$1"
  local expected="$2"
  if [[ "$STATUS_CODE" == "$expected" ]]; then
    pass "$label returned HTTP $expected"
  else
    fail "$label returned HTTP $STATUS_CODE, expected $expected"
    sed -n '1,120p' "$BODY_FILE"
  fi
}


assert_contains() {
  local label="$1"
  local needle="$2"
  if grep -Fq "$needle" "$BODY_FILE"; then
    pass "$label contains: $needle"
  else
    fail "$label is missing: $needle"
    sed -n '1,120p' "$BODY_FILE"
  fi
}


assert_not_contains() {
  local label="$1"
  local needle="$2"
  if grep -Fq "$needle" "$BODY_FILE"; then
    fail "$label unexpectedly contains: $needle"
    sed -n '1,120p' "$BODY_FILE"
  else
    pass "$label does not contain: $needle"
  fi
}


request_with_headers() {
  BODY_FILE="$(mktemp)"
  HEADER_FILE="$(mktemp)"
  STATUS_CODE="$(
    curl_common \
      -D "$HEADER_FILE" \
      -w '%{http_code}' \
      "$@" \
      -o "$BODY_FILE"
  )"
}


follow_redirect_as_get() {
  local location
  location="$(awk 'BEGIN{IGNORECASE=1} /^Location:/ {sub(/\r$/, "", $2); print $2; exit}' "$HEADER_FILE")"
  if [[ -z "$location" ]]; then
    location="$(sed -n 's/.*<a href="\([^"]*\)".*/\1/p' "$BODY_FILE" | head -n 1)"
  fi
  if [[ -z "$location" ]]; then
    fail "Redirect response did not include a Location target"
    sed -n '1,120p' "$BODY_FILE"
    exit 1
  fi

  if [[ "$location" != http://* && "$location" != https://* ]]; then
    location="${BASE_URL}${location}"
  fi

  BODY_FILE="$(mktemp)"
  STATUS_CODE="$(
    curl_common \
      -w '%{http_code}' \
      "$location" \
      -o "$BODY_FILE"
  )"
}


assert_redirect() {
  local label="$1"
  if [[ "$STATUS_CODE" == "302" || "$STATUS_CODE" == "303" ]]; then
    pass "$label returned redirect"
  else
    fail "$label returned HTTP $STATUS_CODE, expected 302 or 303"
    sed -n '1,120p' "$BODY_FILE"
  fi
}


preflight_get() {
  local label="$1"
  local url="$2"
  request_with_headers "$url"
  if [[ "$STATUS_CODE" == "302" || "$STATUS_CODE" == "303" ]]; then
    follow_redirect_as_get
  fi
  if [[ "$STATUS_CODE" != "200" ]]; then
    fail "$label preflight failed with HTTP $STATUS_CODE"
    sed -n '1,120p' "$BODY_FILE"
    exit 1
  fi
  pass "$label preflight returned HTTP 200"
}


perform_login() {
  request_with_headers \
    -X POST "$BASE_URL/login" \
    --data-urlencode "username=$USERNAME" \
    --data-urlencode "password=$PASSWORD"

  if [[ "$STATUS_CODE" != "302" && "$STATUS_CODE" != "303" ]]; then
    fail "Login did not redirect after POST"
    sed -n '1,120p' "$BODY_FILE"
    exit 1
  fi

  BODY_FILE="$(mktemp)"
  STATUS_CODE="$(
    curl_common \
      -L \
      -w '%{http_code}' \
      "$BASE_URL/competitions" \
      -o "$BODY_FILE"
  )"
  assert_status "GET /competitions after login" "200"
  assert_not_contains "GET /competitions after login" "Sign In"
}


select_competition() {
  print_section "Competition"
  request_with_headers \
    -X POST "$BASE_URL/competitions/select/$COMPETITION_ID"
  assert_redirect "Select competition"
  follow_redirect_as_get
  assert_status "GET redirect target after competition select" "200"
}


test_teams_invalid_group_query() {
  print_section "Teams"
  BODY_FILE="$(mktemp)"
  STATUS_CODE="$(
    curl_common \
      -L \
      -w '%{http_code}' \
      "$BASE_URL/teams/?group_id=abc" \
      -o "$BODY_FILE"
  )"
  assert_status "Teams list with invalid group_id" "200"
  assert_contains "Teams list with invalid group_id" "Group must be an integer."
  assert_not_contains "Teams list with invalid group_id" "500 Internal Server Error"
}


test_add_team_invalid_number() {
  BODY_FILE="$(mktemp)"
  STATUS_CODE="$(
    curl_common \
      -L \
      -w '%{http_code}' \
      -X POST "$BASE_URL/teams/add" \
      --data-urlencode "name=Smoke Team Invalid Number" \
      --data-urlencode "number=abc" \
      --data-urlencode "organization=QA" \
      -o "$BODY_FILE"
  )"
  assert_status "Add team with invalid number" "200"
  assert_contains "Add team with invalid number" "Team number must be an integer."
  assert_not_contains "Add team with invalid number" "500 Internal Server Error"
}


test_add_team_invalid_rfid_number() {
  BODY_FILE="$(mktemp)"
  STATUS_CODE="$(
    curl_common \
      -L \
      -w '%{http_code}' \
      -X POST "$BASE_URL/teams/add" \
      --data-urlencode "name=Smoke Team Invalid RFID Number" \
      --data-urlencode "number=123" \
      --data-urlencode "organization=QA" \
      --data-urlencode "rfid_uid=ABC123" \
      --data-urlencode "rfid_number=xyz" \
      -o "$BODY_FILE"
  )"
  assert_status "Add team with invalid RFID number" "200"
  assert_contains "Add team with invalid RFID number" "RFID number must be an integer."
  assert_not_contains "Add team with invalid RFID number" "500 Internal Server Error"
}


test_edit_team_invalid_number() {
  preflight_get "Edit team" "$BASE_URL/teams/$TEAM_ID/edit"
  BODY_FILE="$(mktemp)"
  STATUS_CODE="$(
    curl_common \
      -L \
      -w '%{http_code}' \
      -X POST "$BASE_URL/teams/$TEAM_ID/edit" \
      --data-urlencode "name=Edited Team" \
      --data-urlencode "number=not-a-number" \
      --data-urlencode "organization=QA" \
      -o "$BODY_FILE"
  )"
  assert_status "Edit team with invalid number" "200"
  assert_contains "Edit team with invalid number" "Team number must be an integer."
  assert_not_contains "Edit team with invalid number" "500 Internal Server Error"
}


test_edit_team_invalid_rfid_number() {
  preflight_get "Edit team" "$BASE_URL/teams/$TEAM_ID/edit"
  BODY_FILE="$(mktemp)"
  STATUS_CODE="$(
    curl_common \
      -L \
      -w '%{http_code}' \
      -X POST "$BASE_URL/teams/$TEAM_ID/edit" \
      --data-urlencode "name=Edited Team" \
      --data-urlencode "number=123" \
      --data-urlencode "organization=QA" \
      --data-urlencode "rfid_uid=ABC123" \
      --data-urlencode "rfid_number=not-a-number" \
      -o "$BODY_FILE"
  )"
  assert_status "Edit team with invalid RFID number" "200"
  assert_contains "Edit team with invalid RFID number" "RFID number must be an integer."
  assert_not_contains "Edit team with invalid RFID number" "500 Internal Server Error"
}


test_add_checkpoint_invalid_device_id() {
  print_section "Checkpoints"
  BODY_FILE="$(mktemp)"
  STATUS_CODE="$(
    curl_common \
      -L \
      -w '%{http_code}' \
      -X POST "$BASE_URL/checkpoints/add" \
      --data-urlencode "name=Smoke CP Invalid Device" \
      --data-urlencode "lora_device_id=abc" \
      -o "$BODY_FILE"
  )"
  assert_status "Add checkpoint with invalid device id" "200"
  assert_contains "Add checkpoint with invalid device id" "Device ID must be an integer."
  assert_not_contains "Add checkpoint with invalid device id" "500 Internal Server Error"
}


test_add_checkpoint_invalid_group_id() {
  BODY_FILE="$(mktemp)"
  STATUS_CODE="$(
    curl_common \
      -L \
      -w '%{http_code}' \
      -X POST "$BASE_URL/checkpoints/add" \
      --data-urlencode "name=Smoke CP Invalid Group" \
      --data-urlencode "group_ids=abc" \
      -o "$BODY_FILE"
  )"
  assert_status "Add checkpoint with invalid group id" "200"
  assert_contains "Add checkpoint with invalid group id" "Group ID must be an integer."
  assert_not_contains "Add checkpoint with invalid group id" "500 Internal Server Error"
}


test_edit_checkpoint_invalid_device_id() {
  preflight_get "Edit checkpoint" "$BASE_URL/checkpoints/$CHECKPOINT_ID/edit"
  BODY_FILE="$(mktemp)"
  STATUS_CODE="$(
    curl_common \
      -L \
      -w '%{http_code}' \
      -X POST "$BASE_URL/checkpoints/$CHECKPOINT_ID/edit" \
      --data-urlencode "name=Edited CP" \
      --data-urlencode "lora_device_id=abc" \
      -o "$BODY_FILE"
  )"
  assert_status "Edit checkpoint with invalid device id" "200"
  assert_contains "Edit checkpoint with invalid device id" "Device ID must be an integer."
  assert_not_contains "Edit checkpoint with invalid device id" "500 Internal Server Error"
}


test_edit_checkpoint_invalid_group_id() {
  preflight_get "Edit checkpoint" "$BASE_URL/checkpoints/$CHECKPOINT_ID/edit"
  BODY_FILE="$(mktemp)"
  STATUS_CODE="$(
    curl_common \
      -L \
      -w '%{http_code}' \
      -X POST "$BASE_URL/checkpoints/$CHECKPOINT_ID/edit" \
      --data-urlencode "name=Edited CP" \
      --data-urlencode "group_ids=abc" \
      -o "$BODY_FILE"
  )"
  assert_status "Edit checkpoint with invalid group id" "200"
  assert_contains "Edit checkpoint with invalid group id" "Group ID must be an integer."
  assert_not_contains "Edit checkpoint with invalid group id" "500 Internal Server Error"
}


test_set_active_group_invalid_group_id() {
  print_section "Groups"
  request_with_headers \
    -X POST "$BASE_URL/groups/set_active" \
    --data-urlencode "team_id=$TEAM_ID" \
    --data-urlencode "group_id=abc"
  assert_redirect "Set active group with invalid group id"
  follow_redirect_as_get
  assert_status "GET redirect target after invalid set_active" "200"
  assert_contains "Set active group with invalid group id" "group_id must be an integer."
  assert_not_contains "Set active group with invalid group id" "500 Internal Server Error"
}


main() {
  trap cleanup EXIT

  require_value "BASE_URL" "$BASE_URL"
  require_value "USERNAME" "$USERNAME"
  require_value "PASSWORD" "$PASSWORD"
  require_value "COMPETITION_ID" "$COMPETITION_ID"
  require_value "TEAM_ID" "$TEAM_ID"
  require_value "CHECKPOINT_ID" "$CHECKPOINT_ID"

  rm -f "$COOKIE_JAR"

  perform_login
  select_competition

  test_teams_invalid_group_query
  test_add_team_invalid_number
  test_add_team_invalid_rfid_number
  test_edit_team_invalid_number
  test_edit_team_invalid_rfid_number
  test_add_checkpoint_invalid_device_id
  test_add_checkpoint_invalid_group_id
  test_edit_checkpoint_invalid_device_id
  test_edit_checkpoint_invalid_group_id
  test_set_active_group_invalid_group_id

  print_section "Summary"
  printf 'Passed: %s\n' "$PASS_COUNT"
  printf 'Failed: %s\n' "$FAIL_COUNT"

  if [[ "$FAIL_COUNT" -gt 0 ]]; then
    exit 1
  fi
}


main "$@"
