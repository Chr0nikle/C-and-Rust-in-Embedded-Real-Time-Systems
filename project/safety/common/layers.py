"""Deterministic, configuration-based source-layer classification."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any


class LayerClassifier:
    def __init__(self, spec_path: Path):
        self.spec = json.loads(spec_path.read_text(encoding="utf-8"))
        self.rules = self.spec["rules"]
        self.default = self.spec.get("default", "unclassified")

    @staticmethod
    def _scope_matches(rule: dict[str, Any], scope: str) -> bool:
        expected = rule.get("scope", "*")
        if expected == "*":
            return True
        if isinstance(expected, list):
            return scope in expected
        return expected == scope

    @staticmethod
    def _path_matches(rule: dict[str, Any], path: str) -> bool:
        prefix = rule.get("path_prefix")
        if prefix and not path.startswith(prefix):
            return False
        basename = rule.get("basename")
        if basename and PurePosixPath(path).name != basename:
            return False
        return bool(prefix or basename)

    def classify(self, scope: str, path: str | None) -> str:
        if not path:
            return self.default
        normalised = path.replace("\\", "/")
        while normalised.startswith("./"):
            normalised = normalised[2:]
        for rule in self.rules:
            if not self._scope_matches(rule, scope):
                continue
            if self._path_matches(rule, normalised):
                return rule["layer"]
        return self.default

    def describe(self) -> dict[str, Any]:
        return {"schema": self.spec.get("schema"), "rules": self.rules, "default": self.default}
