"""Shared fixtures for the understand-docs script suites.

The scripts live under ``understand-anything-plugin/skills/understand-docs/`` —
a hyphenated path — so they cannot be imported with an ``import`` statement.
Each is loaded once per session via importlib (same pattern as
``tests/skill/understand/test_merge_batch_graphs.py``) and handed to tests as a
module fixture.

Corpus builders write the small JSON artifacts the pipeline consumes (mirror
files, bundles, verdict passes) into ``tmp_path``. They are helpers, not tests:
no Given-When-Then docstrings here by convention.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPTS_DIR = _REPO_ROOT / "understand-anything-plugin" / "skills" / "understand-docs"


def load_script(stem: str) -> Any:
    """Load ``<stem>.py`` from the understand-docs skill directory."""
    path = SCRIPTS_DIR / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Register so dataclasses/typing introspection inside the module resolves.
    sys.modules[stem] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def reconcile_verdicts() -> Any:
    return load_script("reconcile_verdicts")


@pytest.fixture(scope="session")
def reconcile_claims() -> Any:
    return load_script("reconcile_claims")


@pytest.fixture(scope="session")
def lint_claims() -> Any:
    return load_script("lint_claims")


@pytest.fixture(scope="session")
def build_grounding_bundles() -> Any:
    return load_script("build_grounding_bundles")


@pytest.fixture(scope="session")
def build_tiebreak_bundle() -> Any:
    return load_script("build_tiebreak_bundle")


@pytest.fixture(scope="session")
def strip_docstrings() -> Any:
    return load_script("strip_docstrings")


@pytest.fixture(scope="session")
def design_lens() -> Any:
    return load_script("design_lens")


@pytest.fixture(scope="session")
def emit_claim_graph() -> Any:
    return load_script("emit_claim_graph")


@pytest.fixture(scope="session")
def prompt_invariants() -> Any:
    return load_script("test_prompts")


# ── Corpus builders ───────────────────────────────────────────────────────

MIRROR_SOURCE = """\
import asyncio


class Pool:
    def spawn(self, count):
        if count == 0:
            count = cpu_count()
        return [Worker() for _ in range(count)]

    def stop(self, timeout):
        for worker in self._workers:
            worker.cancel(timeout)
        return True


def cpu_count():
    ...
