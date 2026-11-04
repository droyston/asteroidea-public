"""Red proofs (a) to (f) on copies of the four real templates.

The fixtures are the templates synthesized from the repo with `-c sandbox-test=true
-c raw-retention-days=1`. Each test builds a base and a head root from them, changes the
head, and runs the comparison as `scripts/gates/templates.sh` does.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from asteroidea_infra import template_compare

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "templates"
API = "ServicesStackApiC48500D6.nested.template.json"
INGEST = "ServicesStackIngest4A804412.nested.template.json"
CONTEXT = "sandbox-test"
COMPUTE_IMAGE = "f93a7911dbcbfe208beecd8023e4e354fbd0e286210c110746591579aa189efe"
EXTRACT_IMAGE = "04a45187102e817890eee04b0269d3003880b412e9eec8126c663a0e339bcc0d"
ZIP_ASSET = "f69d952cc195c09bfd2a6f86f964a861702df06ebdff876c9a9be2b389ccb01e"

type Files = dict[str, str]


def read_all() -> Files:
    return {p.name: p.read_text() for p in sorted(FIXTURES.glob("*.template.json"))}


def write_root(root: Path, files: Files) -> None:
    (root / CONTEXT).mkdir(parents=True)
    for name, text in files.items():
        (root / CONTEXT / name).write_text(text)


def verdict(
    tmp_path: Path, edit: Callable[[Files], Files], capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    """Compare the fixtures to an edited copy; return exit status and output."""
    write_root(tmp_path / "base", read_all())
    write_root(tmp_path / "head", edit(read_all()))
    status = template_compare.main([str(tmp_path / "base"), str(tmp_path / "head")])
    return status, capsys.readouterr().out


def edit_json(
    files: Files, name: str, change: Callable[[dict[str, Any]], None]
) -> Files:
    template = json.loads(files[name])
    change(template)
    return {**files, name: json.dumps(template, indent=1)}


def test_fixtures_are_the_four_real_templates() -> None:
    files = read_all()
    counts = [len(json.loads(text)["Resources"]) for text in files.values()]
    assert sorted(files) == sorted(
        [
            "DataStack.template.json",
            "ServicesStack.template.json",
            API,
            INGEST,
        ]
    )
    assert counts == [35, 3, 80, 38]


def test_identical_templates_are_equal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, out = verdict(tmp_path, lambda f: f, capsys)
    assert (status, out) == (0, "templates equal: 4 files x 1 contexts\n")


def test_a_one_iam_action_change_names_the_statement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(a) One IAM action changed: exit 1 naming the statement path."""

    def change(template: dict[str, Any]) -> None:
        policy = next(
            r for r in template["Resources"].values() if r["Type"] == "AWS::IAM::Policy"
        )
        statement = policy["Properties"]["PolicyDocument"]["Statement"][0]
        actions = statement["Action"]
        statement["Action"] = (
            [*actions, "s3:DeleteObject"]
            if isinstance(actions, list)
            else [actions, "s3:DeleteObject"]
        )

    status, out = verdict(tmp_path, lambda f: edit_json(f, API, change), capsys)
    assert status == 1
    assert f"DIFF {CONTEXT} {API} $.Resources." in out
    assert ".PolicyDocument.Statement[0].Action" in out
    assert "s3:DeleteObject" in out


