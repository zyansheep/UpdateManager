#!/usr/bin/env bash
# The root switch helper against fake nix-store, nix-env and systemd-run, as an
# unprivileged user: what it refuses, what it runs, what it records.
#   bash tests/test-helper.sh [HELPER] [SYSTEM]
# SYSTEM is any NixOS system already in this store (default: /run/current-system).
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
helper=${1:-$here/helper/update-manager-switch}
system=$(readlink -f "${2:-/run/current-system}")
name=$(basename "$system")
host=$(sed -E 's/^[a-z0-9]{32}-nixos-system-(.*)-[0-9][^-]*$/\1/' <<<"$name")
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
mkdir -p "$scratch/bin"
log=$scratch/calls

cat >"$scratch/bin/nix-store" <<EOF
#!/usr/bin/env bash
echo "nix-store \$*" >>"$log"
if [ "\$1" = --check-validity ]; then exec test -e "\$2"; fi
echo '@nix {"action":"msg","level":3,"msg":"these 2 paths will be fetched (10.0 MiB download, 30.0 MiB unpacked):"}' >&2
[ -z "\${FAIL_FETCH:-}" ]
EOF
cat >"$scratch/bin/nix-env" <<EOF
#!/usr/bin/env bash
echo "nix-env \$*" >>"$log"
EOF
cat >"$scratch/bin/systemd-run" <<EOF
#!/usr/bin/env bash
echo "systemd-run \$*" >>"$log"
[ -z "\${FAIL_ACTIVATE:-}" ]
EOF
cat >"$scratch/bin/update-manager" <<EOF
#!/usr/bin/env bash
PYTHONPATH=$here/src\${PYTHONPATH:+:\$PYTHONPATH} exec python3 -m update_manager "\$@"
EOF
chmod +x "$scratch"/bin/*

export PATH=$scratch/bin:$PATH UPDATE_MANAGER_HOST=$host UPDATE_MANAGER_SYSTEM_RUNTIME=$scratch/run \
  UPDATE_MANAGER_SYSTEM_STATE=$scratch/state UPDATE_MANAGER_SYSTEM_PROFILE=$scratch/profile/system \
  UPDATE_MANAGER_CURRENT_SYSTEM=$system UPDATE_MANAGER_SUBSTITUTERS="http://cache:5000?priority=50"
failures=0
check() {
  if eval "$2"; then echo "ok: $1"; else echo "FAIL: $1"; failures=$((failures + 1)); fi
}

# Refusals: another host's system, not a system, path tricks, bad modes.
for bad in "${name/nixos-system-$host/nixos-system-otherhost}" "${name:0:32}-hello-2.12" "../../etc/passwd" \
  "$name/../x" "$name;reboot" ""; do
  check "refuses '$bad'" "! bash '$helper' switch '$bad' >/dev/null 2>&1"
done
check "refuses mode 'test'" "! bash '$helper' test '$name' >/dev/null 2>&1"
check "ran nothing for them" "[ ! -e '$log' ]"

hash=${name:0:32}
bash "$helper" switch "$name" >"$scratch/out" 2>&1
check "switch succeeds" "grep -q 'switched to' '$scratch/run/$hash.result.json'"
check "fetches with the extra substituter" "grep -q -- '--realise $system --log-format internal-json --option extra-substituters http://cache:5000?priority=50' '$log'"
check "sets the profile" "grep -q 'nix-env --profile $scratch/profile/system --set $system' '$log'"
check "activates in its own unit" "grep -q 'systemd-run .*--unit=update-manager-switch-to-configuration $system/bin/switch-to-configuration switch' '$log'"
check "records the download" "python3 -c \"import json,sys; h=json.load(open('$scratch/state/history.json')); sys.exit(0 if h[-1]['bytes']==10*2**20 and h[-1]['ok'] else 1)\""

rm -f "$log"
bash "$helper" boot "$name" >/dev/null 2>&1
check "boot runs switch-to-configuration boot" "grep -q 'switch-to-configuration boot' '$log'"
check "boot says to reboot" "grep -q 'reboot to use it' '$scratch/run/$hash.result.json'"

rm -f "$log"
check "a failed fetch fails" "! FAIL_FETCH=1 bash '$helper' switch '$name' >/dev/null 2>&1"
check "and touches no profile" "! grep -q nix-env '$log'"
check "and says so" "grep -q '\"ok\":false' '$scratch/run/$hash.result.json'"
check "a failed activation fails" "! FAIL_ACTIVATE=1 bash '$helper' switch '$name' >/dev/null 2>&1"
check "and says so" "grep -q 'switch-to-configuration switch exited' '$scratch/run/$hash.result.json'"

[ "$failures" = 0 ] && echo "all helper tests passed" || { echo "$failures failed"; exit 1; }
