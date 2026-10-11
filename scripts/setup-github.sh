#!/usr/bin/env bash
set -euo pipefail

source_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
target=${1:?Usage: scripts/setup-github.sh /path/to/target-repo}
[[ -t 0 ]] || { echo 'Run this script in a terminal so secrets can be entered hidden.' >&2; exit 1; }
repo=$(cd "$target" && gh repo view --json nameWithOwner --jq .nameWithOwner)
[[ -n $repo ]] || { echo 'Could not identify the target GitHub repository.' >&2; exit 1; }
read -r -p 'Shared Prisme config path (Enter for per-repository config): ' shared_config
if [[ -n $shared_config ]]; then
  [[ $shared_config =~ ^[A-Za-z0-9_./-]+$ && $shared_config != /* && $shared_config != *..* && -f "$source_dir/$shared_config" ]] || { echo 'Shared config must name a JSON file inside Prisme.' >&2; exit 1; }
  provider=$(jq -er '.estimate.provider' "$source_dir/$shared_config")
fi
files=(.github/workflows/prism-estimate.yml .github/workflows/prism-cfo-allocation.yml)
if [[ -z $shared_config ]]; then files+=(.github/prism.json); fi
for file in "${files[@]}"; do
  [[ ! -e "$target/$file" ]] || { echo "$target/$file already exists; move it before setup." >&2; exit 1; }
done

if [[ -z $shared_config ]]; then
keys=(org_id scenario page budget_table request_table decision_table column_prefix cooldown_minutes provider worker_model max_cost_usd max_output_tokens max_extra_rounds timeout_seconds)
defaults=('' 'Main Scenario' '' '' '' '' '' 10 claude claude-sonnet-5-5 0.05 4000 1 180)
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

read -r -p "Shared CFO login repositories (comma-separated owner/repo, Enter for only $repo): " shared_repos
if [[ -n $shared_repos ]]; then
  IFS=, read -r -a shared_repo_list <<< "$shared_repos"
  found_current=false
  for shared_repo in "${shared_repo_list[@]}"; do
    [[ $shared_repo =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "Invalid repository: $shared_repo" >&2; exit 1; }
    [[ $shared_repo == "$repo" ]] && found_current=true
  done
  [[ $found_current == true ]] || { echo "Shared login list must include $repo" >&2; exit 1; }
fi
provider=${values[8]}
fi

secrets=(ANTHROPIC_API_KEY OPENAI_API_KEY NEATLOGS_API_KEY ENTIRE_TOKEN PRISME_READ_TOKEN CFO_OAUTH_CLIENT_ID CFO_OAUTH_REFRESH_TOKEN CFO_SECRET_WRITE_TOKEN)
for name in "${secrets[@]}"; do
  label=$name
  optional=false
  if [[ $name == NEATLOGS_API_KEY || $name == PRISME_READ_TOKEN || ( $name == ANTHROPIC_API_KEY && $provider != claude ) || ( $name == OPENAI_API_KEY && $provider != openai ) ]]; then optional=true; label+=' (optional, Enter to skip)'; fi
  while :; do
    read -r -s -p "$label: " value
    printf '\n'
    if [[ -n $value || $optional == true ]]; then break; fi
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
if [[ -z $shared_config ]]; then
{
  printf '{\n  "cfo": {\n'
  for i in {0..6}; do
    printf '    "%s": ' "${keys[i]}"
    json_string "${values[i]}"
    if (( i < 6 )) || [[ -n $shared_repos ]]; then printf ','; fi
    printf '\n'
  done
  if [[ -n $shared_repos ]]; then
    printf '    "shared_login_repos": ['
    for i in "${!shared_repo_list[@]}"; do
      (( i == 0 )) || printf ', '
      json_string "${shared_repo_list[i]}"
    done
    printf ']\n'
  fi
  printf '  },\n  "estimate": {\n'
  for i in {7..13}; do
    printf '    "%s": ' "${keys[i]}"
    if [[ ${keys[i]} == provider || ${keys[i]} == worker_model ]]; then
      json_string "${values[i]}"
    else
      printf '%s' "${values[i]}"
    fi
    if (( i < 13 )); then printf ','; fi
    printf '\n'
  done
  printf '  }\n}\n'
} > "$target/.github/prism.json"
fi
cp "$source_dir/examples/prism-estimate.yml" "$target/.github/workflows/prism-estimate.yml"
cp "$source_dir/examples/prism-cfo-allocation.yml" "$target/.github/workflows/prism-cfo-allocation.yml"
if [[ -n $shared_config ]]; then
  for file in "${files[@]}"; do
    awk -v config="$shared_config" '{ print; if ($0 == "      prisme_ref: main") print "      config: " config }' "$target/$file" > "$target/$file.tmp"
    mv "$target/$file.tmp" "$target/$file"
  done
fi
echo "Prism files are ready in $target. Commit and push them to the repository's default branch."
