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

install_home() {
    su - tester -c "uv tool install /proof/*.whl" >"$EVIDENCE/install.log" 2>&1
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

# The journal retention cells (#279, #167 T1-T5; T6 is a unit test). They run after the cloudflared cells in the
# stage that has applied the playbook once, and T3 ends by taking its proof-only override away again.
retention_active_journal_survives() {
    local file
    file=$(find /run/log/journal /var/log/journal -name system.journal 2>/dev/null | head -n 1)
    [[ -n $file ]]
    return
}

# A planted file gets a sequence id of its own: journalctl orders two files of one id by sequence number, which would
# contradict their ages.
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

cell_t4() {
    local before
    before=$(systemctl show "$RETENTION_SERVICE" -p InvocationID --value)
    touch -d '2 days ago' "/var/lib/systemd/timers/stamp-$RETENTION_TIMER"
    systemctl restart "$RETENTION_TIMER"
    verdict T4 "two days after its last run the service runs within a minute of the timer's start" \
        'timeout 70 bash -c "until [[ \$(systemctl show $RETENTION_SERVICE -p InvocationID --value) != \"$before\" ]]; do sleep 1; done"'
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

cell_t3() {
    local marker=retention-marker-$$ override_service="$UNIT_DIR/$RETENTION_SERVICE.d" override_timer="$UNIT_DIR/$RETENTION_TIMER.d"
    mkdir -p "$override_service" "$override_timer"
    printf '%s\n' '[Service]' 'ExecStart=' 'ExecStart=/usr/bin/journalctl --rotate --vacuum-time=2min' >"$override_service/proof.conf"
    printf '%s\n' '[Timer]' 'OnCalendar=' 'OnCalendar=minutely' >"$override_timer/proof.conf"
    systemctl daemon-reload
    systemctl restart "$RETENTION_TIMER"
    logger -t proof-marker "$marker"
    verdict T3 "the marker line is in the journal before the run" 'journalctl --no-pager --grep "$marker" | grep -q "$marker"'
    verdict T3 "a quiet journal loses the marker within 3 min 10 s" \
        'timeout 190 bash -c "while journalctl --no-pager --grep \"$marker\" | grep -q \"$marker\"; do sleep 5; done"'
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

case "${1:?stage}" in
    install) install_home ;;
    fresh) cell_11; cell_12; cell_13; cell_10; cell_1; cell_9; cells_retention; cells_polkit; marker_on ;;
    after-marker-reboot) cell_7; marker_off ;;
    after-unmarked-reboot) cell_8 ;;
    prepared) prepare_today; cell_2 ;;
    *) echo "unknown stage $1" >&2; exit 2 ;;
esac
