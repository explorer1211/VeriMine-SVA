"""N4 path C: LLM proposals, optional (`llm.mode`, default off). The model is an enhancement
layer: with it off, path A runs in full.

  1  input: a structural summary per instance only (module header, ports, VDG neighborhood,
     state graph, role table), never a source file
  2  output: JSON constrained by OUTPUT_SCHEMA (template instances or propositions as
     expression trees); free SVA text is not accepted
  3  the prompt asks for implementation-independent, observable properties
  4  everything returned goes through the same pool and later N6/N7 like any other candidate
  5  data levels (spec "LLM 合规模式"): L0 (files, real names) never leaves; L1 masks signal
     names (the map stays local); L2 is the structural summary. Default L1 + L2: names masked.
     Foreign cloud endpoints are off unless `run.params.n4_llm_allow_foreign` is true

Every call is written to the audit table (input hash, output, adopted ids), exportable.

Providers:
  openai     OpenAI-compatible chat endpoint (vLLM for private deployment, domestic clouds):
             URL in VERIMINE_LLM_URL, key in VERIMINE_LLM_KEY
  replay     answers read from a local JSON file (VERIMINE_LLM_REPLAY): {"<module>": response},
             for tests and offline review of earlier answers
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

from verimine.hashing import tagged_hash
from verimine.n2b.props import EQ, const, ref
from verimine.n4.ir import Cand
from verimine.n4.templates import Scope

PROMPT_SET_VERSION = "n4c-1"
OPS = {"!", "~", "&&", "||", "==", "!=", "<", "<=", ">", ">=", "&", "|", "^", "+", "-", "$past"}
TEMPLATES = {"mutex", "onehot", "handshake_hold", "implies"}
OUTPUT_SCHEMA = {
    "type": "object", "required": ["candidates"], "additionalProperties": False,
    "properties": {"candidates": {"type": "array", "maxItems": 64, "items": {
        "type": "object", "required": ["kind"], "additionalProperties": False,
        "properties": {
            "kind": {"enum": ["template", "prop"]},
            "template": {"enum": sorted(TEMPLATES)},
            "signals": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
            "ante": {"$ref": "#/$defs/expr"}, "cons": {"$ref": "#/$defs/expr"},
            "next": {"type": "boolean"}, "why": {"type": "string", "maxLength": 300},
        }}}},
    "$defs": {"expr": {"oneOf": [
        {"type": "object", "required": ["sig"], "properties": {"sig": {"type": "string"}}, "additionalProperties": False},
        {"type": "object", "required": ["const"], "properties": {"const": {"type": "integer", "minimum": 0}},
         "additionalProperties": False},
        {"type": "object", "required": ["op", "args"], "additionalProperties": False,
         "properties": {"op": {"enum": sorted(OPS)}, "args": {"type": "array", "items": {"$ref": "#/$defs/expr"},
                                                               "minItems": 1, "maxItems": 2}}},
    ]}},
}
SYSTEM = (
    "你是硬件验证助手。根据给出的模块结构摘要，提出可以写成断言的性质。要求：只提与实现无关、"
    "在端口或给出的信号上可观察的性质；不要猜测摘要中没有的信号；只输出符合给定 JSON schema 的 JSON，"
    "不要输出 SVA 文本或其他说明。信号名是脱敏后的代号，原样使用。"
)
FOREIGN_HINTS = ("openai.com", "anthropic.com", "googleapis.com", "azure.com", "amazonaws.com")


class LlmError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Input: L2 structural summary with L1 masking
# --------------------------------------------------------------------------- #

def summary(n2, sc: Scope, widths: dict[str, int]) -> tuple[dict, dict[str, str]]:
    """(masked summary, code -> real name). Codes: p0.. for ports, s0.. for internal signals."""
    inst = sc.inst
    mask: dict[str, str] = {}
    for i, p in enumerate(p for p in inst.ports if not p.is_interface):
        mask[p.name] = f"p{i}"
    fsms = [f for f in n2.fsms if f.inst == inst.path]
    interesting = {f.state for f in fsms} | {p.signal for p in n2.patterns if p.inst == inst.path}
    for i, n in enumerate(sorted(interesting - set(mask))):
        mask[n] = f"s{i}"
    m = mask.get
    roles = []
    for r in n2.roles:
        if r.inst == inst.path and r.name in mask and r.roles:
            roles.append({"signal": m(r.name), "roles": [x.role for x in r.roles if x.evidence_kind != "naming"]})
    pre = inst.path + "."
    neigh = []
    for e in n2.vdg.edges:
        if e.src.startswith(pre) and e.dst.startswith(pre):
            a, b = e.src[len(pre):], e.dst[len(pre):]
            if a in mask and b in mask and a != b:
                neigh.append([m(a), m(b), e.kind, e.delay])
    data = {
        "ports": [{"name": m(p.name), "dir": p.direction, "width": p.width or 1} for p in inst.ports
                  if p.name in mask],
        "signals": [{"name": m(n), "width": widths.get(n, 1)} for n in sorted(interesting) if n in mask
                    and n not in sc.ports],
        "clock": m(sc.clock["name"]) if sc.clock else None,
        "resets": [{"name": m(r), "active": lv} for r, lv in sorted(sc.resets.items()) if r in mask],
        "vdg": sorted(neigh),
        "fsms": [{"state": m(f.state), "values": [s["value"] for s in f.states], "reset": f.reset_state,
                  "transitions": sorted({(t["from"], t["to"]) for t in f.transitions})} for f in fsms],
        "roles": roles,
    }
    return data, {v: k for k, v in mask.items()}


def prompt(data: dict) -> list[dict]:
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "结构摘要：\n" + json.dumps(data, ensure_ascii=False, sort_keys=True)
             + "\n\n输出 JSON schema：\n" + json.dumps(OUTPUT_SCHEMA, ensure_ascii=False, sort_keys=True)}]


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #

def call(llm: dict, params: dict, module: str, messages: list[dict]) -> str:
    provider = (llm.get("provider") or "").lower()
    if provider == "replay":
        path = os.environ.get("VERIMINE_LLM_REPLAY")
        if not path or not os.path.exists(path):
            raise LlmError("provider=replay 需要 VERIMINE_LLM_REPLAY 指向回答文件")
        answers = json.loads(open(path, encoding="utf-8").read())
        if module not in answers:
            raise LlmError(f"回答文件里没有模块 {module}")
        a = answers[module]
        return a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)
    if provider in ("openai", "vllm", ""):
        url = os.environ.get("VERIMINE_LLM_URL")
        if not url:
            raise LlmError("需要 VERIMINE_LLM_URL（OpenAI 兼容接口，例如私有部署的 vLLM）")
        if any(h in url for h in FOREIGN_HINTS) and not params.get("n4_llm_allow_foreign"):
            raise LlmError(f"{url} 看起来是境外云端：默认禁用，需显式设置 run.params.n4_llm_allow_foreign")
        body = json.dumps({"model": llm.get("model") or "", "messages": messages, "temperature": 0,
                           "response_format": {"type": "json_object"}}).encode()
        req = urllib.request.Request(url.rstrip("/") + "/chat/completions", body,
                                     {"Content-Type": "application/json",
                                      "Authorization": f"Bearer {os.environ.get('VERIMINE_LLM_KEY', '')}"})
        try:
            with urllib.request.urlopen(req, timeout=int(params.get("n4_llm_timeout", 120))) as r:
                return json.loads(r.read())["choices"][0]["message"]["content"]
        except Exception as e:  # network, HTTP, malformed answer
            raise LlmError(f"调用失败：{e}") from e
    raise LlmError(f"不支持的 provider：{provider}")


# --------------------------------------------------------------------------- #
# Output: validation against the schema and the summary, unmasking
# --------------------------------------------------------------------------- #

def _expr(t, names: dict[str, str], widths: dict[str, int]) -> dict:
    if not isinstance(t, dict):
        raise LlmError("表达式不是对象")
    if set(t) == {"sig"}:
        if t["sig"] not in names:
            raise LlmError(f"信号 {t['sig']} 不在摘要中")
        return ref(names[t["sig"]])
    if set(t) == {"const"}:
        if not isinstance(t["const"], int) or isinstance(t["const"], bool) or t["const"] < 0:
            raise LlmError("常量必须是非负整数")
        return const(t["const"])
    if set(t) == {"op", "args"}:
        if t["op"] not in OPS or not isinstance(t["args"], list) or not 1 <= len(t["args"]) <= 2:
            raise LlmError(f"不允许的运算 {t.get('op')}")
        unary = t["op"] in ("!", "~", "$past")
        if unary != (len(t["args"]) == 1):
            raise LlmError(f"运算 {t['op']} 的参数个数不对")
        return {"op": t["op"], "args": [_expr(a, names, widths) for a in t["args"]]}
    raise LlmError("表达式字段不符合 schema")


def parse(text: str, sc: Scope, names: dict[str, str], widths: dict[str, int]) -> tuple[list[Cand], list[str]]:
    """(candidates, rejected item messages). The whole answer is rejected if it is not JSON."""
    try:
        data = json.loads(re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M))
    except json.JSONDecodeError as e:
        raise LlmError(f"回答不是 JSON：{e}") from e
    if not isinstance(data, dict) or set(data) != {"candidates"} or not isinstance(data["candidates"], list):
        raise LlmError("回答不符合 schema：顶层必须只有 candidates 数组")
    out, bad = [], []
    for k, item in enumerate(data["candidates"][:64]):
        try:
            if not isinstance(item, dict) or not set(item) <= set(OUTPUT_SCHEMA["properties"]["candidates"]["items"]["properties"]):
                raise LlmError("字段不符合 schema")
            why = [f"LLM：{item.get('why', '')}".rstrip("：")]
            if item.get("kind") == "template":
                sig = [names.get(s) for s in item.get("signals", [])]
                if None in sig or not sig:
                    raise LlmError("模板信号不在摘要中")
                t = item.get("template")
                if t == "mutex" and len(sig) >= 2 and all(widths.get(s) == 1 for s in sig):
                    pairs = [{"op": "!", "args": [{"op": "&&", "args": [ref(a), ref(b)]}]}
                             for i, a in enumerate(sig) for b in sig[i + 1:]]
                    cons = pairs[0]
                    for p in pairs[1:]:
                        cons = {"op": "&&", "args": [cons, p]}
                    out.append(sc.cand("mutex", None, cons, evidence=why))
                elif t == "onehot" and len(sig) == 1 and widths.get(sig[0], 1) > 1:
                    s = ref(sig[0])
                    out.append(sc.cand("onehot", None, EQ({"op": "&", "args": [s, {"op": "-", "args": [s, const(1)]}]},
                                                         const(0)), evidence=why))
                elif t == "handshake_hold" and len(sig) == 2:
                    v, r = ref(sig[0]), ref(sig[1])
                    out.append(sc.cand("handshake_hold", {"op": "&&", "args": [v, {"op": "!", "args": [r]}]}, v, True,
                                       why))
                elif t == "implies" and len(sig) == 2:
                    out.append(sc.cand("implies", ref(sig[0]), ref(sig[1]), False, why))
                else:
                    raise LlmError(f"模板 {t} 的信号不合适")
            elif item.get("kind") == "prop":
                cons = _expr(item.get("cons"), names, widths)
                ante = _expr(item["ante"], names, widths) if item.get("ante") is not None else None
                nxt = item.get("next", False)
                if not isinstance(nxt, bool):
                    raise LlmError("next 必须是布尔值")
                out.append(sc.cand("llm", ante, cons, nxt, why))
            else:
                raise LlmError("kind 必须是 template 或 prop")
        except LlmError as e:
            bad.append(f"第 {k + 1} 项：{e}")
    for c in out:
        c.sources = ["C"]
    return out, bad


def mine(n2, scopes: dict[str, Scope], llm: dict, params: dict, widths_of, log) -> tuple[list[Cand], list[dict]]:
    """One call per module (names in the answer are module-local, so it applies to every
    instance of it). Returns (candidates, audit rows)."""
    by_module: dict[str, list[str]] = {}
    for path in sorted(scopes):
        by_module.setdefault(scopes[path].inst.module, []).append(path)
    cands, audit = [], []
    for module, paths in sorted(by_module.items()):
        sc = scopes[paths[0]]
        data, names = summary(n2, sc, widths_of(paths[0]))
        msgs = prompt(data)
        ih = tagged_hash("n4_llm_input", {"messages": msgs, "model": llm.get("model"),
                                          "prompt_set": llm.get("prompt_set_version") or PROMPT_SET_VERSION})
        row = {"input_hash": ih, "module": module, "instances": paths, "provider": llm.get("provider") or "",
               "model": llm.get("model") or "", "level": "L1+L2", "output": "", "error": "", "rejected": [],
               "candidates": []}
        try:
            text = call(llm, params, module, msgs)
            row["output"] = text
            for path in paths:
                cs, bad = parse(text, scopes[path], names, widths_of(path))
                cands += cs
                row["candidates"] += [c.key for c in cs]
            row["rejected"] = bad
            if bad:
                log.warn("N4_LLM_REJECTED", f"{module}：LLM 回答中 {len(bad)} 项不符合 schema 或摘要，已丢弃")
        except LlmError as e:
            row["error"] = str(e)
            log.warn("N4_LLM_FAILED", f"{module}：{e}；路径 C 跳过这个模块")
        audit.append(row)
    return cands, audit
