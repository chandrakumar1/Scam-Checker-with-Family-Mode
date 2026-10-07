import ast
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# Packages that must never depend on a channel (Telegram, WhatsApp, ...).
PROTECTED_PACKAGES = ["analyzer", "replies"]
PROTECTED_FILES = sorted(p for name in PROTECTED_PACKAGES for p in (APP_DIR / name).rglob("*.py"))


def imported_modules(source: str, package: list[str]) -> set[str]:
    """Absolute names of everything `source` imports, as if it lived in `package`."""
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                prefix = ".".join(base + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            modules.add(prefix)
            modules.update(f"{prefix}.{alias.name}" for alias in node.names)
    return modules


def package_of(path: Path) -> list[str]:
    return ["app", *path.relative_to(APP_DIR).parent.parts]


def imports_channels(modules: set[str]) -> set[str]:
    return {m for m in modules if m == "app.channels" or m.startswith("app.channels.")}


def test_analyzer_files_found():
    names = {p.name for p in PROTECTED_FILES if p.parent.name == "analyzer"}
    assert {"rules.py", "llm.py", "prompts.py", "combine.py", "cli.py"} <= names


def test_replies_files_found():
    names = {p.name for p in PROTECTED_FILES if p.parent.name == "replies"}
    assert {"formatter.py", "templates.py", "language.py"} <= names


@pytest.mark.parametrize("path", PROTECTED_FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_protected_packages_do_not_import_channels(path):
    source = path.read_text(encoding="utf-8")
    assert not imports_channels(imported_modules(source, package_of(path)))
    assert "app.channels" not in source


def test_analyzer_does_not_depend_on_replies():
    # Replies sit on top of the analyzer, never the other way round.
    for path in (APP_DIR / "analyzer").rglob("*.py"):
        modules = imported_modules(path.read_text(encoding="utf-8"), package_of(path))
        assert not {m for m in modules if m == "app.replies" or m.startswith("app.replies.")}, path.name


@pytest.mark.parametrize(
    "source",
    [
        "import app.channels.telegram",
        "from app.channels import telegram",
        "from app import channels",
        "from ..channels import telegram",
        "from .. import channels",
    ],
)
def test_checker_catches_channel_imports(source):
    assert imports_channels(imported_modules(source, ["app", "analyzer"]))


def test_checker_ignores_unrelated_imports():
    assert not imports_channels(imported_modules("from app.replies import formatter\nimport httpx", ["app", "analyzer"]))


# --- Family Mode and the database layer ----------------------------------------------------------------------------
# These use an allowlist, which is stricter than a denylist: app/family cannot
# quietly gain a new dependency, only the ones named here.

FAMILY_FILES = sorted((APP_DIR / "family").rglob("*.py"))
DB_FILES = sorted((APP_DIR / "db").rglob("*.py"))
THIRD_PARTY_ALLOWED = {"sqlmodel", "sqlalchemy", "pydantic"}


def disallowed_imports(modules: set[str], allowed_app_prefixes: tuple[str, ...]) -> set[str]:
    """Imports that are not stdlib, not an allowed third-party package, and not an allowed app package."""
    bad = set()
    for module in modules:
        top = module.split(".")[0]
        if top == "app":
            if not any(module == prefix or module.startswith(prefix + ".") for prefix in allowed_app_prefixes):
                bad.add(module)
        elif top not in sys.stdlib_module_names and top not in THIRD_PARTY_ALLOWED:
            bad.add(module)
    return bad


def app_imports(path: Path) -> set[str]:
    modules = imported_modules(path.read_text(encoding="utf-8"), package_of(path))
    return {m for m in modules if m == "app" or m.startswith("app.")}


def test_family_and_db_files_found():
    assert {"types.py", "tokens.py", "policy.py", "repository.py", "service.py"} <= {p.name for p in FAMILY_FILES}
    assert {"engine.py", "models.py"} <= {p.name for p in DB_FILES}


@pytest.mark.parametrize("path", FAMILY_FILES, ids=lambda p: f"family/{p.name}")
def test_family_imports_only_itself_the_db_layer_stdlib_and_the_database_libraries(path):
    modules = imported_modules(path.read_text(encoding="utf-8"), package_of(path))
    assert disallowed_imports(modules, ("app.family", "app.db")) == set()


def test_family_imports_neither_the_analyzer_nor_the_channels():
    for path in FAMILY_FILES:
        imported = app_imports(path)
        for forbidden in ("app.analyzer", "app.channels"):
            assert not {m for m in imported if m == forbidden or m.startswith(forbidden + ".")}, (path.name, forbidden)
        source = path.read_text(encoding="utf-8")
        assert "app.analyzer" not in source and "app.channels" not in source, path.name


@pytest.mark.parametrize("path", DB_FILES, ids=lambda p: f"db/{p.name}")
def test_db_layer_imports_only_itself_stdlib_and_the_database_libraries(path):
    modules = imported_modules(path.read_text(encoding="utf-8"), package_of(path))
    # In particular never app.family: family depends on db, so the reverse would be a cycle.
    assert disallowed_imports(modules, ("app.db",)) == set()


def test_analyzer_knows_nothing_about_family_mode_or_the_database():
    for path in (APP_DIR / "analyzer").rglob("*.py"):
        assert not {m for m in app_imports(path) if m.startswith(("app.family", "app.db"))}, path.name


def test_replies_may_use_the_family_types_but_nothing_else_from_family_or_db():
    for path in (APP_DIR / "replies").rglob("*.py"):
        imported = app_imports(path)
        assert not {m for m in imported if m.startswith("app.db")}, path.name
        family = {m for m in imported if m.startswith("app.family")}
        # `from app.family.types import X` is recorded as both app.family.types and app.family.types.X.
        assert all(m in ("app.family", "app.family.types") or m.startswith("app.family.types.") for m in family), (path.name, family)


def test_family_types_is_a_leaf_that_depends_only_on_the_db_enums():
    # AlertSummary lives here; if this module could import the analyzer (or any
    # sibling), analysis-shaped data could become part of it.
    imported = app_imports(APP_DIR / "family" / "types.py")
    assert imported, "expected types.py to import the status enums from app.db.models"
    assert all(m in ("app.db", "app.db.models") or m.startswith("app.db.models.") for m in imported), imported


@pytest.mark.parametrize(
    ("modules", "allowed", "bad"),
    [
        ({"app.analyzer.combine"}, ("app.family", "app.db"), {"app.analyzer.combine"}),
        ({"app.channels.telegram"}, ("app.family", "app.db"), {"app.channels.telegram"}),
        ({"app.replies.formatter"}, ("app.family", "app.db"), {"app.replies.formatter"}),
        ({"app.config"}, ("app.family", "app.db"), {"app.config"}),
        ({"app.family.service"}, ("app.db",), {"app.family.service"}),
        ({"httpx", "anthropic"}, ("app.family", "app.db"), {"httpx", "anthropic"}),
        ({"app.familyx"}, ("app.family",), {"app.familyx"}),  # a prefix match must respect the dot
        (
            {"app.family.tokens", "app.db.models", "hashlib", "sqlmodel", "sqlalchemy.orm", "pydantic"},
            ("app.family", "app.db"),
            set(),
        ),
    ],
)
def test_allowlist_checker(modules, allowed, bad):
    assert disallowed_imports(modules, allowed) == bad
