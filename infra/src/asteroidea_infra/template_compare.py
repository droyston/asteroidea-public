"""Compare two sets of synthesized templates, ignoring asset hashes.

Pure Python, no subprocess: `scripts/gates/templates.sh` does the git worktree and the
synth, this module does the comparison. It proves the infrastructure templates are
unchanged apart from asset hashes. It proves nothing about the Lambda code inside an
asset, nor about an image `CMD` when the function has no `ImageConfig`.

Normaliser: a 64-hex token is replaced only in four places: `Code.S3Key`, the
`Code.ImageUri` string, `Metadata."aws:asset:path"` and `TemplateURL`. One numbering
covers every file of a context: tokens are numbered by first appearance, walking the
files in file-name order, each as a key-sorted dump. An asset shared by two stacks
therefore stays shared on both sides, and a swap of one stack's image shows up.
Everything else is compared exactly, list order included.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, Final

type Node = Any
type Where = tuple[Any, ...]

HEX64: Final = re.compile(r"[0-9a-f]{64}")
MAX_DIFFS: Final = 50
ARGC: Final = 2
ABSENT: Final = object()
VALUE_WIDTH: Final = 100
USAGE: Final = (
    "usage: template_compare <base-dir> <head-dir> [--expected <file>]"
    " (one directory per context)"
)


def _hashed_place(path: Where) -> bool:
    """Return whether `path` is one of the four places an asset hash lives."""
    keys = [p for p in path if isinstance(p, str)]
    if "TemplateURL" in keys or "ImageUri" in keys:
        return True
    if keys[-1:] == ["S3Key"] and "Code" in keys:
        return True
    return keys[-1:] == ["aws:asset:path"]


def _normalise_node(node: Node, path: Where, numbers: dict[str, int]) -> Node:
    if isinstance(node, dict):
        return {
            k: _normalise_node(node[k], (*path, k), numbers)
            for k in sorted(node, key=str)
        }
    if isinstance(node, list):
        return [
            _normalise_node(item, (*path, i), numbers) for i, item in enumerate(node)
        ]
    if isinstance(node, str) and _hashed_place(path):

        def number(match: re.Match[str]) -> str:
            """Return a stable `<asset:N>` placeholder for the matched 64-hex hash."""
            return f"<asset:{numbers.setdefault(match.group(0), len(numbers) + 1)}>"

        return HEX64.sub(number, node)
    return node


def normalise(templates: dict[str, Node]) -> dict[str, Node]:
    """Return the templates by file name, asset hashes numbered across all files."""
    numbers: dict[str, int] = {}
    return {
        name: _normalise_node(templates[name], (), numbers)
        for name in sorted(templates, key=str)
    }


def _show(value: Node) -> str:
    text = "<absent>" if value is ABSENT else json.dumps(value, sort_keys=True)
    return text if len(text) <= VALUE_WIDTH else text[: VALUE_WIDTH - 3] + "..."


def _where(path: Where) -> str:
    return "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in path)


def _walk(
    base: Node, head: Node, path: Where, out: list[tuple[Where, Node, Node]]
) -> None:
    if isinstance(base, dict) and isinstance(head, dict):
        for key in sorted(base.keys() | head.keys(), key=str):
            _walk(base.get(key, ABSENT), head.get(key, ABSENT), (*path, key), out)
    elif isinstance(base, list) and isinstance(head, list):
        for i in range(max(len(base), len(head))):
            _walk(
                base[i] if i < len(base) else ABSENT,
                head[i] if i < len(head) else ABSENT,
                (*path, i),
                out,
            )
    elif (
        base is ABSENT or head is ABSENT or base != head or type(base) is not type(head)
    ):
        out.append((path, base, head))


def differences(
    context: str, base: dict[str, Node], head: dict[str, Node]
) -> list[str]:
    """Return one `DIFF <context> <file> <path>: base=<v> head=<v>` line each."""
    lines: list[str] = []
    for name in sorted(base.keys() | head.keys(), key=str):
        found: list[tuple[Where, Node, Node]] = []
        _walk(base.get(name, ABSENT), head.get(name, ABSENT), (), found)
        lines.extend(
            f"DIFF {context} {name} {_where(p)}: base={_show(x)} head={_show(y)}"
            for p, x, y in found
        )
    return lines


def _load(directory: Path) -> dict[str, Node]:
    if not directory.is_dir():
        return {}
    return {
        p.name: json.loads(p.read_text())
        for p in sorted(directory.glob("*.template.json"))
    }


def compare_roots(base_root: Path, head_root: Path) -> tuple[list[str], int, int]:
    """Compare every context directory under two roots.

    Returns the DIFF lines, the file count of the first context and the context count.
    """
    contexts = sorted(
        {p.name for r in (base_root, head_root) for p in r.iterdir() if p.is_dir()}
    )
    lines: list[str] = []
    files = 0
    for context in contexts:
        base = normalise(_load(base_root / context))
        head = normalise(_load(head_root / context))
        files = files or len(base.keys() | head.keys())
        lines.extend(differences(context, base, head))
    return lines, files, len(contexts)


def read_expected(path: Path) -> list[str]:
    """Return the file DIFF lines; blank and `#` lines are ignored."""
    return [
        line
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def check_expected(
    actual: list[str], expected: list[str]
) -> tuple[list[str], list[str]]:
    """Return (unexpected, missing): actual not expected, expected not seen."""
    seen, wanted = set(actual), set(expected)
    return (
        [line for line in actual if line not in wanted],
        [line for line in dict.fromkeys(expected) if line not in seen],
    )


def _print_capped(prefix: str, lines: list[str]) -> None:
    sys.stdout.write("".join(f"{prefix}{line}\n" for line in lines[:MAX_DIFFS]))
    if len(lines) > MAX_DIFFS:
        sys.stdout.write(f"... {len(lines) - MAX_DIFFS} more\n")


def _verdict(lines: list[str], files: int, contexts: int, expected: list[str]) -> int:
    unexpected, missing = check_expected(lines, expected)
    if unexpected or missing:
        _print_capped("UNEXPECTED ", unexpected)
        _print_capped("MISSING ", missing)
        return 1
    if lines:
        sys.stdout.write(f"templates differ as expected: {len(lines)} lines\n")
    else:
        sys.stdout.write(f"templates equal: {files} files x {contexts} contexts\n")
    return 0


def main(argv: list[str]) -> int:
    """Print the verdict for two roots; exit 0 equal or as expected, 1 differ, 2 usage.

    `--expected <file>` (after the two directories) makes the DIFF lines pass when they
    equal the file's lines as a set (`infra/template-diff.expected`, passed by the
    shell script only when a PR changes it). Without it, any DIFF line fails.
    """
    expected_path: Path | None = None
    if len(argv) == ARGC + 2 and argv[ARGC] == "--expected":
        expected_path = Path(argv[ARGC + 1])
        argv = argv[:ARGC]
    if len(argv) != ARGC:
        sys.stderr.write(USAGE + "\n")
        return 2
    lines, files, contexts = compare_roots(Path(argv[0]), Path(argv[1]))
    expected = read_expected(expected_path) if expected_path else []
    if not expected and lines:
        # Plain mode: the old output, the DIFF lines themselves.
        _print_capped("", lines)
        return 1
    return _verdict(lines, files, contexts, expected)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
