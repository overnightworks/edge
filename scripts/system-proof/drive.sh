#!/bin/bash
# Drives the cells of #40's scenario matrix inside the proof container, as root, one subcommand per stage;
# run.sh calls the stages in order and restarts the container between the reboot stages.
# Every cell prints one line `cell <n>: held|FAILED: <what>`; each log lands in /proof/evidence.
set -uo pipefail

EVIDENCE=/proof/evidence
HOME_BIN=/home/tester/.local/bin/home
UNIT_DIR=/etc/systemd/system
SYSTEM_FILES=(
    "$UNIT_DIR/cloudflared.service"
    "$UNIT_DIR/cloudflared.service.d/20-nonroot.conf"
    "$UNIT_DIR/cloudflared.service.d/30-edge-global-stop.conf"
    /etc/polkit-1/rules.d/10-edge-cloudflared-stop.rules
)
RETIRED_DROP_IN="$UNIT_DIR/cloudflared.service.d/10-root-binary.conf"
MARKER=/home/tester/.local/state/edge/global-stop
# The properties the operator's ruling „sauber ersetzen“ keeps equal; ExecStart is compared apart from its path.
SHOWN=ExecStart,Type,User,DynamicUser,LoadCredential,Environment,ProtectSystem,ProtectHome,PrivateTmp,NoNewPrivileges,TimeoutStartUSec,Restart
RETENTION_SERVICE=hal-journal-retention.service
RETENTION_TIMER=hal-journal-retention.timer
mkdir -p "$EVIDENCE"

verdict() {  # verdict <cell> <what> <condition>: held when the condition, a shell expression, is true
    local cell=$1 what=$2 condition=$3
    if eval "$condition"; then echo "cell $cell: held: $what"; else echo "cell $cell: FAILED: $what"; fi
    return 0
}

at_terminal() {  # at_terminal <log> <command>: as tester at a pseudo-terminal; the exit code is the command's
    local log=$1 command=$2
    /proof/terminal.exp "$command" >"$EVIDENCE/$log.log" 2>&1
    return
}

without_terminal() {  # without_terminal <log> <command>: as tester with no terminal on any end
    local log=$1 command=$2
    timeout 300 su - tester -c "$command" </dev/null >"$EVIDENCE/$log.log" 2>&1
    return
}

in_log() {
    local log=$1 text=$2
    grep -qF -- "$text" "$EVIDENCE/$log.log"
    return
}

asked_once() {
    local log=$1
    grep -qx 'PROMPTS=1' "$EVIDENCE/$log.log"
    return
}

snapshot() {
    find "$UNIT_DIR" /etc/polkit-1/rules.d -path '*cloudflared*' -type f -exec sha256sum {} + 2>/dev/null | sort
    return
}

as_tester() {
    runuser -u tester -- "$@"
    return
}

refused() {  # refused <unit> <command...>: polkit says no, for a unit that is loaded; "not found" is no refusal
    local unit=$1 said
    shift
    [[ $(systemctl show "$unit" -p LoadState --value) == loaded ]] || return 1
    said=$("$@" 2>&1) && return 1
    echo "$said" >>"$EVIDENCE/polkit.log"
    [[ $said == *"Interactive authentication required"* || $said == *"Access denied"* ]]
    return
}

all_exist() {
    local file
    for file in "$@"; do [[ -f $file ]] || return 1; done
    return 0
}

install_home() {  # the wheel, and the user manager `hal setup`'s user-units step talks to, running without a login
    loginctl enable-linger tester
    timeout 60 bash -c 'until systemctl is-active --quiet "user@$(id -u tester).service"; do sleep 1; done'
    su - tester -c "uv tool install /proof/*.whl" >"$EVIDENCE/install.log" 2>&1
    plant_fake_secrets >>"$EVIDENCE/install.log" 2>&1
    return
}

# `hal setup --apply` ends with the secrets step, which would walk the wizard at the cells' terminal for every secret the
# packaged manifest names. The tester's home therefore holds a fake file for each one, never a real secret, and the
# secrets cell takes one away and puts it back.
plant_fake_secrets() {  # plant_fake_secrets [drop]: a fake file for every live entry of the manifest; with `drop`, the first is taken away again and its name printed
    local python
    python=$(head -n 1 "$HOME_BIN" | sed 's/^#!//')
    cat >/proof/plant_secrets.py <<'PYTHON'
import sys
from pathlib import Path

from home_tools import secrets_view

home = Path.home()
live = [entry for entry in secrets_view.load_manifest(secrets_view.MANIFEST_PATH) if not entry.retired]
places = [home / str(pattern).replace("*", "fake") for entry in live for pattern in entry.paths]
for path in places:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("proof-fake-secret")
    path.chmod(0o600)
for directory in [home / ".secrets", *(home / ".secrets").rglob("*")]:
    if directory.is_dir():
        directory.chmod(0o700)
if sys.argv[1:] == ["drop"]:
    for pattern in live[0].paths:
        (home / str(pattern).replace("*", "fake")).unlink()
    print(live[0].name)
PYTHON
    su - tester -c "$python -I /proof/plant_secrets.py $*"
    return
}

# Today's PC, frozen literally, never derived from /proof/tunnel, so a changed hardening setting in edge's files shows
# as a difference. The non-ExecStart lines come from edge's EFFECTIVE_UNIT_WITHOUT_EXEC_START (tests/test_edge_tunnel_files.py,
# tunnel id as @TUNNEL_ID@); the ExecStart lines come from the head's read-only `systemctl cat cloudflared.service` of
# 18.10.2026. The effective ExecStart is the one `10-root-binary.conf` sets; the base unit's is the operator's ~/bin one.
write_todays_unit() {
    local id
    id=$(sed -n 's/^tunnel: //p' /etc/cloudflared/config.yml)
    mkdir -p "$UNIT_DIR/cloudflared.service.d"
    cat >"$UNIT_DIR/cloudflared.service" <<'UNIT'
[Unit]
Description=cloudflared
After=network-online.target
Wants=network-online.target

[Service]
TimeoutStartSec=15
Type=notify
ExecStart=/home/tester/bin/cloudflared --no-autoupdate --config /etc/cloudflared/config.yml tunnel run
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
UNIT
    cat >"$RETIRED_DROP_IN" <<'UNIT'
[Service]
ExecStart=
ExecStart=/usr/bin/cloudflared --no-autoupdate --config /etc/cloudflared/config.yml tunnel run
UNIT
    sed "s/@TUNNEL_ID@/$id/" >"$UNIT_DIR/cloudflared.service.d/20-nonroot.conf" <<'UNIT'
[Service]
DynamicUser=yes
LoadCredential=tunnel.json:/etc/cloudflared/@TUNNEL_ID@.json
Environment=TUNNEL_CRED_FILE=%d/tunnel.json
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
UNIT
    return
}

