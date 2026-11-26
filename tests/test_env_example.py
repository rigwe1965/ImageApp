"""Every environment variable the code reads is documented in .env.example and in the README's table, and
nothing is documented that the code no longer reads. Add a setting: read it in the code, then add it to both."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"tests", "venv", ".venv", "node_modules", ".git", "__pycache__"}
READ = re.compile(r"""(?:os\.environ\.get|os\.getenv|os\.environ\[)\(?\s*["']([A-Z][A-Z0-9_]*)["']""")


def _code_reads():
    names = {}
    for path in ROOT.rglob("*.py"):
        if SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        for name in READ.findall(path.read_text(encoding="utf-8")):
            names.setdefault(name, str(path.relative_to(ROOT)))
    return names


def _example_names():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", text, flags=re.M))


def _readme_names():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\|\s*`([A-Z][A-Z0-9_]*)`\s*\|", text, flags=re.M))


def test_the_scan_finds_the_settings():
    assert {"SECRET_KEY", "MAX_BATCH_FILES", "RATELIMIT_DEFAULT", "FLASK_DEBUG"} <= set(_code_reads())


def test_every_setting_the_code_reads_is_in_env_example():
    missing = {n: f for n, f in _code_reads().items() if n not in _example_names()}
    assert not missing, f"add these to .env.example: {missing}"


def test_every_setting_the_code_reads_is_in_the_readme_table():
    missing = {n: f for n, f in _code_reads().items() if n not in _readme_names()}
    assert not missing, f"add these to the README's Configuration table: {missing}"


def test_nothing_documented_that_the_code_no_longer_reads():
    reads = set(_code_reads())
    assert not (_example_names() - reads), f"remove from .env.example: {_example_names() - reads}"
    assert not (_readme_names() - reads), f"remove from the README table: {_readme_names() - reads}"
