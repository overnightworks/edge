"""Ratchet over the old operator-facing names `edge ...` and `home ...`.

The operator types only `hal`; every remaining old name is counted per tracked
file and may only shrink. When the test is red it prints the whole baseline to
paste into tests/old_names_baseline.toml, so the baseline is regenerated, never
hand-tuned.
"""

import re
import subprocess
import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = Path(__file__).with_name("old_names_baseline.toml")
OWN_FILES = frozenset({"tests/test_old_names.py", "tests/old_names_baseline.toml"})

OLD_NAME_PATTERNS: dict[str, re.Pattern[str]] = {
    "edge-verb": re.compile(r"\bedge (apply|stop|start|external|offline|online)\b"),
    "edge-script": re.compile(r"\bedge-(backup|check)\b"),
    "home-verb": re.compile(r"\bhome (check|next|tidy|secrets|setup)\b"),
    "home-tools": re.compile(r"\bhome-tools\b"),
    "config-directory": re.compile(r"~/\.config/(home|tidy-home)\b"),
    "state-directory": re.compile(r"~/\.local/state/home\b"),
}

Counts = dict[str, dict[str, int]]


def tracked_files() -> list[str]:
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        check=True,
    ).stdout
    names = (name.decode() for name in listing.split(b"\0") if name)
    return sorted(name for name in names if name not in OWN_FILES)


def read_text(name: str) -> str | None:
    try:
        return (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
        return None


def count_old_names() -> Counts:
    counts: Counts = {pattern_name: {} for pattern_name in OLD_NAME_PATTERNS}
    for name in tracked_files():
        text = read_text(name)
        if text is None:
            continue
        for pattern_name, pattern in OLD_NAME_PATTERNS.items():
            hits = len(pattern.findall(text))
            if hits:
                counts[pattern_name][name] = hits
    return counts


def load_baseline() -> Counts:
    with BASELINE_PATH.open("rb") as baseline_file:
        return tomllib.load(baseline_file)


def render_baseline(counts: Counts) -> str:
    sections = []
    for pattern_name, per_file in counts.items():
        lines = [f"[{pattern_name}]"]
        lines += [f'"{name}" = {hits}' for name, hits in sorted(per_file.items())]
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"


def find_violations(baseline: Counts, counts: Counts) -> list[str]:
    violations = []
    tracked = set(tracked_files())
    for pattern_name in OLD_NAME_PATTERNS:
        allowed = baseline.get(pattern_name, {})
        current = counts[pattern_name]
        for name, hits in sorted(current.items()):
            if name not in allowed:
                violations.append(
                    f"{name}: new file with {hits} x {pattern_name} (baseline has none)"
                )
            elif hits > allowed[name]:
                violations.append(f"{name}: {pattern_name} grew from {allowed[name]} to {hits}")
        for name, allowed_hits in sorted(allowed.items()):
            if name not in tracked:
                violations.append(
                    f"{name}: {pattern_name} baseline entry names a file that no longer exists"
                )
            elif current.get(name, 0) < allowed_hits:
                violations.append(
                    f"{name}: tighten {pattern_name} baseline from {allowed_hits} "
                    f"to {current.get(name, 0)}"
                )
    return violations


def test_old_names_only_shrink() -> None:
    counts = count_old_names()
    violations = find_violations(load_baseline(), counts)
    assert not violations, (
        "Old operator-facing names must only shrink (#207):\n"
        + "\n".join(violations)
        + "\n\nPaste this as tests/old_names_baseline.toml:\n\n"
        + render_baseline(counts)
    )


def test_every_pattern_is_listed_in_the_baseline() -> None:
    assert set(load_baseline()) == set(OLD_NAME_PATTERNS)