shown_without_exec_start() {
    systemctl show cloudflared -p "$SHOWN" | grep -v '^ExecStart='
    return
}

exec_start_arguments() {  # the arguments of ExecStart, without the path of the binary, which is the one permitted change
    systemctl show cloudflared -p ExecStart --value | sed -n 's/.*argv\[\]=[^ ]* \(.*\) ; ignore_errors=.*/\1/p'
    return
}

prepare_today() {
    write_todays_unit
    systemctl daemon-reload
    shown_without_exec_start >"$EVIDENCE/show-before.txt"
    exec_start_arguments >"$EVIDENCE/exec-start-before.txt"
    systemctl show cloudflared -p InvocationID,ActiveState >"$EVIDENCE/state-before.txt"
    return
}

cell_2() {
    local code
    at_terminal cell2 "$HOME_BIN setup --apply"
    code=$?
    shown_without_exec_start >"$EVIDENCE/show-after.txt"
    exec_start_arguments >"$EVIDENCE/exec-start-after.txt"
    systemctl show cloudflared -p InvocationID,ActiveState >"$EVIDENCE/state-after.txt"
    verdict 2 "apply exits 0" '[ $code -eq 0 ]'
    verdict 2 "the recorded arguments of ExecStart were there before" '[ -s "$EVIDENCE/exec-start-before.txt" ]'
    verdict 2 "systemctl show is equal apart from the ExecStart path" \
        'diff "$EVIDENCE/show-before.txt" "$EVIDENCE/show-after.txt" && diff "$EVIDENCE/exec-start-before.txt" "$EVIDENCE/exec-start-after.txt"'
    verdict 2 "10-root-binary.conf is gone" '[ ! -e "$RETIRED_DROP_IN" ]'
    verdict 2 "nothing started or restarted" 'diff "$EVIDENCE/state-before.txt" "$EVIDENCE/state-after.txt"'
    verdict 2 "the backups are outside $UNIT_DIR" '[ -n "$(find /var/backups/home-setup -name 10-root-binary.conf)" ]'
    return
}

cell_11() {
    local dry apply
    without_terminal cell11-dry "$HOME_BIN setup"
    dry=$?
    verdict 11 "a dry run without a terminal says not checked, exit 0" \
        '[ $dry -eq 0 ] && in_log cell11-dry "System: not checked (needs sudo at a terminal)"'
    without_terminal cell11-apply "$HOME_BIN setup --apply"
    apply=$?
    verdict 11 "--apply without a terminal refuses naming the command, exit 1, no hang" \
        '[ $apply -eq 1 ] && in_log cell11-apply "\`hal setup --apply\` in your own terminal window"'
    return
}

cell_12() {
    cell_12_without_a_tunnel_id missing '/^tunnel:/d'
    cell_12_without_a_tunnel_id empty 's/^tunnel:.*/tunnel:/'
    return
}

cell_12_without_a_tunnel_id() {  # <case> <sed edit of config.yml>: the `tunnel:` line gone, or left without a value
    local case_name=$1 edit=$2 log="cell12-$1" before code
    before=$(snapshot)
    cp /etc/cloudflared/config.yml /proof/config.yml.saved
    sed -i "$edit" /etc/cloudflared/config.yml
    at_terminal "$log" "$HOME_BIN setup --apply"
    code=$?
    cp /proof/config.yml.saved /etc/cloudflared/config.yml
    verdict 12 "apply fails loudly when the tunnel id is $case_name" '[ $code -eq 1 ] && in_log "$log" "names no \`tunnel:\`"'
    verdict 12 "nothing is half-written when the tunnel id is $case_name" '[ "$before" = "$(snapshot)" ]'
    return
}

cell_13() {  # cell 11 has activated the hook already, so it is undone here for the activation to run again
    local code
    su - tester -c "git config --global --unset core.hooksPath"
    PROOF_PASSWORD="not-the-password" at_terminal cell13 "$HOME_BIN setup --apply"
    code=$?
    verdict 13 "a wrong password fails the system step alone, exit 1" \
        '[ $code -eq 1 ] && in_log cell13 "Aktivierung: changed" && in_log cell13 "„System“"'
    verdict 13 "the commit hook is active" '[ -n "$(su - tester -c "git config --global --get core.hooksPath")" ]'
    return
}

cell_10() {
    local code
    at_terminal cell10 "$HOME_BIN setup"
    code=$?
    verdict 10 "a dry run at a terminal asks once for sudo" '[ $code -eq 0 ] && asked_once cell10'
    verdict T1 "the dry run shows the units in its diff and starts nothing" \
        'in_log cell10 "hal-journal-retention.timer" && ! systemctl cat "$RETENTION_TIMER" >/dev/null 2>&1'
    verdict 10 "it shows would change, with the diff" 'in_log cell10 "System: would change" && grep -q "^+++ " "$EVIDENCE/cell10.log"'
    return
}

cell_1() {
    local code
    at_terminal cell1 "$HOME_BIN setup --apply"
    code=$?
    verdict 1 "apply exits 0 after one prompt" '[ $code -eq 0 ] && asked_once cell1 && in_log cell1 "System: changed"'
    verdict 1 "the four files are root-owned, mode 644" '[ "$(stat -c "%U %a" "${SYSTEM_FILES[@]}" | sort -u)" = "root 644" ]'
    verdict 1 "every placeholder is filled" 'all_exist "${SYSTEM_FILES[@]}" && ! grep -lE "@[A-Z_]+@" "${SYSTEM_FILES[@]}"'
    verdict 1 "systemd-analyze verify passes" 'systemd-analyze verify cloudflared.service >"$EVIDENCE/verify.log" 2>&1'
    return
}

