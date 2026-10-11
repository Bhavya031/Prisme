#!/usr/bin/env bash
set -euo pipefail

source_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
target=${1:?Usage: scripts/setup-github.sh /path/to/target-repo}
[[ -t 0 ]] || { echo 'Run this script in a terminal so secrets can be entered hidden.' >&2; exit 1; }
repo=$(cd "$target" && gh repo view --json nameWithOwner --jq .nameWithOwner)
[[ -n $repo ]] || { echo 'Could not identify the target GitHub repository.' >&2; exit 1; }
for file in .github/prism.json .github/workflows/prism-estimate.yml .github/workflows/prism-cfo-allocation.yml; do
  [[ ! -e "$target/$file" ]] || { echo "$target/$file already exists; move it before setup." >&2; exit 1; }
done

keys=(org_id scenario page budget_table request_table column_prefix cooldown_minutes provider worker_model max_cost_usd max_output_tokens max_extra_rounds timeout_seconds)
defaults=('' 'Main Scenario' '' '' '' '' 10 claude claude-sonnet-5-5 0.05 4000 1 180)
values=()
for i in "${!keys[@]}"; do
  key=${keys[i]}
  default=${defaults[i]}
  while :; do
    if [[ -n $default ]]; then
      read -r -p "$key [$default]: " value
      value=${value:-$default}
    else
      read -r -p "$key: " value
    fi
    if [[ -z $value || $value =~ [[:cntrl:]] ]]; then
      echo 'Enter a nonempty, single-line value without control characters.' >&2
    elif [[ $key == cooldown_minutes || $key == max_output_tokens || $key == max_extra_rounds || $key == timeout_seconds ]] && [[ ! $value =~ ^[0-9]+$ ]]; then
      echo 'Enter a nonnegative whole number.' >&2
    elif [[ $key == max_cost_usd && ! $value =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
      echo 'Enter a nonnegative USD number.' >&2
    else
      values+=("$value")
      break
    fi
  done
done

secrets=(ANTHROPIC_API_KEY NEATLOGS_API_KEY ENTIRE_TOKEN PRISME_READ_TOKEN CFO_OAUTH_CLIENT_ID CFO_OAUTH_REFRESH_TOKEN CFO_SECRET_WRITE_TOKEN)
for name in "${secrets[@]}"; do
  label=$name
  if [[ $name == NEATLOGS_API_KEY ]]; then label+=' (optional, Enter to skip)'; fi
  while :; do
    read -r -s -p "$label: " value
    printf '\n'
    if [[ -n $value || $name == NEATLOGS_API_KEY ]]; then break; fi
    echo "$name is required." >&2
  done
  if [[ -n $value ]]; then
    printf '%s' "$value" | gh secret set "$name" --repo "$repo"
  fi
  unset value
done

json_string() {
  local escaped=${1//\\/\\\\}
  escaped=${escaped//\"/\\\"}
  printf '"%s"' "$escaped"
}

mkdir -p "$target/.github/workflows"
{
  printf '{\n  "cfo": {\n'
  for i in {0..5}; do
    printf '    "%s": ' "${keys[i]}"
    json_string "${values[i]}"
    if (( i < 5 )); then printf ','; fi
    printf '\n'
  done
  printf '  },\n  "estimate": {\n'
  for i in {6..12}; do
    printf '    "%s": ' "${keys[i]}"
    if [[ ${keys[i]} == provider || ${keys[i]} == worker_model ]]; then
      json_string "${values[i]}"
    else
      printf '%s' "${values[i]}"
    fi
    if (( i < 12 )); then printf ','; fi
    printf '\n'
  done
  printf '  }\n}\n'
} > "$target/.github/prism.json"
cp "$source_dir/examples/prism-estimate.yml" "$target/.github/workflows/prism-estimate.yml"
cp "$source_dir/examples/prism-cfo-allocation.yml" "$target/.github/workflows/prism-cfo-allocation.yml"
echo "Prism files are ready in $target. Commit and push them to the repository's default branch."
