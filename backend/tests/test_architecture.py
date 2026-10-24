"""Small dependency guards for the boundaries that make the monolith maintainable."""

import ast

from app.configuration.paths import PROJECT_ROOT

APP = PROJECT_ROOT / "backend/app"


def test_domain_has_no_runtime_or_outer_layer_dependencies():
    forbidden = {"fastapi", "httpx", "sqlite3", "subprocess", "multiprocessing", "pathlib", "os"}
    violations = []
    for path in APP.rglob("*.py"):
        if "domain" not in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            modules = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else ([alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            )
            for module in modules:
                if module.split(".")[0] in forbidden or any(
                    part in module.split(".")
                    for part in ("application", "infrastructure", "interfaces", "bootstrap")
                ):
                    violations.append(f"{path.relative_to(APP)}:{node.lineno}: {module}")
    assert not violations, "\n".join(violations)


def test_http_routes_delegate_persistence_to_application_services():
    violations = []
    for path in (APP / "interfaces/http").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in {
                "execute",
                "executemany",
                "transaction",
                "connect",
            }:
                violations.append(f"{path.relative_to(APP)}:{node.lineno}: {node.attr}")
    assert not violations, "\n".join(violations)


def test_top_level_is_only_composition_and_stable_entrypoints():
    assert {path.name for path in APP.glob("*.py")} == {"__init__.py", "main.py", "bootstrap.py"}
    assert {p.name for p in (APP / "contexts").iterdir() if (p / "__init__.py").is_file()} == {
        "accounts",
        "workflows",
        "conversations",
        "documents",
        "ingestion",
        "legal_analysis",
        "drafting",
    }