cell_9() {
    local code
    at_terminal cell9 "$HOME_BIN setup --apply"
    code=$?
    verdict 9 "a second apply: Ansible reports changed=0, System unchanged" \
        '[ $code -eq 0 ] && grep -qE "changed=0 " "$EVIDENCE/cell9.log" && in_log cell9 "System: unchanged"'
    verdict 9 "Ansible left nothing in the home" '[ ! -e /home/tester/.ansible ]'
    return
}

cell_s1() {  # the secrets step: verified with every fake secret in place; the dry run names the one taken away and writes nothing
    local code dropped
    verdict S1 "every secret in place: the secrets step is unchanged" 'in_log cell9 "Geheimnisse: unchanged"'
    dropped=$(plant_fake_secrets drop)
    without_terminal cell-s1 "$HOME_BIN setup"
    code=$?
    verdict S1 "a dry run names the missing secret $dropped and exits 0" \
        '[ $code -eq 0 ] && in_log cell-s1 "Geheimnisse: would change" && in_log cell-s1 "fehlt: $dropped"'
    plant_fake_secrets >/dev/null
    return
}

# The journal retention cells (T1-T5b; T6 is a unit test). They run after the cloudflared cells in
# the stage that has applied the playbook once, and T3 ends by taking its proof-only override away again.
retention_active_journal_survives() {
    local file
    file=$(find /run/log/journal /var/log/journal -name system.journal 2>/dev/null | head -n 1)
    [[ -n $file ]]
    return
}

# A planted file gets a sequence id of its own: journalctl orders two files of one id by sequence
# number, which would contradict their ages.
plant_archive() {  # plant_archive <directory> <age in seconds> <suffix>: a real archive, named as if its oldest entry were that old
    local directory=$1 age=$2 suffix=$3 microseconds name
    microseconds=$(( ($(date +%s) - age) * 1000000 ))
    if [[ $suffix == journal ]]; then
        name=$(printf 'system@%032x-%016x-%016x.journal' "$microseconds" 1 "$microseconds")
    else
        name=$(printf 'system@%016x-%016x.journal~' "$microseconds" 1)
    fi
    cp "$directory/planted-source" "$directory/$name"
    echo "$directory/$name"
    return
}

cell_t1() {
    verdict T1 "both units show 28d and daily in systemctl cat" \
        'systemctl cat "$RETENTION_SERVICE" "$RETENTION_TIMER" >"$EVIDENCE/t1-cat.txt" && grep -qF -- "--rotate --vacuum-time=28d" "$EVIDENCE/t1-cat.txt" && grep -qx "OnCalendar=daily" "$EVIDENCE/t1-cat.txt" && grep -qx "Persistent=true" "$EVIDENCE/t1-cat.txt"'
    verdict T1 "the timer is enabled and list-timers shows a next run" \
        'systemctl list-timers "$RETENTION_TIMER" --no-pager >"$EVIDENCE/t1-timers.txt"; [[ $(systemctl is-enabled "$RETENTION_TIMER") == enabled ]] && grep -q "$RETENTION_TIMER" "$EVIDENCE/t1-timers.txt" && ! grep -q " n/a " "$EVIDENCE/t1-timers.txt"'
    verdict T1 "the units are root-owned, mode 644" \
        '[ "$(stat -c "%U %a" "$UNIT_DIR/$RETENTION_SERVICE" "$UNIT_DIR/$RETENTION_TIMER" | sort -u)" = "root 644" ]'
    return
}

cell_t2() {
    local directory=/var/log/journal/$(cat /etc/machine-id) old kept disposed
    mkdir -p "$directory"
    logger -t proof-planted "a line for the planted archives"
    journalctl --rotate
    cp "$(ls -t "$directory"/system@*.journal | head -n 1)" "$directory/planted-source"
    old=$(plant_archive "$directory" $((28 * 86400 + 3600)) journal)
    kept=$(plant_archive "$directory" $((27 * 86400)) journal)
    disposed=$(plant_archive "$directory" $((29 * 86400)) disposed)
    rm "$directory/planted-source"
    ls -la --time-style=full-iso "$directory" >"$EVIDENCE/t2-before.txt"
    systemctl start "$RETENTION_SERVICE" >"$EVIDENCE/t2-run.log" 2>&1
    ls -la --time-style=full-iso "$directory" >"$EVIDENCE/t2-after.txt"
    journalctl --no-pager -u "$RETENTION_SERVICE" >"$EVIDENCE/t2-service.txt" 2>&1
    verdict T2 "one run deletes the archive at 28 d + 1 h" '[ ! -e "$old" ]'
    verdict T2 "one run deletes the .journal~ at 29 d" '[ ! -e "$disposed" ]'
    verdict T2 "it keeps the archive at 27 d and the active file" '[ -e "$kept" ] && retention_active_journal_survives'
    rm -f "$kept"
    return
}

seconds_since_epoch() {  # seconds_since_epoch <systemd timestamp>: fails on an empty or unparsable one
    local timestamp=$1
    [[ -n $timestamp ]] || return 1
    LC_ALL=C date -d "$timestamp" +%s
    return
}

wait_for_new_invocation() {  # wait_for_new_invocation <old id>: the service's new, non-empty id, or failure after 70 s
    local old=$1 current deadline=$((SECONDS + 70))
    while ((SECONDS < deadline)); do
        current=$(systemctl show "$RETENTION_SERVICE" -p InvocationID --value) || return 1
        if [[ -n $current && $current != "$old" ]]; then
            echo "$current"
            return 0
        fi
        sleep 1
    done
    return 1
}

cell_t4() {
    local before restarted invocation exit_timestamp activated
    before=$(systemctl show "$RETENTION_SERVICE" -p InvocationID --value) || before=unobserved
    touch -d '2 days ago' "/var/lib/systemd/timers/stamp-$RETENTION_TIMER"
    restarted=$(date +%s)
    systemctl restart "$RETENTION_TIMER"
    invocation=$(wait_for_new_invocation "$before")
    exit_timestamp=$(systemctl show "$RETENTION_SERVICE" -p InactiveExitTimestamp --value) || exit_timestamp=
    activated=$(seconds_since_epoch "$exit_timestamp")
    verdict T4 "two days after its last run the service runs within a minute of the timer's start" \
        '[[ $before != unobserved && -n $invocation && -n $activated ]] && ((activated >= restarted && activated - restarted <= 60))'
    return
}