"""


@pytest.fixture()
def mirror(tmp_path: Path) -> Path:
    """A one-file stripped mirror with known content at known line numbers."""
    root = tmp_path / "mirror"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "pool.py").write_text(MIRROR_SOURCE)
    return root


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1))
    return path


def make_bundle(
    out_dir: Path,
    label: str,
    claims: list[dict],
    *,
    source_file: str = "pkg/pool.py",
    qualname: str = "Pool.spawn",
    target: str = "direct",
) -> Path:
    """Write a minimal ground-<label>.json bundle wrapping the given claims."""
    return write_json(out_dir / f"ground-{label}.json", {
        "bundle": f"ground-{label}.json",
        "schemaVersion": "0.1.0",
        "subsystem": label,
        "claimCount": len(claims),
        "bundleCount": 1,
        "claimsByTarget": {target: len(claims)},
        "docUnits": 1,
        "units": [{
            "docUnitId": f"docunit:{label}",
            "sourceFile": source_file,
            "attachedTo": f"function:{source_file}:{qualname}",
            "attachedKind": "function",
            "qualname": qualname,
            "declLine": 5,
            "docLineRange": [6, 7],
            "groundingTarget": target,
            "groundingReason": "symbol has an executable body",
            "candidateSites": [],
            "claims": claims,
        }],
    })


def make_claim(cid: str, text: str, *, quantifier: str = "particular") -> dict:
    return {
        "claimId": f"claim:{cid}",
        "claimText": text,
        "claimType": "factual",
        "passCount": 5,
        "quantifier": quantifier,
        "sourceQuote": text,
    }


def evidence(lines: list[int], code: str, role: str = "supports",
             file: str = "pkg/pool.py") -> dict:
    return {"file": file, "lines": lines, "code": code, "role": role}


def make_pass(out_dir: Path, name: str, verdicts: list[dict]) -> Path:
    """Write one grounding pass file containing the given verdict entries."""
    return write_json(out_dir / f"{name}.json", {
        "bundle": "ground-x.json", "pass": 1, "verdicts": verdicts,
    })


def verdict_entry(cid: str, verdict: str, ev: list[dict] | None = None,
                  overstatement: dict | None = None,
                  scope_note: dict | None = None) -> dict:
    entry: dict = {
        "claimId": f"claim:{cid}",
        "verdict": verdict,
        "confidence": "high",
        "evidence": ev or [],
        "reasoning": "because the code says so",
        "scopeChecked": None,
        "searchedFor": None,
    }
    if overstatement is not None:
        entry["overstatement"] = overstatement
    if scope_note is not None:
        entry["scopeNote"] = scope_note
    return entry


# ── design_lens / emit_claim_graph corpora ────────────────────────────────

DOC_UNIT_ID = "docunit:u1"
CLAIM_ID = "claim:c1"


def make_claims_file(path: Path, claims: list[dict]) -> Path:
    """A reconciled claims file, the positional input to design_lens."""
    return write_json(path, {"schemaVersion": "0.1.0",
                             "stats": {"subsystem": "layer:pool"},
                             "claims": claims})


def reconciled_claim(cid: str = CLAIM_ID, text: str = "Pool.spawn spawns workers.",
                     unit: str = DOC_UNIT_ID) -> dict:
    return {"id": cid, "docUnitId": unit, "claimText": text,
            "claimType": "factual", "quantifier": "particular",
            "passCount": 5, "sourceQuote": text, "fieldIndex": None,
            "variants": [text]}


def make_grounded_file(path: Path, claims: list[dict]) -> Path:
    """A grounded-*.json, the --grounded input to design_lens."""
    return write_json(path, {"schemaVersion": "0.1.0", "stats": {},
                             "claims": claims})


def grounded_claim(cid: str = CLAIM_ID, verdict: str = "supported",
                   ev: list[dict] | None = None,
                   scope_note: dict | None = None) -> dict:
    entry = {"claimId": cid, "verdict": verdict, "agreement": "unanimous",
             "evidence": ev if ev is not None else [
                 {"file": "pkg/pool.py", "lines": [6, 7], "role": "supports"}],
             "claimText": "Pool.spawn spawns workers."}
    if scope_note is not None:
        entry["scopeNote"] = scope_note
    return entry


def make_doc_units(path: Path, units: list[dict] | None = None) -> Path:
    return write_json(path, {"docUnits": units or [{
        "id": DOC_UNIT_ID,
        "sourceFile": "pkg/pool.py",
        "sourceLine": 5,
        "docLineRange": [6, 7],
        "attachedKind": "function",
        "attachedTo": "function:pkg/pool.py:Pool.spawn",
        "text": "Spawns workers.",
    }]})


def make_graph(path: Path) -> Path:
    """A knowledge graph with one file node, enough for claim linkage."""
    return write_json(path, {
        "version": "1.1.0",
        "project": {"name": "t", "languages": ["python"], "frameworks": [],
                    "description": "t", "analyzedAt": "2026-01-01T00:00:00Z",
                    "gitCommitHash": "abc"},
        "nodes": [{"id": "file:pkg/pool.py", "type": "file", "name": "pool.py",
                   "filePath": "pkg/pool.py", "summary": "Pool", "tags": [],
                   "complexity": "simple"}],
        "edges": [], "layers": [], "tour": [],
    })


def make_design_claims(path: Path, claims: list[dict]) -> Path:
    """A design-claims.json, the --design-claims input to emit_claim_graph."""
    return write_json(path, {"schemaVersion": "0.1.0", "stats": {},
                             "claims": claims})


def design_claim(cid: str = CLAIM_ID, grounding: dict | None = None,
                 text: str = "Pool.spawn spawns workers.",
                 unit: str = DOC_UNIT_ID, passes: int = 5) -> dict:
    return {"claimId": cid, "claimText": text, "claimType": "factual",
            "quantifier": "particular", "passCount": passes, "docUnitId": unit,
            "subsystem": "pool", "roles": ["mechanism"], "symbols": ["Pool"],
            "grounding": grounding}
