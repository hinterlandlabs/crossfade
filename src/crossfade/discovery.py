"""Repository discovery: an editable, evidence-labelled manifest of what an application is.

Static inspection can't understand business semantics, so every fact says how it was established
(observed file, static inference, declared configuration, assumption) and with what confidence.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path
from typing import Any

from .models import Evidence, Fact

SKIP = {".git", ".crossfade", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
LANG = {".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".go": "Go", ".rs": "Rust", ".java": "Java", ".rb": "Ruby"}
FRAMEWORKS = {"fastapi": "FastAPI", "flask": "Flask", "django": "Django", "express": "Express", "http.server": "Python http.server",
              "aiohttp": "aiohttp", "langchain": "LangChain", "langgraph": "LangGraph", "crossfade.harness": "Crossfade harness",
              "anthropic": "Anthropic SDK", "openai": "OpenAI SDK"}
STORAGE = {"sqlite3": "SQLite", "psycopg": "PostgreSQL", "psycopg2": "PostgreSQL", "pymongo": "MongoDB", "redis": "Redis", "sqlalchemy": "SQLAlchemy",
           "boto3": "AWS SDK"}
HTTP_CLIENTS = {"requests", "httpx", "urllib.request", "aiohttp"}
WRITE_VERBS = re.compile(r"\b(POST|PUT|PATCH|DELETE)\b")


def _files(root: Path) -> list[Path]:
    out = []
    for p in root.rglob("*"):
        if any(part in SKIP for part in p.relative_to(root).parts):
            continue
        if p.is_file():
            out.append(p)
    return out


def _py_facts(root: Path, f: Path) -> dict[str, Any]:
    rel = str(f.relative_to(root))
    info: dict[str, Any] = {"imports": set(), "env": set(), "urls": set(), "writes": [], "main": False, "routes": [], "otel": False}
    try:
        src = f.read_text(errors="replace")
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return info
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            info["imports"].update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            info["imports"].add(node.module)
        elif isinstance(node, ast.Call):
            fn = ast.unparse(node.func)
            if fn in ("os.environ.get", "os.getenv") and node.args and isinstance(node.args[0], ast.Constant):
                info["env"].add(node.args[0].value)
            for a in node.args[:2]:
                if isinstance(a, ast.Constant) and isinstance(a.value, str) and WRITE_VERBS.fullmatch(a.value):
                    info["writes"].append(f"{rel}:{node.lineno} {fn}({a.value} ...)")
        elif isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ" and isinstance(node.slice, ast.Constant):
            info["env"].add(node.slice.value)
        elif isinstance(node, ast.If) and "__name__" in ast.unparse(node.test) and "__main__" in ast.unparse(node.test):
            info["main"] = True
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith(("http://", "https://")):
            info["urls"].add(node.value)
        elif isinstance(node, ast.FunctionDef) and node.name.startswith("do_") and node.name[3:].isupper():
            info["routes"].append(node.name[3:])
    info["otel"] = any(i.startswith("opentelemetry") for i in info["imports"])
    return info


def inspect(root: Path) -> dict[str, Any]:
    root = Path(root).resolve()
    facts: list[Fact] = []
    files = _files(root)
    langs: dict[str, int] = {}
    for f in files:
        if f.suffix in LANG:
            langs[LANG[f.suffix]] = langs.get(LANG[f.suffix], 0) + 1
    for lang, n in sorted(langs.items(), key=lambda x: -x[1]):
        facts.append(Fact(key="language", value=lang, evidence=Evidence.OBSERVED, confidence=1.0, source=f"{n} files"))

    deps: list[str] = []
    if (root / "pyproject.toml").exists():
        data = tomllib.loads((root / "pyproject.toml").read_text())
        deps = data.get("project", {}).get("dependencies", [])
        facts.append(Fact(key="build", value="pyproject.toml", evidence=Evidence.OBSERVED, confidence=1.0, source="pyproject.toml"))
    if (root / "requirements.txt").exists():
        deps += [ln.strip() for ln in (root / "requirements.txt").read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    if (root / "package.json").exists():
        pkg = json.loads((root / "package.json").read_text())
        deps += list(pkg.get("dependencies", {}))
        for name, cmd in pkg.get("scripts", {}).items():
            facts.append(Fact(key="script", value=f"{name}: {cmd}", evidence=Evidence.OBSERVED, confidence=1.0, source="package.json"))
    for d in deps:
        facts.append(Fact(key="dependency", value=d, evidence=Evidence.DECLARED, confidence=1.0, source="manifest"))
    for name in ("Dockerfile", "docker-compose.yml", "compose.yaml"):
        if (root / name).exists():
            facts.append(Fact(key="container", value=name, evidence=Evidence.OBSERVED, confidence=1.0, source=name))

    tests = [str(f.relative_to(root)) for f in files if re.match(r"(test_.*|.*_test)\.py$", f.name) or "/tests/" in f"/{f.relative_to(root)}"]
    if tests:
        facts.append(Fact(key="tests", value=f"{len(tests)} test files", evidence=Evidence.OBSERVED, confidence=1.0, source=", ".join(tests[:5])))
    else:
        facts.append(Fact(key="tests", value="none found", evidence=Evidence.STATIC, confidence=0.7, source="file scan"))

    env, writes, otel = set(), [], []
    for f in files:
        if f.suffix != ".py" or f.parts[-2:-1] == ("crossfade",) and (root / "crossfade") in f.parents:
            continue
        rel = str(f.relative_to(root))
        info = _py_facts(root, f)
        if info["main"]:
            facts.append(Fact(key="entrypoint", value=f"python {rel}", evidence=Evidence.STATIC, confidence=0.8, source=f"{rel}: __main__ guard"))
        for imp in sorted(info["imports"]):
            top = imp.split(".")[0]
            for key, label in FRAMEWORKS.items():
                if imp == key or imp.startswith(key + "."):
                    facts.append(Fact(key="framework", value=label, evidence=Evidence.STATIC, confidence=0.9, source=rel))
            if top in STORAGE or imp in STORAGE:
                facts.append(Fact(key="storage", value=STORAGE.get(top, STORAGE.get(imp)), evidence=Evidence.STATIC, confidence=0.85, source=rel))
            if imp in HTTP_CLIENTS or top in HTTP_CLIENTS:
                facts.append(Fact(key="external_http", value=f"{imp} client", evidence=Evidence.STATIC, confidence=0.6,
                                  source=f"{rel}: makes outbound HTTP calls; which services and their semantics are unknown"))
        if info["routes"]:
            facts.append(Fact(key="http_methods_served", value=sorted(set(info["routes"])), evidence=Evidence.STATIC, confidence=0.7, source=rel))
        env |= info["env"]
        writes += info["writes"]
        if info["otel"]:
            otel.append(rel)
        for u in sorted(info["urls"]):
            facts.append(Fact(key="hardcoded_url", value=u, evidence=Evidence.STATIC, confidence=0.9, source=rel))
    for e in sorted(env):
        facts.append(Fact(key="env_var", value=e, evidence=Evidence.STATIC, confidence=0.9, source="os.environ access"))
    for w in writes:
        facts.append(Fact(key="possible_side_effect", value=w, evidence=Evidence.STATIC, confidence=0.5,
                          source="outbound write verb; confirm whether it changes external state"))
    facts.append(Fact(key="opentelemetry", value=otel or "not instrumented", evidence=Evidence.STATIC, confidence=0.9 if otel else 0.6, source="imports"))
    facts.append(Fact(key="business_semantics", value="unknown", evidence=Evidence.ASSUMED, confidence=0.0,
                      source="static inspection cannot establish what correct behavior is; write gates and scenarios"))

    manifest = {
        "root": str(root),
        "summary": {
            "languages": list(langs),
            "frameworks": sorted({f.value for f in facts if f.key == "framework"}),
            "storage": sorted({f.value for f in facts if f.key == "storage"}),
            "entrypoints": [f.value for f in facts if f.key == "entrypoint"],
            "env_vars": sorted(env),
            "outbound_http": any(f.key == "external_http" for f in facts),
            "possible_side_effects": len(writes),
            "instrumented": bool(otel),
        },
        "facts": [f.model_dump(mode="json") for f in facts],
    }
    return manifest