cell_t5() {
    local first second
    cp "$UNIT_DIR/$RETENTION_SERVICE" "$UNIT_DIR/$RETENTION_TIMER" /proof/
    systemctl disable --now "$RETENTION_TIMER" >/dev/null 2>&1
    rm -f "$UNIT_DIR/$RETENTION_SERVICE" "$UNIT_DIR/$RETENTION_TIMER"
    systemctl daemon-reload
    cp "/proof/$RETENTION_SERVICE" "/proof/$RETENTION_TIMER" "$UNIT_DIR/"
    verdict T5 "the interrupted state is set: units on disk, the timer neither enabled nor active" \
        '[ "$(systemctl is-enabled $RETENTION_TIMER 2>&1)" != enabled ] && [ "$(systemctl is-active $RETENTION_TIMER)" != active ]'
    at_terminal t5-first "$HOME_BIN setup --apply"
    first=$?
    verdict T5 "apply converges: the timer is enabled and active" \
        '[ $first -eq 0 ] && [ "$(systemctl is-enabled $RETENTION_TIMER)" = enabled ] && [ "$(systemctl is-active $RETENTION_TIMER)" = active ]'
    at_terminal t5-second "$HOME_BIN setup --apply"
    second=$?
    verdict T5 "a second run reports changed=0" '[ $second -eq 0 ] && grep -qE "changed=0 " "$EVIDENCE/t5-second.log" && in_log t5-second "System: unchanged"'
    return
}

cell_t5b() {  # the timer is enabled and the service unit on disk is newer than the one systemd loaded
    local first second
    cp "$UNIT_DIR/$RETENTION_SERVICE" "/proof/t5b-original.service"
    sed -i 's/--vacuum-time=28d/--vacuum-time=5d/' "$UNIT_DIR/$RETENTION_SERVICE"
    systemctl daemon-reload
    cp "/proof/t5b-original.service" "$UNIT_DIR/$RETENTION_SERVICE"
    verdict T5b "the stale state is set: the timer is enabled, systemd runs 5d and needs a reload" \
        '[ "$(systemctl is-enabled $RETENTION_TIMER)" = enabled ] && [ "$(systemctl show $RETENTION_SERVICE -p NeedDaemonReload --value)" = yes ] && systemctl show $RETENTION_SERVICE -p ExecStart --value | grep -qF -- "--vacuum-time=5d"'
    at_terminal t5b-first "$HOME_BIN setup --apply"
    first=$?
    verdict T5b "apply reloads without changing a file: systemd needs no reload and runs 28d" \
        '[ $first -eq 0 ] && grep -qE "changed=0 " "$EVIDENCE/t5b-first.log" && [ "$(systemctl show $RETENTION_SERVICE -p NeedDaemonReload --value)" = no ] && systemctl show $RETENTION_SERVICE -p ExecStart --value | grep -qF -- "--vacuum-time=28d"'
    at_terminal t5b-second "$HOME_BIN setup --apply"
    second=$?
    verdict T5b "a repeat reports changed=0" '[ $second -eq 0 ] && grep -qE "changed=0 " "$EVIDENCE/t5b-second.log" && in_log t5b-second "System: unchanged"'
    return
}

# Prints present or absent when the journal answered, and fails when the observation itself failed. Reading
# writes nothing, so the journal stays quiet. systemd 255 exits 1 and prints nothing when no entry matches,
# so absence is that silent exit 1 with the journal's header and boot list still readable; any other
# outcome is a failed observation, never absence.
observe_journal_line() {  # observe_journal_line <text>
    local text=$1 output status=0
    journalctl --header >/dev/null && journalctl --list-boots >/dev/null || return 1
    output=$(journalctl --no-pager -o cat --grep "$text" 2>&1) || status=$?
    if ((status == 0)) && grep -qF -- "$text" <<<"$output"; then
        echo present
    elif ((status == 1)) && [[ -z $output ]]; then
        echo absent
    else
        return 1
    fi
    return 0
}

wait_until_journal_line_is_there() {  # wait_until_journal_line_is_there <text>: present within 30 s, by a successful query
    local text=$1 deadline=$((SECONDS + 30))
    while ((SECONDS < deadline)); do
        [[ $(observe_journal_line "$text") == present ]] && return 0
        sleep 1
    done
    return 1
}

wait_until_journal_line_is_gone() {  # wait_until_journal_line_is_gone <text>: absent within 190 s, by a successful query
    local text=$1 deadline=$((SECONDS + 190))
    while ((SECONDS < deadline)); do
        [[ $(observe_journal_line "$text") == absent ]] && return 0
        sleep 5
    done
    return 1
}

cell_t3() {
    local marker=retention-marker-$$ override_service="$UNIT_DIR/$RETENTION_SERVICE.d" override_timer="$UNIT_DIR/$RETENTION_TIMER.d"
    mkdir -p "$override_service" "$override_timer"
    printf '%s\n' '[Service]' 'ExecStart=' 'ExecStart=/usr/bin/journalctl --rotate --vacuum-time=2min' >"$override_service/proof.conf"
    printf '%s\n' '[Timer]' 'OnCalendar=' 'OnCalendar=minutely' >"$override_timer/proof.conf"
    systemctl daemon-reload && systemctl restart "$RETENTION_TIMER"
    logger -t proof-marker "$marker"
    verdict T3 "the marker line is in the journal before the run" 'wait_until_journal_line_is_there "$marker"'
    verdict T3 "a quiet journal loses the marker within 3 min 10 s, seen by a query that succeeded" \
        'wait_until_journal_line_is_gone "$marker"'
    rm -rf "$override_service" "$override_timer"
    systemctl daemon-reload
    systemctl restart "$RETENTION_TIMER"
    return
}

cells_retention() {
    cell_t1
    cell_t4
    cell_t2
    cell_t5
    cell_t5b
    cell_t3
    return
}

cells_polkit() {
    : >"$EVIDENCE/polkit.log"
    systemctl start cloudflared || true
    verdict 3 "tester may stop cloudflared" 'as_tester systemctl --no-ask-password stop cloudflared'
    local verb
    for verb in start restart kill; do
        systemctl start cloudflared || true  # kill needs a unit systemd still holds, and stop (cell 3) let it go
        verdict 4 "tester may not $verb cloudflared" 'refused cloudflared as_tester systemctl --no-ask-password "$verb" cloudflared'
    done
    verdict 5 "tester may not stop another unit" 'refused proof-other.service as_tester systemctl --no-ask-password stop proof-other.service'
    systemctl start cloudflared || true
    verdict 6 "another user may not stop cloudflared" 'refused cloudflared runuser -u bystander -- systemctl --no-ask-password stop cloudflared'
    systemctl stop cloudflared
    return
}

