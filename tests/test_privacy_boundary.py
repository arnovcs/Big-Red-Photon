"""Module boundaries for private data (§11), checked statically over app/.

- Only planning/pipeline.py and delivery/itinerary.py may READ from app.private.
- Only onboarding/fsm.py may call the vault WRITE functions.
- Only app/private/ touches the private_profiles table.
- The optimizer stays pure: no I/O, database, network, or LLM imports.
"""

import ast
import inspect
from pathlib import Path

from app.private import vault

APP = Path(__file__).resolve().parents[1] / "app"

READ_API = {"constraints_for", "itinerary_context_for", "guard_secrets_for"}
WRITE_API = {
    "link_customer",
    "set_limit",
    "set_origin",
    "clear_origin",
    "confirm_origin",
    "set_modes",
    "set_drive",
}
ALLOWED = {
    "planning/pipeline.py": READ_API,
    "delivery/itinerary.py": READ_API,
    "onboarding/fsm.py": WRITE_API,
}
OPTIMIZER_FORBIDDEN = (
    "sqlalchemy",
    "httpx",
    "google",
    "app.db",
    "app.providers",
    "app.private",
    "app.messaging",
    "app.deps",
    "app.planning",
    "asyncio",
)


def _modules() -> list[tuple[str, ast.Module]]:
    out = []
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        out.append((rel, ast.parse(path.read_text(encoding="utf-8"))))
    return out


def _private_usage(tree: ast.Module) -> tuple[bool, set[str]]:
    """(imports app.private?, names used from it)."""
    imported = False
    bound: set[str] = set()  # local names bound to a private module
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "app" and any(a.name == "private" for a in node.names):
                imported = True
                bound.update(a.asname or a.name for a in node.names if a.name == "private")
            elif node.module == "app.private":
                imported = True
                bound.update(a.asname or a.name for a in node.names)
            elif node.module.startswith("app.private."):
                imported = True
                used.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app.private"):
                    imported = True
                    bound.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            owner = ast.unparse(node.value)
            if owner in bound:
                used.add(node.attr)
    return imported, used


def test_vault_api_is_fully_classified() -> None:
    public = {
        name
        for name, fn in inspect.getmembers(vault, inspect.iscoroutinefunction)
        if not name.startswith("_") and fn.__module__ == vault.__name__
    }
    assert public == READ_API | WRITE_API, "classify new vault functions as read or write"


def test_only_allowed_modules_use_app_private_and_only_their_functions() -> None:
    for rel, tree in _modules():
        if rel.startswith("private/"):
            continue
        imported, used = _private_usage(tree)
        if not imported:
            continue
        assert rel in ALLOWED, f"{rel} must not import app.private"
        extra = used - ALLOWED[rel]
        assert not extra, f"{rel} uses {sorted(extra)} from app.private"


def test_private_profiles_table_only_used_in_app_private() -> None:
    for rel, tree in _modules():
        if rel.startswith("private/") or rel == "db/tables.py":
            continue
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "PrivateProfileRow" not in names, f"{rel} touches private_profiles"


def test_optimizer_is_pure() -> None:
    for rel, tree in _modules():
        if not rel.startswith("optimizer/"):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            else:
                continue
            for module in modules:
                assert not module.startswith(OPTIMIZER_FORBIDDEN), f"{rel} imports {module}"