def test_changed_zip_and_nested_template_hashes_alone_are_equal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(b) Only the zip and nested-template hashes changed: exit 0."""

    def edit(files: Files) -> Files:
        out = dict(files)
        out[API] = out[API].replace(ZIP_ASSET, "1" * 64)
        services = json.loads(out["ServicesStack.template.json"])
        urls = [
            r["Properties"]["TemplateURL"]
            for r in services["Resources"].values()
            if r["Type"] == "AWS::CloudFormation::Stack"
        ]
        assert len(urls) == 2
        text = out["ServicesStack.template.json"]
        for token in sorted(
            {t for u in urls for t in template_compare.HEX64.findall(json.dumps(u))}
        ):
            text = text.replace(token, "2" * 63 + token[0])
        out["ServicesStack.template.json"] = text
        return out

    status, out = verdict(tmp_path, edit, capsys)
    assert status == 0, out
    assert out.startswith("templates equal:")


def test_a_renamed_construct_id_is_a_difference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(c) A construct id renamed: the logical id key is absent on one side."""

    def change(template: dict[str, Any]) -> None:
        old = next(
            k
            for k, r in template["Resources"].items()
            if r["Type"] == "AWS::SQS::Queue"
        )
        template["Resources"]["Renamed" + old] = template["Resources"].pop(old)

    status, out = verdict(tmp_path, lambda f: edit_json(f, INGEST, change), capsys)
    assert status == 1
    assert f"DIFF {CONTEXT} {INGEST} $.Resources.Renamed" in out
    assert "base=<absent>" in out
    assert "head=<absent>" in out


def test_a_changed_hex_value_in_an_environment_variable_is_a_difference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(d) A 64-hex value in a Lambda environment variable is left alone."""

    def plant(token: str) -> Callable[[Files], Files]:
        def change(template: dict[str, Any]) -> None:
            function = next(
                r
                for r in template["Resources"].values()
                if r["Type"] == "AWS::Lambda::Function"
            )
            function["Properties"]["Environment"]["Variables"]["PLANTED"] = token

        return lambda f: edit_json(f, API, change)

    write_root(tmp_path / "base", plant("a" * 64)(read_all()))
    write_root(tmp_path / "head", plant("b" * 64)(read_all()))
    status = template_compare.main([str(tmp_path / "base"), str(tmp_path / "head")])
    out = capsys.readouterr().out
    assert status == 1
    assert f"{API} $.Resources." in out
    assert ".Environment.Variables.PLANTED" in out
    assert "a" * 64 in out
    assert "b" * 64 in out


def test_an_added_template_file_is_a_difference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(e) A template file present on one side only."""

    def add(files: Files) -> Files:
        return {**files, "Extra.template.json": files["DataStack.template.json"]}

    status, out = verdict(tmp_path, add, capsys)
    assert status == 1
    assert f"DIFF {CONTEXT} Extra.template.json $: base=<absent> head=" in out


def test_a_swapped_image_hash_in_one_stack_is_a_difference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(f) Api's compute image swapped for the extract image: exit 1.

    The compute image hash appears in both the Api and the Ingest templates, so the one
    numbering across files sees the swap that a per-file numbering would hide.
    """

    def swap(files: Files) -> Files:
        return {**files, API: files[API].replace(COMPUTE_IMAGE, EXTRACT_IMAGE)}

    status, out = verdict(tmp_path, swap, capsys)
    assert status == 1
    assert "<asset:" in out
    assert f"DIFF {CONTEXT} " in out


def test_a_missing_context_directory_is_a_difference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_root(tmp_path / "base", read_all())
    (tmp_path / "head").mkdir()
    status = template_compare.main([str(tmp_path / "base"), str(tmp_path / "head")])
    assert status == 1
    assert capsys.readouterr().out.count("DIFF ") > 0


def test_more_than_fifty_differences_print_fifty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def change(template: dict[str, Any]) -> None:
        for resource in template["Resources"].values():
            resource["Type"] = "AWS::Changed"

    status, out = verdict(tmp_path, lambda f: edit_json(f, API, change), capsys)
    assert status == 1
    assert out.count("DIFF ") == template_compare.MAX_DIFFS
    assert "more" in out


def test_usage_error_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert template_compare.main(["only-one"]) == 2
    assert "usage:" in capsys.readouterr().err


def planted_line(files: Files) -> tuple[Files, str]:
    """Return head files with one changed Lambda handler and its exact DIFF line."""
    template = json.loads(files[API])
    name, function = next(
        (k, r)
        for k, r in template["Resources"].items()
        if r["Type"] == "AWS::Lambda::Function" and "Handler" in r["Properties"]
    )
    old = function["Properties"]["Handler"]
    function["Properties"]["Handler"] = old + ".planted"
    line = (
        f"DIFF {CONTEXT} {API} $.Resources.{name}.Properties.Handler:"
        f' base="{old}" head="{old}.planted"'
    )
    return {**files, API: json.dumps(template, indent=1)}, line


def expected_verdict(
    tmp_path: Path,
    *,
    plant: bool,
    expected: list[str],
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    head = read_all()
    line = ""
    if plant:
        head, line = planted_line(head)
    write_root(tmp_path / "base", read_all())
    write_root(tmp_path / "head", head)
    listed = tmp_path / "template-diff.expected"
    body = "\n".join(e.replace("<LINE>", line) for e in expected)
    listed.write_text(f"# header\n\n{body}\n")
    status = template_compare.main(
        [str(tmp_path / "base"), str(tmp_path / "head"), "--expected", str(listed)]
    )
    return status, capsys.readouterr().out


def test_expected_file_empty_and_no_diff_is_equal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red proof 1: no listed lines, no diff: exit 0 equal."""
    status, out = expected_verdict(tmp_path, plant=False, expected=[], capsys=capsys)
    assert (status, out) == (0, "templates equal: 4 files x 1 contexts\n")