marker_on() {  # a failure is a cell 7 line, never an exit: the reboot stages must still run
    local message
    if message=$(as_tester mkdir -p "$(dirname "$MARKER")" && as_tester touch "$MARKER" && systemctl enable cloudflared 2>&1); then
        echo "cell 7: held: the marker is set and cloudflared is enabled"
    else
        echo "cell 7: FAILED: the marker could not be set and cloudflared enabled: $message"
    fi
    return 0
}

marker_off() {
    rm -f "$MARKER"
    return
}

this_boot_journal() {  # the journal is shared across `docker restart` (one boot id), so a boot is told by PID 1's start
    local booted_at
    booted_at=$(date -d "$(systemctl show --value --property UserspaceTimestamp)" '+%F %T')
    journalctl --unit cloudflared --since "$booted_at" --no-pager
    return
}

boot_diagnosis() {  # boot_diagnosis <log>: what systemd made of cloudflared at this boot, for a cell that did not hold
    local log=$1
    {
        ls -la "$(dirname "$MARKER")"
        systemctl is-enabled cloudflared
        systemctl status cloudflared --no-pager
        this_boot_journal
    } >"$EVIDENCE/$log.txt" 2>&1
    return 0
}

# A cell counts only on a line systemd wrote at that boot: an inactive unit is also what one never looked at.
cell_7() {
    boot_diagnosis boot-7
    systemctl show cloudflared -p ActiveState >"$EVIDENCE/cell7.txt"
    verdict 7 "with the marker the unit is condition-skipped after a reboot" \
        'grep -qx ActiveState=inactive "$EVIDENCE/cell7.txt" && grep -q "was skipped because of an unmet condition check (ConditionPathExists=!" "$EVIDENCE/boot-7.txt"'
    return
}

cell_8() {
    boot_diagnosis boot-8
    verdict 8 "without the marker the unit tries to start after a reboot" \
        'grep -q "Starting cloudflared.service" "$EVIDENCE/boot-8.txt" && ! grep -q "unmet condition check" "$EVIDENCE/boot-8.txt"'
    return
}

# The agents' account (#331, matrix A1; its cells are named A1-<n> so they never meet the cells of #40). Cell 10, a revert,
# is not driven: it keeps the user and its home, and no revert exists to run.
AGENT_HOME=/home/agent
DOCKER_SOCKET=/run/docker.sock
OPERATORS_SKIP="skipped: the operator's step"

run_as() {  # run_as <user> <log> <command...>: with that user's own HOME and runtime directory, no terminal, no sudo
    local user=$1 log=$2
    shift 2
    timeout 1800 runuser -u "$user" -- env -i HOME="/home/$user" XDG_RUNTIME_DIR="/run/user/$(id -u "$user")" LC_ALL=C.UTF-8 \
        PATH="/usr/local/bin:/usr/bin:/bin:/home/$user/.local/bin" "$@" </dev/null >"$EVIDENCE/$log.log" 2>&1
    return
}

agent_world() {  # everything the play may write about the account, as one comparable text
    getent passwd agent
    getent group agent
    getent shadow agent
    id -Gn agent
    stat -c '%U:%G %a' "$AGENT_HOME"
    cat /etc/subuid /etc/subgid /etc/cron.deny 2>&1
    ls /var/lib/systemd/linger 2>&1
    snapshot
    return 0
}

agent_is_dormant() {  # one account, one regular id for uid and gid, its own group, locked, nologin, a closed home, no subids, no linger, denied cron and nothing else denied
    [[ $(id -u agent) -ge 1000 && $(id -u agent) == "$(id -g agent)" && $(getent group agent | cut -d: -f3) == "$(id -g agent)" ]] &&
        [[ $(getent passwd agent | cut -d: -f7) == /usr/sbin/nologin ]] &&
        [[ $(id -Gn agent) == agent ]] &&
        [[ $(getent shadow agent | cut -d: -f2 | cut -c1) == '!' ]] &&
        [[ $(stat -c '%U:%G %a' "$AGENT_HOME") == 'agent:agent 700' ]] &&
        ! grep -qE '^agent:' /etc/subuid /etc/subgid &&
        [[ $(cat /etc/cron.deny) == agent ]] &&
        [[ ! -e /var/lib/systemd/linger/agent ]]
    return
}

provide_docker_socket() {  # the Docker socket as the engine ships it: root:docker 660, with the operator in the group
    groupadd --force docker
    usermod --append --groups docker tester
    printf '%s\n' '[Socket]' "ListenStream=$DOCKER_SOCKET" 'SocketMode=0660' 'SocketUser=root' 'SocketGroup=docker' \
        '[Install]' 'WantedBy=sockets.target' >"$UNIT_DIR/proof-docker.socket"
    printf '%s\n' '[Service]' 'ExecStart=/bin/sleep infinity' >"$UNIT_DIR/proof-docker.service"
    systemctl daemon-reload
    systemctl enable --now proof-docker.socket >/dev/null 2>&1
    return
}

connects_to_docker() {  # connects_to_docker <user>
    run_as "$1" "docker-$1" python3 -c "import socket; socket.socket(socket.AF_UNIX).connect('$DOCKER_SOCKET')"
    return
}

agent_is_refused() {  # what the account may not reach, each against a control that shows the thing is there and reachable by its owner
    local secret=/home/tester/.secrets/proof-a1
    [[ $(stat -c %a /home/tester) -le 750 ]] || return 1
    as_tester mkdir -p "$(dirname "$secret")" && as_tester touch "$secret" && as_tester cat "$secret" || return 1
    connects_to_docker tester || return 1
    ! run_as agent a1-ls ls /home/tester &&
        ! run_as agent a1-cat cat "$secret" &&
        ! run_as agent a1-sudo sudo -n true &&
        ! connects_to_docker agent &&
        ! su - agent -c true >"$EVIDENCE/a1-su.log" 2>&1 &&
        grep -q 'not available' "$EVIDENCE/a1-su.log"
    return
}

cell_a1_1() {
    verdict A1-1 "the apply created agent: own group, locked, nologin, home 0700, no subids, cron.deny, no linger" agent_is_dormant
    A1_WORLD_AFTER_CREATION=$(agent_world)
    return
}

