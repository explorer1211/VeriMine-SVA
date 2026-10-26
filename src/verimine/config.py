"""The one configuration model, shared by N0 (validation) and N0a (draft generation).

The JSON Schema in schemas/ is exported from this model (`verimine schema`), never edited by hand.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SIGNAL_RE = r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)*(\[[0-9]+\])?$"
IDENT_RE = r"^[A-Za-z_][A-Za-z0-9_$]*$"

Language = Literal["v2001", "v2005", "sv2005", "sv2009", "sv2012", "sv2017", "sv2023"]


class _Strict(BaseModel):
    # Unknown keys are almost always typos (e.g. `include_dir`); reject them.
    model_config = ConfigDict(extra="forbid")


def _as_list(v: Any) -> Any:
    return [v] if isinstance(v, str) else v


class Design(_Strict):
    name: str | None = Field(None, description="Display name; defaults to top")
    root: str = Field(".", description="Project root, relative to the YAML file")
    top: str | None = Field(None, pattern=IDENT_RE)
    filelist: list[str] = Field(default_factory=list, description=".f files, relative to root")
    files: list[str] = Field(default_factory=list, description="Compile units in compile order")
    include_dirs: list[str] = Field(default_factory=list)
    defines: dict[str, str | int | None] = Field(
        default_factory=dict, description="null means `define NAME with no value"
    )
    parameters: dict[str, str | int | float | bool] = Field(
        default_factory=dict, description="Top-level parameter overrides"
    )
    language: Language = "sv2017"
    blackbox: list[str] = Field(default_factory=list)
    library_files: list[str] = Field(default_factory=list, description="-v")
    library_dirs: list[str] = Field(default_factory=list, description="-y")
    libext: list[str] = Field(default_factory=list, description="+libext+; empty means .v .sv")

    _lists = field_validator(
        "filelist", "files", "include_dirs", "blackbox", "library_files", "library_dirs", "libext",
        mode="before",
    )(_as_list)


class Clock(_Strict):
    name: str = Field(pattern=SIGNAL_RE)
    edge: Literal["posedge", "negedge"] = "posedge"


class Reset(_Strict):
    name: str = Field(pattern=SIGNAL_RE)
    active: Literal["low", "high"]
    type: Literal["async", "sync"]
    clock: str | None = Field(None, description="Clock this reset belongs to, if more than one")


class Assume(_Strict):
    name: str = Field(pattern=IDENT_RE)
    expr: str = Field(min_length=1, description="SVA property body confirmed by the user")


class LLM(_Strict):
    mode: Literal["off", "cloud", "local"] = "off"
    provider: str | None = None
    model: str | None = None
    prompt_set_version: str | None = None


class Run(_Strict):
    seed: int = 1
    profile: str = "default"
    params: dict[str, Any] = Field(default_factory=dict, description="Algorithm parameters")


class Env(_Strict):
    docker_digest: str | None = Field(
        None, pattern=r"^sha256:[0-9a-f]{64}$", description="If set, replaces tool versions in env_hash"
    )


class Config(_Strict):
    schema_version: Literal[1] = 1
    status: Literal["draft", "confirmed"] = "draft"
    unresolved: list[str] = Field(default_factory=list)
    design: Design
    clocks: list[Clock] = Field(default_factory=list)
    resets: list[Reset] = Field(default_factory=list)
    assumes: list[Assume] = Field(default_factory=list)
    waveforms: list[str] = Field(default_factory=list)
    llm: LLM = Field(default_factory=LLM)
    run: Run = Field(default_factory=Run)
    env: Env = Field(default_factory=Env)


def json_schema() -> str:
    schema = Config.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "VeriMine-SVA configuration"
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
