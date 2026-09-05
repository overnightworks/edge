from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import tidy_home
from tidy_home import Action, Finding, Layout, Options

NOW = datetime(2026, 9, 5, 12, 0)


def touch(path: Path, age: timedelta, content: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    stamp = (NOW - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def no_tools(_argv):
    return None


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return Layout(tmp_path)


def subjects(findings: list[Finding]) -> set[str]:
    return {f.subject for f in findings}


# --- clean rules ------------------------------------------------------------


def test_pasted_images_older_than_a_week_are_deletions_in_every_watched_dir(layout: Layout) -> None:
    old_home = touch(layout.home / "Eingefügtes Bild (3).png", timedelta(days=8))
    old_git = touch(layout.git / "Eingefügtes Bild.png", timedelta(days=30))
    touch(layout.home / "Eingefügtes Bild (4).png", timedelta(days=2))
    touch(layout.home / "Dokumente" / "cv" / "Eingefügtes Bild.png", timedelta(days=60))

    findings = tidy_home.pasted_images(layout, NOW)

    assert subjects(findings) == {str(old_home), str(old_git)}
    assert all(f.action is Action.DELETE for f in findings)


def trash_item(trash: Path, name: str, deleted_on: str, directory: bool = False) -> tuple[Path, Path]:
    item = trash / "files" / name
    if directory:
        touch(item / "inner.txt", timedelta(0))
    else:
        touch(item, timedelta(0))
    info = touch(trash / "info" / f"{name}.trashinfo", timedelta(0), f"[Trash Info]\nDeletionDate={deleted_on}T10:00:00\n")
    return item, info


def test_trash_items_past_thirty_days_are_deleted_with_their_info(layout: Layout) -> None:
    trash = layout.home / ".local" / "share" / "Trash"
    old_file, old_info = trash_item(trash, "old.txt", "2026-07-01")
    old_dir, old_dir_info = trash_item(trash, "old-dir", "2026-07-01", directory=True)
    trash_item(trash, "fresh.txt", "2026-09-01")

    findings = tidy_home.expired_trash(layout, NOW)

    assert subjects(findings) == {str(old_file), str(old_info), str(old_dir), str(old_dir_info)}


def test_apply_removes_a_trashed_directory_tree(layout: Layout) -> None:
    trash = layout.home / ".local" / "share" / "Trash"
    old_dir, _ = trash_item(trash, "old-dir", "2026-07-01", directory=True)

    for finding in tidy_home.expired_trash(layout, NOW):
        tidy_home.apply_finding(finding, run=no_tools)

    assert not old_dir.exists()


def test_apply_unlinks_a_symlinked_item_and_leaves_its_target_alone(layout: Layout) -> None:
    target = layout.home / "Bilder" / "keep"
    touch(target / "photo.png", timedelta(0))
    link = layout.home / "Eingefügtes Bild (9).png"
    link.symlink_to(target)
    stamp = (NOW - timedelta(days=10)).timestamp()
    os.utime(link, (stamp, stamp), follow_symlinks=False)

    for finding in tidy_home.pasted_images(layout, NOW):
        tidy_home.apply_finding(finding, run=no_tools)

    assert not link.exists() and not link.is_symlink()
    assert (target / "photo.png").exists()


def test_vscode_snap_trash_is_covered_too(layout: Layout) -> None:
    trash = layout.home / "snap" / "code" / "261" / ".local" / "share" / "Trash"
    item = touch(trash / "files" / "song.wav", timedelta(0))
    touch(trash / "info" / "song.wav.trashinfo", timedelta(0), "DeletionDate=2026-01-01T00:00:00\n")

    assert str(item) in subjects(tidy_home.expired_trash(layout, NOW))


def test_docker_rules_are_skipped_when_docker_is_unavailable() -> None:
    assert tidy_home.docker_leftovers(no_tools, NOW) == []


def test_docker_prune_commands_name_only_week_old_stopped_containers() -> None:
    def fake_docker(argv):
        if argv[:2] == ["docker", "info"]:
            return "28.0\n"
        if argv[:3] == ["docker", "container", "ls"]:
            return (
                "old-probe\t2026-08-20 10:00:00 +0200 CEST\texited\n"
                "fresh-probe\t2026-09-04 10:00:00 +0200 CEST\texited\n"
                "live\t2026-08-01 10:00:00 +0200 CEST\trunning\n"
            )
        if argv[:3] == ["docker", "system", "df"]:
            return "Images\t55.69GB\t25.66GB (46%)\nContainers\t127MB\t14MB (11%)\nBuild Cache\t32.33GB\t421MB\n"
        return ""

    findings = tidy_home.docker_leftovers(fake_docker, NOW)

    containers = next(f for f in findings if f.command[:3] == ("docker", "container", "prune"))
    assert containers.detail == "old-probe"
    assert "--filter" in containers.command and "until=168h" in containers.command
    assert all(f.action is Action.RUN for f in findings)
    details = {f.subject: f.detail for f in findings}
    assert details["dangling images"] == "55.69GB, reclaimable 25.66GB (46%)"
    assert details["build cache beyond 20GB"] == "32.33GB, reclaimable 421MB"


# --- audit rules ------------------------------------------------------------


def test_home_entries_missing_from_the_readme_are_reported(layout: Layout) -> None:
    touch(layout.home / "README.md", timedelta(0))
    touch(layout.home / "leonardo.db", timedelta(0))
    (layout.home / ".secrets").mkdir()

    findings = tidy_home.unknown_home_entries(layout)

    assert subjects(findings) == {str(layout.home / "leonardo.db")}
    assert findings[0].action is Action.REPORT


def test_git_root_accepts_repos_and_their_worktree_dirs_only(layout: Layout) -> None:
    (layout.git / "songmaker" / ".git").mkdir(parents=True)
    (layout.git / "songmaker-worktrees").mkdir()
    (layout.git / "orphan-worktrees").mkdir()
    touch(layout.git / "scratch.log", timedelta(0))

    findings = tidy_home.git_root_litter(layout)

    assert subjects(findings) == {str(layout.git / "orphan-worktrees"), str(layout.git / "scratch.log")}


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


def test_largest_entries_are_listed_only_with_sizes_and_biggest_first(layout: Layout) -> None:
    def fake_du(argv):
        return f"3\t{layout.home}/small\n2048\t{layout.home}/big\n" if argv[0] == "du" else None

    without = tidy_home.collect_findings(layout, NOW, Options(), run=fake_du, issue_state=lambda r, n: None)
    with_sizes = tidy_home.collect_findings(layout, NOW, Options(sizes=True), run=fake_du, issue_state=lambda r, n: None)

    assert not [f for f in without if f.rule == "largest-entries"]
    sized = [f for f in with_sizes.audit if f.rule == "largest-entries"]
    assert [(f.subject, f.detail) for f in sized] == [(f"{layout.home}/big", "2.0 GiB"), (f"{layout.home}/small", "0.0 GiB")]


def test_findings_refuse_a_cleanup_action_filed_as_audit() -> None:
    stray = Finding("audit-rule", Action.DELETE, "/tmp/x")
    with pytest.raises(ValueError):
        tidy_home.Findings(clean=(), audit=(stray,))


def test_downloads_older_than_two_weeks_are_reported_not_deleted(layout: Layout) -> None:
    old = touch(layout.downloads / "invoice.pdf", timedelta(days=20))
    touch(layout.downloads / "today.gif", timedelta(hours=1))

    findings = tidy_home.old_downloads(layout, NOW)

    assert subjects(findings) == {str(old)}
    assert findings[0].action is Action.REPORT


def test_codex_sessions_are_summarised_once_when_old_files_exist(layout: Layout) -> None:
    touch(layout.codex_sessions / "a" / "rollout.jsonl", timedelta(days=100), "x" * 1024)
    touch(layout.codex_sessions / "b" / "rollout.jsonl", timedelta(days=1))

    findings = tidy_home.old_codex_sessions(layout, NOW)

    assert len(findings) == 1
    assert findings[0].detail.startswith("1 files older than 90 d")


# --- orchestration ----------------------------------------------------------


def test_dry_run_reports_without_touching_anything(layout: Layout) -> None:
    image = touch(layout.home / "Eingefügtes Bild.png", timedelta(days=10))

    findings = tidy_home.collect_findings(layout, NOW, Options(), run=no_tools, issue_state=lambda r, n: None)
    report = tidy_home.render_report(findings, NOW)

    assert image.exists()
    assert "(dry run)" in report
    assert "## pasted-images (clean, 1)" in report


def test_apply_deletes_clean_findings_and_records_the_outcome(layout: Layout) -> None:
    image = touch(layout.home / "Eingefügtes Bild.png", timedelta(days=10))
    findings = tidy_home.collect_findings(layout, NOW, Options(apply=True), run=no_tools, issue_state=lambda r, n: None)

    applied = {f: tidy_home.apply_finding(f, run=no_tools) for f in findings.clean}
    report = tidy_home.render_report(findings, NOW, applied)

    assert not image.exists()
    assert f"- {image} [10 d] -> deleted" in report
    assert "(apply)" in report


def test_apply_never_executes_report_findings() -> None:
    finding = Finding("downloads-inbox", Action.REPORT, "/tmp/x")
    assert tidy_home.apply_finding(finding, run=no_tools) == ""


def test_run_findings_use_the_last_output_line_as_outcome() -> None:
    finding = Finding("docker", Action.RUN, "images", command=("docker", "image", "prune", "-f"))
    assert (
        tidy_home.apply_finding(finding, run=lambda argv: "deleted: abc\nTotal reclaimed space: 1.2GB\n") == "Total reclaimed space: 1.2GB"
    )


def test_empty_report_says_so() -> None:
    assert "Nothing to do." in tidy_home.render_report([], NOW)
