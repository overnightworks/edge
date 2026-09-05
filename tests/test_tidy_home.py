from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import tidy_home
from tidy_home import Action, Finding, Layout, Limits, Options, Verdict

NOW = datetime(2026, 9, 5, 12, 0)
LIMITS = Limits()

README_FIXTURE = """
# Home

`git/atelier-2`, `git/songmaker` under `git/`.
`.secrets/gitlab` and `.secrets/deepseek.env` live in `.secrets`.
`.config/gh` holds credentials. `~/.cloudflared` is a symlink.
`npm cache clean` is a command, not a path. `atelier2-*` is a glob.
`<repo>-worktrees/` is a placeholder.
"""


def touch(path: Path, age: timedelta, content: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    stamp = (NOW - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def no_tools(_argv):
    return None


def fake_docker(*, containers: str = "", system_df: str = "", dangling_images: str | None = ""):
    """An installed docker whose three listings are exactly what the caller names; None is a failed listing."""

    def run(argv):
        if argv[:2] == ["docker", "info"]:
            return "28.0\n"
        if argv[:3] == ["docker", "container", "ls"]:
            return containers
        if argv[:3] == ["docker", "system", "df"]:
            return system_df
        if argv[:3] == ["docker", "image", "ls"]:
            return dangling_images
        return ""

    return run


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return Layout(tmp_path)


def subjects(findings: list[Finding]) -> set[str]:
    return {f.subject for f in findings}


def with_readme(layout: Layout, text: str = README_FIXTURE) -> Layout:
    touch(layout.readme, timedelta(0), text)
    return layout


# --- clean rules ------------------------------------------------------------


def test_pasted_images_older_than_a_week_are_deletions_in_every_watched_dir(layout: Layout) -> None:
    old_home = touch(layout.home / "Eingefügtes Bild (3).png", timedelta(days=8))
    old_git = touch(layout.git / "Eingefügtes Bild.png", timedelta(days=30))
    touch(layout.home / "Eingefügtes Bild (4).png", timedelta(days=2))
    touch(layout.home / "Dokumente" / "cv" / "Eingefügtes Bild.png", timedelta(days=60))

    findings = tidy_home.pasted_images(layout, NOW, LIMITS)

    assert subjects(findings) == {str(old_home), str(old_git)}
    assert all(f.action is Action.DELETE and f.verdict is Verdict.DELETE for f in findings)


def test_pasted_image_age_limit_is_configurable(layout: Layout) -> None:
    image = touch(layout.home / "Eingefügtes Bild.png", timedelta(days=8))

    assert tidy_home.pasted_images(layout, NOW, Limits(pasted_image_days=30)) == []
    assert subjects(tidy_home.pasted_images(layout, NOW, LIMITS)) == {str(image)}


def trash_item(trash: Path, name: str, deleted_on: str, directory: bool = False) -> tuple[Path, Path]:
    item = trash / "files" / name
    if directory:
        touch(item / "inner.txt", timedelta(0))
    else:
        touch(item, timedelta(0))
    info = touch(trash / "info" / f"{name}.trashinfo", timedelta(0), f"[Trash Info]\nDeletionDate={deleted_on}T10:00:00\n")
    return item, info


def test_trash_items_past_the_age_limit_are_deleted_with_their_info(layout: Layout) -> None:
    trash = layout.home / ".local" / "share" / "Trash"
    old_file, old_info = trash_item(trash, "old.txt", "2026-07-01")
    old_dir, old_dir_info = trash_item(trash, "old-dir", "2026-07-01", directory=True)
    trash_item(trash, "fresh.txt", "2026-09-01")

    findings = tidy_home.expired_trash(layout, NOW, LIMITS)

    assert subjects(findings) == {str(old_file), str(old_info), str(old_dir), str(old_dir_info)}


def test_apply_removes_a_trashed_directory_tree(layout: Layout) -> None:
    trash = layout.home / ".local" / "share" / "Trash"
    old_dir, _ = trash_item(trash, "old-dir", "2026-07-01", directory=True)

    for finding in tidy_home.expired_trash(layout, NOW, LIMITS):
        tidy_home.apply_finding(finding, run=no_tools)

    assert not old_dir.exists()


def test_apply_unlinks_a_symlinked_item_and_leaves_its_target_alone(layout: Layout) -> None:
    target = layout.home / "Bilder" / "keep"
    touch(target / "photo.png", timedelta(0))
    link = layout.home / "Eingefügtes Bild (9).png"
    link.symlink_to(target)
    stamp = (NOW - timedelta(days=10)).timestamp()
    os.utime(link, (stamp, stamp), follow_symlinks=False)

    for finding in tidy_home.pasted_images(layout, NOW, LIMITS):
        tidy_home.apply_finding(finding, run=no_tools)

    assert not link.exists() and not link.is_symlink()
    assert (target / "photo.png").exists()


def test_vscode_snap_trash_is_covered_too(layout: Layout) -> None:
    trash = layout.home / "snap" / "code" / "261" / ".local" / "share" / "Trash"
    item = touch(trash / "files" / "song.wav", timedelta(0))
    touch(trash / "info" / "song.wav.trashinfo", timedelta(0), "DeletionDate=2026-01-01T00:00:00\n")

    assert str(item) in subjects(tidy_home.expired_trash(layout, NOW, LIMITS))


def test_a_dotted_trashinfo_name_cannot_escape_the_files_directory(layout: Layout) -> None:
    trash = layout.home / ".local" / "share" / "Trash"
    decoy = touch(trash / "files" / "keepme.txt", timedelta(0))
    touch(trash / "info" / "...trashinfo", timedelta(0), "[Trash Info]\nDeletionDate=2026-07-01T10:00:00\n")

    findings = tidy_home.expired_trash(layout, NOW, LIMITS)
    for finding in findings:
        tidy_home.apply_finding(finding, run=no_tools)

    assert findings == []
    assert decoy.exists()
    assert trash.exists()


def test_docker_rules_are_skipped_when_docker_is_unavailable() -> None:
    assert tidy_home.docker_leftovers(no_tools, NOW, LIMITS) == []


def test_docker_leftovers_emit_nothing_when_there_is_nothing_to_clean() -> None:
    docker = fake_docker(
        containers="live\t2026-08-01 10:00:00 +0200 CEST\trunning\n",
        system_df="Images\t5GB\t0GB (0%)\nBuild Cache\t1GB\t0GB\n",
    )

    assert tidy_home.docker_leftovers(docker, NOW, LIMITS) == []


def test_images_are_not_reported_without_dangling_images_even_when_docker_calls_them_reclaimable() -> None:
    docker = fake_docker(system_df="Images\t55.69GB\t25.66GB (46%)\nBuild Cache\t1GB\t0GB\n", dangling_images="")

    assert tidy_home.docker_leftovers(docker, NOW, LIMITS) == []


@pytest.mark.parametrize(
    ("system_df", "dangling_images", "expected_detail"),
    [
        ("Images\t20GB\t5GB (25%)\n", "1.5GB\n500MB\n2GB\n", "3 dangling, bis zu 4,0 GB"),
        ("Images\t59.34GB\t29.22GB (49%)\n", "50GB\n50GB\n", "2 dangling, bis zu 29,2 GB"),
        ("Build Cache\t1GB\t0GB\n", "2GB\n", "1 dangling, bis zu 0,0 GB"),
    ],
    ids=["sum below docker's figure", "shared layers inflate the sum", "docker reports no images at all"],
)
def test_dangling_images_are_counted_from_the_list_and_sized_no_higher_than_docker_says(
    system_df: str, dangling_images: str, expected_detail: str
) -> None:
    docker = fake_docker(system_df=system_df, dangling_images=dangling_images)

    images = next(f for f in tidy_home.docker_leftovers(docker, NOW, LIMITS) if f.subject == "dangling images")

    assert images.detail == expected_detail
    assert images.command == ("docker", "image", "prune", "-f")


def test_a_failing_image_listing_yields_no_images_finding() -> None:
    docker = fake_docker(system_df="Images\t59.34GB\t29.22GB (49%)\nBuild Cache\t1GB\t0GB\n", dangling_images=None)

    assert tidy_home.docker_leftovers(docker, NOW, LIMITS) == []


def test_docker_prune_commands_name_only_week_old_stopped_containers() -> None:
    docker = fake_docker(
        containers=(
            "old-probe\t2026-08-20 10:00:00 +0200 CEST\texited\n"
            "fresh-probe\t2026-09-04 10:00:00 +0200 CEST\texited\n"
            "live\t2026-08-01 10:00:00 +0200 CEST\trunning\n"
        ),
        system_df="Images\t55.69GB\t25.66GB (46%)\nContainers\t127MB\t14MB (11%)\nBuild Cache\t32.33GB\t421MB\n",
        dangling_images="25.66GB\n",
    )

    findings = tidy_home.docker_leftovers(docker, NOW, LIMITS)

    containers = next(f for f in findings if f.command[:3] == ("docker", "container", "prune"))
    assert containers.detail == "old-probe"
    assert "--filter" in containers.command and "until=168h" in containers.command
    assert all(f.action is Action.RUN and f.verdict is Verdict.DELETE for f in findings)
    details = {f.subject: f.detail for f in findings}
    assert details["dangling images"] == "1 dangling, bis zu 25,7 GB"
    assert details["build cache beyond 20GB"] == "32.33GB, reclaimable 421MB"


def test_docker_build_cache_limit_is_configurable() -> None:
    docker = fake_docker(system_df="Build Cache\t10GB\t3GB\n")

    findings = tidy_home.docker_leftovers(docker, NOW, Limits(docker_build_cache_keep="5GB"))

    assert any(f.subject == "build cache beyond 5GB" for f in findings)


def test_terabyte_sized_images_and_build_cache_are_findings() -> None:
    docker = fake_docker(system_df="Images\t2TB\t1.2TB (60%)\nBuild Cache\t1.5TB\t900GB\n", dangling_images="1.2TB\n")

    findings = tidy_home.docker_leftovers(docker, NOW, LIMITS)

    assert {f.subject for f in findings} == {"dangling images", "build cache beyond 20GB"}
    assert {f.subject: f.reclaimable_bytes for f in findings} == {
        "dangling images": 1_200_000_000_000,
        "build cache beyond 20GB": 900_000_000_000,
    }


@pytest.mark.parametrize(
    ("text", "expected_bytes"),
    [
        ("1.234TB", 1_234_000_000_000),
        ("534.5MB", 534_500_000),
        ("8.394kB", 8_394),
        ("12B", 12),
        ("2.5TB (30%)", 2_500_000_000_000),
        ("nichts", 0),
    ],
)
def test_docker_size_strings_are_read_in_every_unit_docker_prints(text: str, expected_bytes: int) -> None:
    assert tidy_home._bytes_from_size(text) == expected_bytes


def test_run_findings_show_the_exact_apply_command_in_the_dry_run_report(layout: Layout) -> None:
    with_readme(layout)
    docker = fake_docker(
        containers="old-probe\t2026-08-20 10:00:00 +0200 CEST\texited\n",
        system_df="Images\t10GB\t1GB (10%)\nBuild Cache\t30GB\t5GB\n",
        dangling_images="1GB\n",
    )

    findings = tidy_home.collect_findings(layout, NOW, Options(), LIMITS, run=docker, issue_state=lambda r, n: None)
    report = tidy_home.render_report(findings, NOW, layout.home)

    assert "--apply → `docker container prune -f --filter until=168h`" in report
    assert "--apply → `docker image prune -f`" in report
    assert "--apply → `docker builder prune -f --keep-storage=20GB`" in report


def test_uv_and_npm_never_appear_as_findings(layout: Layout) -> None:
    with_readme(layout)

    def fake_tools(argv):
        return "1.0.0\n" if argv[-1] == "--version" else None

    findings = tidy_home.collect_findings(layout, NOW, Options(), LIMITS, run=fake_tools, issue_state=lambda r, n: None)

    assert not any(f.rule == "caches" for f in findings)


def test_run_housekeeping_returns_the_last_output_line_per_tool() -> None:
    def fake_run(argv):
        if tuple(argv) == ("uv", "cache", "prune"):
            return "Removed 12 files\nTotal size: 3.4MiB\n"
        return None

    assert tidy_home.run_housekeeping(fake_run) == "Total size: 3.4MiB"


def test_dry_run_report_names_the_housekeeping_commands(layout: Layout) -> None:
    with_readme(layout)

    findings = tidy_home.collect_findings(layout, NOW, Options(), LIMITS, run=no_tools, issue_state=lambda r, n: None)
    report = tidy_home.render_report(findings, NOW, layout.home)

    assert "Wartung mit --apply: uv cache prune, npm cache verify" in report


def test_apply_report_shows_the_housekeeping_outcome(layout: Layout) -> None:
    with_readme(layout)

    findings = tidy_home.collect_findings(layout, NOW, Options(apply=True), LIMITS, run=no_tools, issue_state=lambda r, n: None)
    report = tidy_home.render_report(findings, NOW, layout.home, apply=True, housekeeping="ok; ok")

    assert "Wartung: ok; ok" in report
    assert "Wartung mit --apply" not in report


# --- audit rules ------------------------------------------------------------


def test_home_entries_missing_from_the_readme_are_reported(layout: Layout) -> None:
    with_readme(layout)
    touch(layout.home / "leonardo.db", timedelta(0))
    (layout.home / ".secrets").mkdir()
    (layout.home / "git").mkdir()
    (layout.home / ".config").mkdir()
    (layout.home / ".cloudflared").mkdir()

    findings = tidy_home.unknown_home_entries(layout)

    assert subjects(findings) == {str(layout.home / "leonardo.db")}
    assert findings[0].action is Action.REPORT
    assert findings[0].verdict is Verdict.REVIEW
    assert findings[0].recommendation == "in ~/README.md eintragen oder löschen"


def test_a_symlinked_unknown_entry_is_told_to_check_its_target(layout: Layout) -> None:
    with_readme(layout)
    target = layout.home / ".secrets" / "elsewhere"
    target.mkdir(parents=True)
    link = layout.home / "mystery-link"
    link.symlink_to(target)

    findings = tidy_home.unknown_home_entries(layout)

    assert findings == [Finding("unknown-home-entry", Action.REPORT, str(link), Verdict.REVIEW, "Symlink-Ziel prüfen")]


def test_missing_readme_yields_exactly_one_finding(layout: Layout) -> None:
    touch(layout.home / "leonardo.db", timedelta(0))

    findings = tidy_home.unknown_home_entries(layout)

    assert findings == [Finding("unknown-home-entry", Action.REPORT, "README.md fehlt", Verdict.REVIEW, "anlegen")]


def test_known_entries_stop_at_the_removed_section_heading() -> None:
    text = """
`git/x` and `.secrets/y` are known. `~/.cloudflared` and `Dokumente/` too.

## Was hier nicht mehr liegt

`.atelier` wurde entfernt. `openstream` auch.
"""

    known = tidy_home._known_home_entry_names(text)

    assert {"git", ".secrets", ".cloudflared", "Dokumente"} <= known
    assert ".atelier" not in known
    assert "openstream" not in known


def test_known_entries_skip_backtick_tokens_that_are_not_path_shaped() -> None:
    text = "Use `uv` or `pip` inside `songmaker`. Only `git/x` is a real path."

    known = tidy_home._known_home_entry_names(text)

    assert "git" in known
    assert not {"uv", "pip", "songmaker"} & known


def test_git_root_accepts_repos_and_their_worktree_dirs_only(layout: Layout) -> None:
    (layout.git / "songmaker" / ".git").mkdir(parents=True)
    (layout.git / "songmaker-worktrees").mkdir()
    (layout.git / "orphan-worktrees").mkdir()
    touch(layout.git / "scratch.log", timedelta(0))

    findings = tidy_home.git_root_litter(layout)

    assert subjects(findings) == {str(layout.git / "orphan-worktrees"), str(layout.git / "scratch.log")}
    assert all(f.verdict is Verdict.REVIEW for f in findings)


def test_worktrees_with_closed_issues_or_no_registration_are_reported(layout: Layout) -> None:
    repo = layout.git / "songmaker"
    (repo / ".git").mkdir(parents=True)
    container = layout.git / "songmaker-worktrees"
    closed = container / "issue-461-worker-ffmpeg"
    open_ = container / "issue-689-voices"
    unregistered = container / "issue-700-orphan"
    for wt in (closed, open_, unregistered):
        wt.mkdir(parents=True)

    def registered(argv):
        return "\n".join(f"worktree {p.resolve()}" for p in (repo, closed, open_))

    def issue_state(_repo: Path, number: int) -> str:
        return {461: "CLOSED", 689: "OPEN"}[number]

    findings = tidy_home.stale_worktrees(layout, issue_state, registered)

    assert {f.subject: f.detail for f in findings} == {
        str(closed): "issue #461 is closed",
        str(unregistered): "not registered in git worktree list",
    }
    assert all(f.verdict is Verdict.REMOVE and f.recommendation == "Claim freigeben, dann `git worktree remove`" for f in findings)


@pytest.mark.parametrize(
    "remote",
    ["git@github.com:overnightworks/songmaker.git\n", "https://github.com/overnightworks/songmaker.git\n"],
)
def test_issue_state_is_looked_up_on_the_repo_slug_of_either_remote_form(remote: str) -> None:
    calls: list[list[str]] = []

    def fake_run(argv):
        calls.append(list(argv))
        return remote if argv[0] == "git" else "CLOSED\n"

    assert tidy_home.github_issue_state(Path("/repo"), 461, fake_run) == "CLOSED"
    gh_call = next(c for c in calls if c[0] == "gh")
    assert gh_call[gh_call.index("-R") + 1] == "overnightworks/songmaker"


def test_largest_entries_only_list_big_entries_with_sizes_and_biggest_first(layout: Layout) -> None:
    with_readme(layout)

    def fake_du(argv):
        if argv[0] != "du":
            return None
        return f"2048\t{layout.home}/small\n8192\t{layout.home}/big\n20480\t{layout.home}/huge\n"

    without = tidy_home.collect_findings(layout, NOW, Options(), LIMITS, run=fake_du, issue_state=lambda r, n: None)
    with_sizes = tidy_home.collect_findings(layout, NOW, Options(sizes=True), LIMITS, run=fake_du, issue_state=lambda r, n: None)

    assert not [f for f in without if f.rule == "largest-entries"]
    sized = [f for f in with_sizes.audit if f.rule == "largest-entries"]
    assert [(f.subject, f.detail) for f in sized] == [
        (f"{layout.home}/huge", "20.0 GiB"),
        (f"{layout.home}/big", "8.0 GiB"),
    ]
    assert all(f.verdict is Verdict.REVIEW for f in sized)
    assert all(f.recommendation == "mit `du -h --max-depth=1 <Pfad>` aufschlüsseln" for f in sized)


def test_a_failed_du_is_not_applicable_not_a_clean_finding(layout: Layout) -> None:
    with_readme(layout)

    findings = tidy_home.collect_findings(layout, NOW, Options(sizes=True), LIMITS, run=no_tools, issue_state=lambda r, n: None)

    assert not [f for f in findings if f.rule == "largest-entries"]
    assert ("largest-entries", "du fehlgeschlagen") in findings.not_applicable


def test_findings_refuse_a_cleanup_action_filed_as_audit() -> None:
    stray = Finding("audit-rule", Action.DELETE, "/tmp/x", Verdict.DELETE, "--apply")
    with pytest.raises(ValueError):
        tidy_home.Findings(clean=(), audit=(stray,))


def test_findings_refuse_a_delete_verdict_on_an_audit_finding() -> None:
    stray = Finding("audit-rule", Action.REPORT, "/tmp/x", Verdict.DELETE, "--apply")
    with pytest.raises(ValueError):
        tidy_home.Findings(clean=(), audit=(stray,))


def test_findings_refuse_a_non_delete_verdict_on_a_clean_finding() -> None:
    stray = Finding("clean-rule", Action.DELETE, "/tmp/x", Verdict.REVIEW, "prüfen")
    with pytest.raises(ValueError):
        tidy_home.Findings(clean=(stray,), audit=())


def test_downloads_older_than_the_age_limit_are_reported_not_deleted(layout: Layout) -> None:
    old = touch(layout.downloads / "invoice.pdf", timedelta(days=20))
    touch(layout.downloads / "today.gif", timedelta(hours=1))

    findings = tidy_home.old_downloads(layout, NOW, LIMITS)

    assert subjects(findings) == {str(old)}
    assert findings[0].action is Action.REPORT
    assert findings[0].verdict is Verdict.REVIEW
    assert findings[0].recommendation == "nach Dokumente/ einsortieren oder löschen"


def test_downloads_ignore_hides_a_named_file(layout: Layout) -> None:
    touch(layout.downloads / "invoice.pdf", timedelta(days=20))
    touch(layout.downloads / "keepme.iso", timedelta(days=20))

    findings = tidy_home.old_downloads(layout, NOW, Limits(downloads_ignore=("keepme.iso",)))

    assert subjects(findings) == {str(layout.downloads / "invoice.pdf")}


def test_downloads_age_limit_is_configurable(layout: Layout) -> None:
    touch(layout.downloads / "invoice.pdf", timedelta(days=20))

    assert tidy_home.old_downloads(layout, NOW, Limits(downloads_days=30)) == []


def test_codex_sessions_are_summarised_once_when_old_files_exist(layout: Layout) -> None:
    touch(layout.codex_sessions / "a" / "rollout.jsonl", timedelta(days=100), "x" * 1024)
    touch(layout.codex_sessions / "b" / "rollout.jsonl", timedelta(days=1))

    findings = tidy_home.old_codex_sessions(layout, NOW, LIMITS)

    assert len(findings) == 1
    assert findings[0].detail.startswith("1 files older than 90 d")
    assert findings[0].verdict is Verdict.REVIEW
    assert findings[0].recommendation == ("Sessions älter als 90 d löschen: `find ~/.codex/sessions -type f -mtime +90 -delete`")


# --- configuration -----------------------------------------------------------


def test_limits_default_when_config_file_is_absent(layout: Layout) -> None:
    assert tidy_home.load_limits(layout.config_file) == Limits()


def test_limits_override_a_single_key(layout: Layout) -> None:
    touch(layout.config_file, timedelta(0), "trash_days = 60\n")

    limits = tidy_home.load_limits(layout.config_file)

    assert limits.trash_days == 60
    assert limits.downloads_days == Limits().downloads_days


def test_limits_reject_an_unknown_key(layout: Layout) -> None:
    touch(layout.config_file, timedelta(0), "not_a_real_key = 1\n")

    with pytest.raises(ValueError, match="not_a_real_key"):
        tidy_home.load_limits(layout.config_file)


def test_a_zero_build_cache_limit_is_accepted(layout: Layout) -> None:
    touch(layout.config_file, timedelta(0), 'docker_build_cache_keep = "0GB"\n')

    assert tidy_home.load_limits(layout.config_file).docker_build_cache_keep == "0GB"


def test_limits_parse_downloads_ignore_list(layout: Layout) -> None:
    touch(layout.config_file, timedelta(0), 'downloads_ignore = ["keepme.iso", "notes.txt"]\n')

    limits = tidy_home.load_limits(layout.config_file)

    assert limits.downloads_ignore == ("keepme.iso", "notes.txt")


@pytest.mark.parametrize(
    "config_text",
    [
        "trash_days = -1\n",
        "trash_days = true\n",
        'trash_days = "30"\n',
        "docker_build_cache_keep = 5\n",
        'docker_build_cache_keep = "20GiB"\n',
        'downloads_ignore = "keepme.iso"\n',
        "downloads_ignore = [1, 2]\n",
    ],
)
def test_limits_reject_a_bad_value_naming_the_key(layout: Layout, config_text: str) -> None:
    touch(layout.config_file, timedelta(0), config_text)
    key = config_text.split(" ", 1)[0]

    with pytest.raises(ValueError, match=key):
        tidy_home.load_limits(layout.config_file)


# --- orchestration ----------------------------------------------------------


def test_dry_run_reports_without_touching_anything(layout: Layout) -> None:
    with_readme(layout)
    image = touch(layout.home / "Eingefügtes Bild.png", timedelta(days=10))

    findings = tidy_home.collect_findings(layout, NOW, Options(), LIMITS, run=no_tools, issue_state=lambda r, n: None)
    report = tidy_home.render_report(findings, NOW, layout.home)

    assert image.exists()
    assert "Dry-Run" in report
    assert "| pasted-images |" in report
    assert "löschen" in report


def test_apply_deletes_clean_findings_and_records_the_outcome(layout: Layout) -> None:
    with_readme(layout)
    image = touch(layout.home / "Eingefügtes Bild.png", timedelta(days=10))
    findings = tidy_home.collect_findings(layout, NOW, Options(apply=True), LIMITS, run=no_tools, issue_state=lambda r, n: None)

    applied = {f: tidy_home.apply_finding(f, run=no_tools) for f in findings.clean}
    report = tidy_home.render_report(findings, NOW, layout.home, apply=True, applied=applied)

    assert not image.exists()
    assert "Apply" in report
    assert "Ergebnis" in report
    assert "deleted" in report


def test_apply_never_executes_report_findings() -> None:
    finding = Finding("downloads-inbox", Action.REPORT, "/tmp/x", Verdict.REVIEW, "prüfen")
    assert tidy_home.apply_finding(finding, run=no_tools) == ""


def test_run_findings_use_the_last_output_line_as_outcome() -> None:
    finding = Finding("docker", Action.RUN, "images", Verdict.DELETE, "--apply", command=("docker", "image", "prune", "-f"))
    assert (
        tidy_home.apply_finding(finding, run=lambda argv: "deleted: abc\nTotal reclaimed space: 1.2GB\n") == "Total reclaimed space: 1.2GB"
    )


def test_report_shows_subjects_relative_to_home(layout: Layout) -> None:
    with_readme(layout)
    touch(layout.downloads / "invoice.pdf", timedelta(days=20))

    findings = tidy_home.collect_findings(layout, NOW, Options(), LIMITS, run=no_tools, issue_state=lambda r, n: None)
    report = tidy_home.render_report(findings, NOW, layout.home)

    assert "~/Downloads/invoice.pdf" in report
    assert str(layout.home) not in report


def test_report_lists_rules_that_ran_and_found_nothing(layout: Layout) -> None:
    with_readme(layout)
    touch(layout.downloads / "invoice.pdf", timedelta(days=20))

    findings = tidy_home.collect_findings(layout, NOW, Options(), LIMITS, run=no_tools, issue_state=lambda r, n: None)
    report = tidy_home.render_report(findings, NOW, layout.home)

    assert "Geprüft, nichts gefunden:" in report
    assert "pasted-images" in report.rsplit("Geprüft, nichts gefunden:", 1)[1]


def test_missing_docker_is_not_applicable_not_checked_clean(layout: Layout) -> None:
    with_readme(layout)

    findings = tidy_home.collect_findings(layout, NOW, Options(), LIMITS, run=no_tools, issue_state=lambda r, n: None)
    report = tidy_home.render_report(findings, NOW, layout.home)

    assert "Nicht geprüft: docker (nicht installiert)" in report
    checked_section = report.split("Geprüft, nichts gefunden:", 1)[1].splitlines()[0]
    assert "docker" not in checked_section


def test_header_reports_reclaimable_docker_size(layout: Layout) -> None:
    with_readme(layout)
    docker = fake_docker(system_df="Images\t10GB\t2.5GB (25%)\nBuild Cache\t1GB\t0GB\n", dangling_images="1.5GB\n1GB\n")

    findings = tidy_home.collect_findings(layout, NOW, Options(), LIMITS, run=docker, issue_state=lambda r, n: None)
    report = tidy_home.render_report(findings, NOW, layout.home)

    assert "docker meldet ~2,5 GB wiederverwendbar" in report


def test_header_never_promises_more_than_docker_reports_for_images(layout: Layout) -> None:
    with_readme(layout)
    docker = fake_docker(system_df="Images\t59.34GB\t29.22GB (49%)\nBuild Cache\t1GB\t0GB\n", dangling_images="50GB\n50GB\n")

    findings = tidy_home.collect_findings(layout, NOW, Options(), LIMITS, run=docker, issue_state=lambda r, n: None)
    report = tidy_home.render_report(findings, NOW, layout.home)

    assert "docker meldet ~29,2 GB wiederverwendbar" in report
    assert "2 dangling, bis zu 29,2 GB" in report


def test_header_caps_the_build_cache_contribution_at_its_reclaimable_amount(layout: Layout) -> None:
    with_readme(layout)
    docker = fake_docker(system_df="Images\t55.69GB\t25.66GB (46%)\nBuild Cache\t32.33GB\t421MB\n", dangling_images="25.66GB\n")

    findings = tidy_home.collect_findings(layout, NOW, Options(), LIMITS, run=docker, issue_state=lambda r, n: None)
    report = tidy_home.render_report(findings, NOW, layout.home)

    # 25.66 GB (images) + 0.421 GB (build cache, capped at its own reclaimable figure, not size - keep)
    assert "docker meldet ~26,1 GB wiederverwendbar" in report


def test_header_counts_only_what_docker_can_reclaim(layout: Layout) -> None:
    bulky_audit_finding = Finding("codex-sessions", Action.REPORT, "/tmp/x", Verdict.REVIEW, "prüfen", reclaimable_bytes=9 * 10**9)

    report = tidy_home.render_report(tidy_home.Findings(clean=(), audit=(bulky_audit_finding,)), NOW, layout.home)

    assert "wiederverwendbar" not in report


def test_empty_report_says_so(layout: Layout) -> None:
    empty = tidy_home.Findings(clean=(), audit=())
    assert "Nichts zu tun." in tidy_home.render_report(empty, NOW, layout.home)


def test_pipe_characters_in_a_cell_are_escaped(layout: Layout) -> None:
    finding = Finding("rule", Action.REPORT, "sub|ject", Verdict.REVIEW, "prüfen", "de|tail")
    report = tidy_home.render_report(tidy_home.Findings(clean=(), audit=(finding,)), NOW, layout.home)

    assert "sub\\|ject" in report
    assert "de\\|tail" in report


def test_notify_sends_the_report_header_as_the_summary() -> None:
    calls: list[list[str]] = []
    report = "# tidy-home 05.09.2026 12:00  Dry-Run | 1 Funde\n\n| Regel |\n"

    tidy_home.notify(report, run=lambda argv: calls.append(list(argv)))

    assert calls == [["notify-send", "# tidy-home 05.09.2026 12:00  Dry-Run | 1 Funde"]]


def test_exit_code_is_zero_when_nothing_is_found() -> None:
    assert tidy_home.exit_code(tidy_home.Findings(clean=(), audit=())) == 0


def test_exit_code_is_one_when_any_finding_exists() -> None:
    finding = Finding("downloads-inbox", Action.REPORT, "/tmp/x", Verdict.REVIEW, "prüfen")
    assert tidy_home.exit_code(tidy_home.Findings(clean=(), audit=(finding,))) == 1


# --- main --------------------------------------------------------------------


def test_main_runs_end_to_end_with_an_injected_home_and_runner(tmp_path: Path) -> None:
    dirty_home = tmp_path / "dirty"
    with_readme(Layout(dirty_home))
    touch(dirty_home / "Eingefügtes Bild.png", timedelta(days=10))
    clean_home = tmp_path / "clean"
    with_readme(Layout(clean_home))

    dirty_calls: list[list[str]] = []
    clean_calls: list[list[str]] = []

    def record(calls, argv):
        calls.append(list(argv))
        return None

    dirty_code = tidy_home.main([], home=dirty_home, run=lambda argv: record(dirty_calls, argv))
    clean_code = tidy_home.main([], home=clean_home, run=lambda argv: record(clean_calls, argv))

    assert dirty_code == 1
    assert clean_code == 0
    assert (dirty_home / ".local" / "state" / "tidy-home" / "last-report.md").is_file()
    assert (clean_home / ".local" / "state" / "tidy-home" / "last-report.md").is_file()
    assert any(call[:1] == ["notify-send"] for call in dirty_calls)
    assert not any(call[:1] == ["notify-send"] for call in clean_calls)


@pytest.mark.parametrize(
    "config_text",
    [
        "not_a_real_key = 1\n",
        "trash_days = [\n",
    ],
)
def test_main_exits_2_and_reports_a_broken_config(tmp_path: Path, capsys: pytest.CaptureFixture[str], config_text: str) -> None:
    layout = Layout(tmp_path)
    touch(layout.config_file, timedelta(0), config_text)

    code = tidy_home.main([], home=tmp_path, run=no_tools)

    assert code == 2
    assert "tidy-home:" in capsys.readouterr().err


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_main_exits_2_when_the_config_file_cannot_be_read(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    layout = Layout(tmp_path)
    touch(layout.config_file, timedelta(0), "trash_days = 5\n")
    layout.config_file.chmod(0)
    try:
        code = tidy_home.main([], home=tmp_path, run=no_tools)
    finally:
        layout.config_file.chmod(0o644)

    assert code == 2
    assert "tidy-home:" in capsys.readouterr().err