def test_expected_file_empty_and_a_diff_fails_as_before(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red proof 2: no listed lines, one diff: exit 1 with the DIFF line."""
    status, out = expected_verdict(tmp_path, plant=True, expected=[], capsys=capsys)
    assert status == 1
    assert out.startswith(f"DIFF {CONTEXT} {API} ")


def test_expected_file_listing_the_diff_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red proof 3: the listed lines equal the actual lines: exit 0."""
    status, out = expected_verdict(
        tmp_path, plant=True, expected=["<LINE>"], capsys=capsys
    )
    assert (status, out) == (0, "templates differ as expected: 1 lines\n")


def test_expected_file_missing_a_line_reports_unexpected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red proof 4: the file lacks a line the diff has: exit 1 with UNEXPECTED."""
    other = f"DIFF {CONTEXT} {API} $.Other: base=1 head=2"
    status, out = expected_verdict(
        tmp_path, plant=True, expected=[other], capsys=capsys
    )
    assert status == 1
    assert f"UNEXPECTED DIFF {CONTEXT} {API} " in out


def test_expected_file_with_an_extra_line_reports_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red proof 5: the file lists a line the diff lacks: exit 1 with MISSING."""
    extra = f"DIFF {CONTEXT} {API} $.Extra: base=1 head=2"
    status, out = expected_verdict(
        tmp_path, plant=True, expected=["<LINE>", extra], capsys=capsys
    )
    assert status == 1
    assert out == f"MISSING {extra}\n"


def test_stale_expected_file_with_equal_templates_reports_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red proof 6: a list left over after the diff went away cannot pass silently."""
    stale = f"DIFF {CONTEXT} {API} $.Extra: base=1 head=2"
    status, out = expected_verdict(
        tmp_path, plant=False, expected=[stale], capsys=capsys
    )
    assert status == 1
    assert out == f"MISSING {stale}\n"


def test_missing_lines_are_capped_at_fifty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = [f"DIFF {CONTEXT} {API} $.X{i}: base=1 head=2" for i in range(60)]
    status, out = expected_verdict(tmp_path, plant=False, expected=stale, capsys=capsys)
    assert status == 1
    assert out.count("MISSING DIFF") == template_compare.MAX_DIFFS
    assert "... 10 more" in out


def test_read_expected_ignores_comments_and_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "e"
    path.write_text("# c\n\n  \nDIFF a\n")
    assert template_compare.read_expected(path) == ["DIFF a"]