cell_a1_2() {  # runs after the second apply of cell 9
    verdict A1-2 "a second apply changes nothing of the account" \
        '[ "$A1_WORLD_AFTER_CREATION" = "$(agent_world)" ] && grep -qE "changed=0 " "$EVIDENCE/cell9.log"'
    return
}

refuses_foreign_shape() {  # refuses_foreign_shape <name> <make> <undo> [reason] [dry]: the play fails with exit 1 and nothing, not even cloudflared's files, moves
    local name=$1 make=$2 undo=$3 reason=${4:-nothing was written} before after code dry_code dry_after
    eval "$make"
    before=$(agent_world)
    at_terminal "a1-3-$name" "$HOME_BIN setup --apply"
    code=$?
    after=$(agent_world)
    if [[ ${5:-} == dry ]]; then
        at_terminal "a1-3-$name-dry" "$HOME_BIN setup"
        dry_code=$?
        dry_after=$(agent_world)
    fi
    eval "$undo"
    verdict A1-3 "$name: the apply refuses with exit 1 and nothing was written" \
        '[ $code -eq 1 ] && in_log "a1-3-$name" "$reason" && ! in_log "a1-3-$name" "$OPERATORS_SKIP" && [ "$before" = "$after" ]'
    if [[ ${5:-} == dry ]]; then
        verdict A1-3 "$name: the dry run refuses with exit 1 and nothing was written" \
            '[ $dry_code -eq 1 ] && in_log "a1-3-$name-dry" "$reason" && [ "$before" = "$dry_after" ]'
    fi
    verdict A1-3 "$name: undone, the account is dormant again" agent_is_dormant
    return
}

move_entry() {  # move_entry <file> <name> <first|last>: the entry of that name to the top or bottom of the file, in place
    local file=$1 name=$2 where=$3 rest
    rest=$(grep -v "^$name:" "$file")
    if [[ $where == first ]]; then { grep "^$name:" "$file"; echo "$rest"; } >"$file.new"; else { echo "$rest"; grep "^$name:" "$file"; } >"$file.new"; fi
    cat "$file.new" >"$file"
    rm "$file.new"
    return
}

set_passwd_field() {  # set_passwd_field <name> <field number> <value>
    awk -F: -v OFS=: -v name="$1" -v field="$2" -v value="$3" '$1 == name { $field = value } 1' /etc/passwd >/etc/passwd.new
    cat /etc/passwd.new >/etc/passwd
    rm /etc/passwd.new
    return
}

drop_entry() {  # drop_entry <file> <name or id>: the entry keyed by it
    sed -i "/^$2:/d" "$1"
    return
}

remove_account() {  # the account, its group and its home, as a cell that needs the account absent starts
    userdel --remove agent
    groupdel agent 2>/dev/null
    return 0
}

recreate_account() {
    at_terminal a1-3-recreate "$HOME_BIN setup --apply"
    return
}

lowest_free_id() {  # lowest_free_id [skip]: the lowest id from 1000 up that no user and no group has, other than the one named
    local id
    for ((id = 1000; id < 60000; id++)); do
        [[ $id == "${1:-}" ]] && continue
        getent passwd "$id" >/dev/null || getent group "$id" >/dev/null || { echo "$id"; return 0; }
    done
    return 1
}

free_system_id() {  # the highest id below 1000 that no user and no group has
    local id
    for ((id = 999; id > 100; id--)); do
        getent passwd "$id" >/dev/null || getent group "$id" >/dev/null || { echo "$id"; return 0; }
    done
    return 1
}

create_account_with_ids() {  # create_account_with_ids <uid> <gid>: the dormant account by hand, with those ids
    groupadd --gid "$2" agent
    useradd --create-home --home-dir "$AGENT_HOME" --gid "$2" --shell /usr/sbin/nologin --uid "$1" -K SUB_UID_COUNT=0 -K SUB_GID_COUNT=0 agent
    return
}

cell_a1_3() {
    AGENT_ID=$(id -u agent)
    AGENT_GID=$(id -g agent)
    refuses_foreign_shape "an extra group" 'usermod --append --groups bystander agent' 'gpasswd --delete agent bystander'
    refuses_foreign_shape "a subuid entry" 'echo agent:100000:65536 >>/etc/subuid' 'drop_entry /etc/subuid agent'
    refuses_foreign_shape "the shell bash" 'usermod --shell /bin/bash agent' 'usermod --shell /usr/sbin/nologin agent'
    refuses_foreign_shape "linger" 'loginctl enable-linger agent' 'loginctl disable-linger agent'
    refuses_foreign_shape "a usable password" 'passwd --delete agent >/dev/null' 'usermod --lock agent'
    refuses_foreign_shape "a subgid entry" 'echo agent:100000:65536 >>/etc/subgid' 'drop_entry /etc/subgid agent'
    refuses_foreign_shape "a numeric subuid entry for its uid" 'echo "$AGENT_ID:100000:65536" >>/etc/subuid' 'drop_entry /etc/subuid $AGENT_ID' \
        "a subuid or subgid entry"
    refuses_foreign_shape "a numeric subgid entry for its uid" 'echo "$AGENT_ID:100000:65536" >>/etc/subgid' 'drop_entry /etc/subgid $AGENT_ID' \
        "a subuid or subgid entry"
    cell_a1_3_homes
    cell_a1_3_shared_ids
    cell_a1_3_account_absent
    cell_a1_3_cron_files
    return
}

cell_a1_3_homes() {  # an existing account whose home is not its own; the dry run refuses too
    refuses_foreign_shape "another home" 'usermod --home /srv/agent-elsewhere agent' "usermod --home $AGENT_HOME agent" "a home other than" dry
    refuses_foreign_shape "a home that is a link" "mv $AGENT_HOME $AGENT_HOME.real && ln -s $AGENT_HOME.real $AGENT_HOME" \
        "rm $AGENT_HOME && mv $AGENT_HOME.real $AGENT_HOME" "a home that is a link" dry
    refuses_foreign_shape "a home that is a file of its own" "mv $AGENT_HOME $AGENT_HOME.real && touch $AGENT_HOME && chown agent:agent $AGENT_HOME" \
        "rm $AGENT_HOME && mv $AGENT_HOME.real $AGENT_HOME" "a home that is a link" dry
    refuses_foreign_shape "a home owned by root" "chown root:root $AGENT_HOME" "chown agent:agent $AGENT_HOME" "a home that is a link" dry
    refuses_foreign_shape "a line in cron.allow" 'echo agent >/etc/cron.allow' 'rm -f /etc/cron.allow'
    return
}

