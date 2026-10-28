"""
Module fingerprinting for N1 incremental runs.

Fingerprint = sha256 over:
  1. preprocessed token stream of the module body (no comments, no whitespace)
  2. sorted parameter name=value pairs for this instance
  3. sorted child module fingerprints

This means: changing a comment doesn't change the fingerprint, but changing a
macro definition (which changes the preprocessed tokens) does. A parameter
change ripples up through the hierarchy.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pyslang.ast import Compilation, VisitAction, SymbolKind
from pyslang.syntax import SyntaxTree
from pyslang.pyslang import Bag, SourceManager
from pyslang.parsing import PreprocessorOptions


def _module_tokens(syn_node) -> list[str]:
    """
    Walk a syntax node and collect every token that is not a comment or
    pure whitespace. The result is the preprocessed token stream.
    """
    tokens: list[str] = []

    def visit(node):
        k = str(node.kind) if hasattr(node, "kind") else ""
        if hasattr(node, "rawText"):
            # Token node — filter out trivia
            if (
                "Comment" not in k
                and "WhiteSpace" not in k
                and "EndOfFile" not in k
                and node.rawText.strip()
            ):
                tokens.append(node.rawText)
            return None  # tokens have no children to recurse into
        return VisitAction.Advance

    syn_node.visit(visit)
    return tokens


def _hash_tokens(tokens: list[str], params: dict[str, Any], child_fps: list[str]) -> str:
    """
    sha256("verimine/module_fp/v1\0" + JSON([tokens, sorted_params, sorted_child_fps]))
    """
    payload = json.dumps(
        [tokens, sorted(params.items()), sorted(child_fps)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    raw = b"verimine/module_fp/v1\x00" + payload.encode()
    return hashlib.sha256(raw).hexdigest()


def compute_fingerprints(
    files: list,
    top: str,
    include_dirs: list,
    defines: dict[str, str | None],
) -> dict[str, str]:
    """
    Return a mapping of instance_path -> fingerprint hex string.

    We re-use a fresh Compilation here so that fingerprints depend only on the
    preprocessed source, not on any analysis state.
    """
    sm = SourceManager()
    po = PreprocessorOptions()
    po.additionalIncludePaths = [str(d) for d in include_dirs]
    po.predefines = [
        f"{k}={v}" if v is not None else k
        for k, v in defines.items()
    ]
    trees = SyntaxTree.fromFiles([str(f) for f in files], sm, Bag([po]))
    if not isinstance(trees, (list, tuple)):
        trees = [trees]

    comp = Compilation(Bag([]))
    for tree in trees:
        comp.addSyntaxTree(tree)

    # Force elaboration so topInstances is populated
    top_instances = comp.getRoot().topInstances

    top_inst = None
    for inst in top_instances:
        if inst.name == top or inst.body.definition.name == top:
            top_inst = inst
            break
    if top_inst is None and top_instances:
        top_inst = top_instances[0]

    if top_inst is None:
        return {}

    # Cache definition tokens by module name (definition is shared, params differ per instance)
    _def_token_cache: dict[str, list[str]] = {}

    def _def_tokens(body) -> list[str]:
        defn = body.definition
        name = defn.name
        if name not in _def_token_cache:
            _def_token_cache[name] = _module_tokens(defn.syntax)
        return _def_token_cache[name]

    # Recurse depth-first, compute child fingerprints before parents
    fingerprints: dict[str, str] = {}

    def _walk(sym) -> str:
        path = sym.hierarchicalPath
        body = sym.body
        params: dict[str, Any] = {}
        for p in body.parameters:
            try:
                params[p.name] = str(p.value)
            except Exception:
                params[p.name] = None

        child_fps: list[str] = []

        def child_visitor(s):
            if str(s.kind) == "SymbolKind.InstanceBody":
                if s.hierarchicalPath != path:
                    return VisitAction.Skip
                return VisitAction.Advance
            if str(s.kind) == "SymbolKind.Instance":
                child_fp = _walk(s)
                child_fps.append(child_fp)
                return VisitAction.Skip
            return VisitAction.Skip

        sym.body.visit(child_visitor)

        tokens = _def_tokens(body)
        fp = _hash_tokens(tokens, params, child_fps)
        fingerprints[path] = fp
        return fp

    _walk(top_inst)
    return fingerprints
