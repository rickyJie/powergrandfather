"""Skill bundle walk / hash / path-validation.

The read half of "a skill is a directory, not a file". The failure this
guards against is subtle: a bundle that looks complete but quietly dropped
files reproduces the original bug in a form that's harder to notice, so
omissions have to be *reported*, never silent.
"""
from __future__ import annotations

import os

import pytest
from csm.modules.sync.bundle import (
    IGNORE_FILE,
    BundleFile,
    BundleTooLarge,
    bundle_hash,
    bundle_manifest,
    count_bundle_files,
    resolve_within,
    validate_rel_path,
    walk_skill_bundle,
)


@pytest.fixture
def skill_dir(tmp_path):
    d = tmp_path / "demo"
    (d / "scripts").mkdir(parents=True)
    (d / "references").mkdir()
    (d / "SKILL.md").write_text("---\nname: demo\n---\nrun ./scripts/go.py\n")
    go = d / "scripts" / "go.py"
    go.write_text("#!/usr/bin/env python3\n")
    go.chmod(0o755)
    (d / "references" / "notes.md").write_text("# notes\n")
    return d


# ---- walk ------------------------------------------------------------


def test_walk_collects_siblings_and_excludes_skill_md(skill_dir):
    files, skipped = walk_skill_bundle(skill_dir)
    assert [f.rel_path for f in files] == ["references/notes.md", "scripts/go.py"]
    assert skipped == []


def test_walk_captures_permission_bits(skill_dir):
    files, _ = walk_skill_bundle(skill_dir)
    modes = {f.rel_path: f.mode for f in files}
    assert modes["scripts/go.py"] == 0o755
    assert modes["references/notes.md"] & 0o111 == 0  # not executable


def test_walk_excludes_build_artefacts(skill_dir):
    (skill_dir / "__pycache__").mkdir()
    (skill_dir / "__pycache__" / "go.cpython-311.pyc").write_bytes(b"junk")
    (skill_dir / ".git").mkdir()
    (skill_dir / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (skill_dir / "scripts" / "stale.pyc").write_bytes(b"junk")
    (skill_dir / "scripts" / "stale.py.lock").write_bytes(b"lock bookkeeping")
    (skill_dir / ".DS_Store").write_bytes(b"junk")

    rels = [f.rel_path for f in walk_skill_bundle(skill_dir)[0]]
    assert rels == ["references/notes.md", "scripts/go.py"]


def test_walk_follows_symlinked_skill_dir(tmp_path, skill_dir):
    """The common real shape: ~/.claude/skills/<name> links into a repo."""
    link = tmp_path / "linked"
    os.symlink(skill_dir, link)
    rels = [f.rel_path for f in walk_skill_bundle(link)[0]]
    assert rels == ["references/notes.md", "scripts/go.py"]


def test_walk_reads_through_a_symlinked_file(skill_dir, tmp_path):
    """Content is copied; the link itself is never reproduced on write."""
    external = tmp_path / "shared.md"
    external.write_text("shared content")
    os.symlink(external, skill_dir / "references" / "linked.md")

    files, _ = walk_skill_bundle(skill_dir)
    by_path = {f.rel_path: f for f in files}
    assert by_path["references/linked.md"].content == b"shared content"


def test_walk_honours_csmsyncignore(skill_dir):
    (skill_dir / "reports").mkdir()
    (skill_dir / "reports" / "run-1.md").write_text("noise")
    (skill_dir / IGNORE_FILE).write_text("reports/\n")

    rels = [f.rel_path for f in walk_skill_bundle(skill_dir)[0]]
    assert rels == ["references/notes.md", "scripts/go.py"]
    assert IGNORE_FILE not in rels  # the ignore file itself never syncs


def test_walk_reports_oversize_files_instead_of_dropping_them(skill_dir):
    (skill_dir / "big.bin").write_bytes(b"x" * 5000)
    files, skipped = walk_skill_bundle(skill_dir, max_file_bytes=1000)

    assert "big.bin" not in [f.rel_path for f in files]
    assert len(skipped) == 1
    assert "big.bin" in skipped[0] and "cap" in skipped[0]


def test_walk_raises_rather_than_truncating_a_huge_bundle(skill_dir):
    for i in range(10):
        (skill_dir / f"f{i}.md").write_text("x")
    with pytest.raises(BundleTooLarge) as e:
        walk_skill_bundle(skill_dir, max_files=5)
    assert IGNORE_FILE in str(e.value)  # tells the user how to fix it


def test_walk_missing_dir_is_empty(tmp_path):
    assert walk_skill_bundle(tmp_path / "nope") == ([], [])


def test_count_bundle_files_matches_the_walk(skill_dir):
    (skill_dir / "__pycache__").mkdir()
    (skill_dir / "__pycache__" / "x.pyc").write_bytes(b"junk")
    assert count_bundle_files(skill_dir) == len(walk_skill_bundle(skill_dir)[0])


# ---- rel_path validation --------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "../escape.txt",
        "scripts/../../escape.txt",
        "",
        "   ",
        "SKILL.md",
        "nul\x00byte",
    ],
)
def test_validate_rel_path_rejects(bad):
    with pytest.raises(ValueError):
        validate_rel_path(bad)


def test_validate_rel_path_accepts_nested():
    assert validate_rel_path("scripts/sub/go.py") == "scripts/sub/go.py"


def test_validate_rel_path_normalises_a_leading_dot():
    """`./x` is safe — it denotes the same file — so it normalises rather
    than raising. Only `..` escapes."""
    assert validate_rel_path("./implicit.txt") == "implicit.txt"


def test_resolve_within_blocks_escape_through_an_existing_symlink(tmp_path):
    """A symlink already inside the target dir must not redirect the write."""
    root = tmp_path / "skill"
    (root / "refs").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "refs" / "out")

    with pytest.raises(ValueError):
        resolve_within(root, "refs/out/../../../evil.txt")


# ---- hashing ---------------------------------------------------------


def test_bundle_hash_changes_when_a_file_is_removed(skill_dir):
    files, _ = walk_skill_bundle(skill_dir)
    full = bundle_hash("body", files)
    partial = bundle_hash("body", files[:1])
    assert full != partial


def test_bundle_hash_changes_when_the_exec_bit_is_stripped(skill_dir):
    files, _ = walk_skill_bundle(skill_dir)
    stripped = [
        BundleFile(rel_path=f.rel_path, content=f.content, mode=0o644) for f in files
    ]
    assert bundle_hash("body", files) != bundle_hash("body", stripped)


def test_bundle_hash_is_order_independent(skill_dir):
    files, _ = walk_skill_bundle(skill_dir)
    assert bundle_hash("body", files) == bundle_hash("body", list(reversed(files)))


def test_bundle_hash_tracks_the_skill_md_body(skill_dir):
    files, _ = walk_skill_bundle(skill_dir)
    assert bundle_hash("body one", files) != bundle_hash("body two", files)


def test_bundle_manifest_shape(skill_dir):
    files, _ = walk_skill_bundle(skill_dir)
    m = bundle_manifest(files)
    assert set(m) == {"references/notes.md", "scripts/go.py"}
    assert all(len(v) == 64 for v in m.values())