cell_a1_3_shared_ids() {  # a uid or gid the account shares, with the shared name before and after the agent's; `hal setup` itself never guesses the role
    refuses_foreign_shape "the uid of bystander, agent listed after" 'set_passwd_field agent 3 $(id -u bystander)' 'set_passwd_field agent 3 $AGENT_ID' \
        "a uid shared with"
    refuses_foreign_shape "the uid of bystander, agent listed first" 'set_passwd_field agent 3 $(id -u bystander); move_entry /etc/passwd agent first' \
        'set_passwd_field agent 3 $AGENT_ID; move_entry /etc/passwd agent last' "a uid shared with"
    refuses_foreign_shape "the uid of the running user, agent listed after" 'set_passwd_field agent 3 $(id -u tester)' 'set_passwd_field agent 3 $AGENT_ID' \
        "must have a uid of its own"
    refuses_foreign_shape "the uid of the running user, agent listed first" 'set_passwd_field agent 3 $(id -u tester); move_entry /etc/passwd agent first' \
        'set_passwd_field agent 3 $AGENT_ID; move_entry /etc/passwd agent last' "must have a uid of its own"
    refuses_foreign_shape "its gid in another group, listed after" 'groupadd --non-unique --gid $AGENT_GID sharer' 'drop_entry /etc/group sharer; drop_entry /etc/gshadow sharer' "a gid shared with sharer"
    refuses_foreign_shape "its gid in another group, listed first" 'groupadd --non-unique --gid $AGENT_GID sharer; move_entry /etc/group sharer first' \
        'drop_entry /etc/group sharer; drop_entry /etc/gshadow sharer' "a gid shared with sharer"
    refuses_foreign_shape "an id below 1000 for uid and gid" 'remove_account; create_account_with_ids $(free_system_id) $(free_system_id)' \
        'remove_account; recreate_account' "an id that is not one uid and gid from 1000 up" dry
    refuses_foreign_shape "a uid and a gid that differ" 'remove_account; create_account_with_ids $(lowest_free_id) $(lowest_free_id $(lowest_free_id))' \
        'remove_account; recreate_account' "an id that is not one uid and gid from 1000 up" dry
    refuses_foreign_shape "a primary gid that is not the group agent's" 'set_passwd_field agent 4 $(id -g bystander)' 'set_passwd_field agent 4 $AGENT_GID' \
        "a primary group other than"
    return
}

cell_a1_3_account_absent() {  # no account yet: what is in its way is refused before anything is written; ids and subid files of a new one
    refuses_foreign_shape "a home path already taken by a directory" "remove_account; mkdir $AGENT_HOME" "rmdir $AGENT_HOME; recreate_account" \
        "a home path that is already taken" dry
    refuses_foreign_shape "a home path already taken by a file" "remove_account; touch $AGENT_HOME" "rm $AGENT_HOME; recreate_account" \
        "a home path that is already taken" dry
    refuses_foreign_shape "a lone group agent" 'remove_account; groupadd agent' 'groupdel agent; recreate_account' "a group agent without the account (sudo groupdel agent removes it)"
    cell_a1_3_failed_useradd
    cell_a1_3_ids_of_the_new_account
    cell_a1_3_without_subid_files
    return
}

cell_a1_3_failed_useradd() {  # useradd cannot lock /etc/passwd: the apply fails, no group of that name is left, the next apply converges
    local code
    remove_account
    echo $$ >/etc/passwd.lock
    at_terminal a1-3-useradd-fails "$HOME_BIN setup --apply"
    code=$?
    rm /etc/passwd.lock
    verdict A1-3 "useradd fails on a held lock: the apply fails and leaves no group agent" '[ $code -ne 0 ] && ! getent group agent >/dev/null'
    recreate_account
    verdict A1-3 "the lock gone, the next apply creates the account" 'agent_is_dormant'
    return
}

cell_a1_3_ids_of_the_new_account() {  # a numeric subuid or subgid entry for the lowest free id: the new account takes the next id, one for uid and gid
    local file skipped expected code
    for file in /etc/subuid /etc/subgid; do
        remove_account
        skipped=$(lowest_free_id)
        echo "$skipped:100000:65536" >>"$file"
        expected=$(lowest_free_id "$skipped")
        at_terminal a1-3-new-ids "$HOME_BIN setup --apply"
        code=$?
        verdict A1-3 "a numeric entry in $file for the lowest free id: exit 0, the account gets the next id for uid and gid" \
            '[ $code -eq 0 ] && [[ $(id -u agent) == "$expected" && $(id -g agent) == "$expected" ]] && agent_is_dormant'
        drop_entry "$file" "$skipped"
    done
    remove_account
    recreate_account
    verdict A1-3 "the entries gone, the apply creates the account at the lowest free id again" '[[ $(id -u agent) == "$AGENT_ID" ]] && agent_is_dormant'
    return
}

cell_a1_3_without_subid_files() {  # the subordinate id files do not exist: an existing and a new account both converge, and no file appears
    local code
    mv /etc/subuid /etc/subuid.proof
    mv /etc/subgid /etc/subgid.proof
    at_terminal a1-3-no-subid-existing "$HOME_BIN setup --apply"
    code=$?
    verdict A1-3 "no subuid and subgid file, the account exists: exit 0, changed=0, dormant" \
        '[ $code -eq 0 ] && in_log a1-3-no-subid-existing "changed=0 " && agent_is_dormant'
    remove_account
    at_terminal a1-3-no-subid-new "$HOME_BIN setup --apply"
    code=$?
    verdict A1-3 "no subuid and subgid file, a new account: exit 0, dormant, and no file was created" \
        '[ $code -eq 0 ] && agent_is_dormant && [ ! -e /etc/subuid ] && [ ! -e /etc/subgid ]'
    mv /etc/subuid.proof /etc/subuid
    mv /etc/subgid.proof /etc/subgid
    return
}

keep_the_cron_deny_of() {  # keep_the_cron_deny_of <entry> <owner:group> <mode>: the file the operator already has
    printf '%s\n' "$1" >/etc/cron.deny
    chown "$2" /etc/cron.deny
    chmod "$3" /etc/cron.deny
    return
}

