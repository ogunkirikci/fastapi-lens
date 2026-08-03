from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[1]
TEXT_EXTENSIONS = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".yaml",
    ".yml",
}
SCANNED_DIRECTORIES = (
    ".github",
    "benchmarks",
    "docs",
    "examples",
    "src",
    "tests",
)
TURKISH_SPECIFIC_CHARACTERS = frozenset(
    chr(codepoint)
    for codepoint in (
        0x00C7,
        0x00D6,
        0x00DC,
        0x00E7,
        0x00F6,
        0x00FC,
        0x011E,
        0x011F,
        0x0130,
        0x0131,
        0x015E,
        0x015F,
    )
)


def repository_text_files() -> list[Path]:
    files = [
        REPOSITORY_ROOT / name
        for name in (
            "AGENTS.md",
            "CHANGELOG.md",
            "CONTRIBUTING.md",
            "README.md",
            "pyproject.toml",
        )
    ]
    for directory_name in SCANNED_DIRECTORIES:
        directory = REPOSITORY_ROOT / directory_name
        if not directory.exists():
            continue
        files.extend(
            path
            for path in directory.rglob("*")
            if path.is_file() and path.suffix in TEXT_EXTENSIONS
        )
    return sorted(files)


@pytest.mark.parametrize(
    "path",
    repository_text_files(),
    ids=lambda path: str(path.relative_to(REPOSITORY_ROOT)),
)
def test_repository_text_uses_no_turkish_specific_characters(path: Path) -> None:
    content = path.read_text(encoding="utf-8")

    found = TURKISH_SPECIFIC_CHARACTERS.intersection(content)

    assert not found, f"{path} contains disallowed characters: {sorted(found)}"