cron_deny_is() {  # cron_deny_is <entries> <owner:group> <mode>
    [[ $(tr '\n' ' ' </etc/cron.deny) == "$1 " ]] && [[ $(stat -c '%U:%G' /etc/cron.deny) == "$2" ]] && [[ $(stat -c %a /etc/cron.deny) == "$3" ]]
    return
}

cell_a1_3_cron_files() {  # the states of the files; the proof image has no cron, so crontab itself is not driven
    local code
    keep_the_cron_deny_of bob root:bystander 640
    at_terminal a1-3-cron-deny "$HOME_BIN setup --apply"
    code=$?
    verdict A1-3 "an existing cron.deny keeps owner and mode and gains the agent line" \
        '[ $code -eq 0 ] && cron_deny_is "bob agent" root:bystander 640'
    keep_the_cron_deny_of bob root:root 644
    echo alice >/etc/cron.allow
    at_terminal a1-3-cron-allow "$HOME_BIN setup --apply"
    code=$?
    verdict A1-3 "a cron.allow without the agent denies it already: exit 0, cron.deny left alone" \
        '[ $code -eq 0 ] && cron_deny_is bob root:root 644 && [[ $(cat /etc/cron.allow) == alice ]]'
    rm -f /etc/cron.allow
    keep_the_cron_deny_of agent root:root 644
    return
}

install_wheel_for() {  # install_wheel_for <user>: the packaged `home`, in that user's own home
    run_as "$1" "install-$1" uv tool install /proof/*.whl
    return
}

A1_NEXT_AS_AGENT="1. hal setup --apply (beliebiges Terminal) — erwartet: quality, Engines, Hook-Dateien, Aktivierung: changed
   warum: ohne den Hook prüft kein Commit auf Geheimnisse
Außerdem zum Einsortieren: ~/README.md fehlt. Das macht ein Agent: sag „räum home auf“."

cell_a1_4_and_5() {
    local dry apply next
    install_wheel_for agent
    run_as agent a1-4-dry "$AGENT_HOME/.local/bin/home" setup
    dry=$?
    verdict A1-4 "as agent the dry run exits 0, shows the per-user steps and skips the operator's" \
        '[ $dry -eq 0 ] && in_log a1-4-dry "quality: would change" && in_log a1-4-dry "Aktivierung: would change" &&
         in_log a1-4-dry "User units: skipped" && in_log a1-4-dry "Ansible: skipped" && in_log a1-4-dry "System: skipped"'
    run_as agent a1-4-next "$AGENT_HOME/.local/bin/home" next
    next=$?
    verdict A1-4 "home next exits 0 and lists exactly the per-user step and the bookkeeping line, none of the operator's steps" \
        '[ $next -eq 0 ] && [[ $(<"$EVIDENCE/a1-4-next.log") == "$A1_NEXT_AS_AGENT" ]]'
    run_as agent a1-5-apply "$AGENT_HOME/.local/bin/home" setup --apply
    apply=$?
    verdict A1-5 "as agent --apply exits 0 with the same skips, no sudo prompt, no user unit linked" \
        '[ $apply -eq 0 ] && in_log a1-5-apply "Aktivierung: changed" && in_log a1-5-apply "System: skipped" &&
         ! grep -qi "password" "$EVIDENCE/a1-5-apply.log" && [ ! -e "$AGENT_HOME/.config/systemd/user" ]'
    verdict A1-5 "the account is still dormant after the agent's own apply" agent_is_dormant
    return
}

cell_a1_6() {
    verdict A1-6 "the operator's dry run and apply print no skip and still run the user units and the system step" \
        '! grep -q "$OPERATORS_SKIP" "$EVIDENCE"/cell1.log "$EVIDENCE"/cell9.log "$EVIDENCE"/cell10.log &&
         in_log cell9 "User units: " && in_log cell9 "System: unchanged" && in_log cell10 "System: would change"'
    return
}

cell_a1_7() {
    local dry apply
    loginctl enable-linger bystander
    timeout 60 bash -c 'until systemctl is-active --quiet "user@$(id -u bystander).service"; do sleep 1; done'
    install -d -o bystander -g bystander /home/bystander/git/edge
    cp -r /proof/systemd /proof/tunnel /home/bystander/git/edge/
    chown -R bystander:bystander /home/bystander/git
    install_wheel_for bystander
    run_as bystander a1-7-dry /home/bystander/.local/bin/home setup
    dry=$?
    verdict A1-7 "a bystander's dry run behaves as today: not checked, exit 0, nothing skipped" \
        '[ $dry -eq 0 ] && in_log a1-7-dry "System: not checked (needs sudo at a terminal)" && ! grep -q "$OPERATORS_SKIP" "$EVIDENCE/a1-7-dry.log"'
    run_as bystander a1-7-apply /home/bystander/.local/bin/home setup --apply
    apply=$?
    verdict A1-7 "a bystander's --apply fails the system step, exit 1" \
        '[ $apply -eq 1 ] && in_log a1-7-apply "„System“" && in_log a1-7-apply "sudo at a terminal" && ! grep -q "$OPERATORS_SKIP" "$EVIDENCE/a1-7-apply.log"'
    return
}

cell_a1_8() {
    verdict A1-8 "agent may not read ~tester or its secrets, use sudo or the Docker socket, and has no login shell" agent_is_refused
    return
}

cell_a1_9() {  # after a reboot
    provide_docker_socket
    verdict A1-9 "after a reboot the account is still dormant" agent_is_dormant
    verdict A1-9 "after a reboot agent is still refused" agent_is_refused
    return
}

cells_agent() {
    cell_a1_3
    provide_docker_socket
    cell_a1_4_and_5
    cell_a1_6
    cell_a1_7
    cell_a1_8
    return
}

case "${1:?stage}" in
    install) install_home ;;
    fresh) cell_11; cell_12; cell_13; cell_10; cell_1; cell_a1_1; cell_9; cell_a1_2; cell_s1; cells_agent; cells_retention; cells_polkit; marker_on ;;
    after-marker-reboot) cell_7; cell_a1_9; marker_off ;;
    after-unmarked-reboot) cell_8 ;;
    prepared) prepare_today; cell_2 ;;
    *) echo "unknown stage $1" >&2; exit 2 ;;
esac
