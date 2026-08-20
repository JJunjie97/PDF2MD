"""Small, safe linear classifiers used by the front-region cascade."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping


ARTIFACT_SCHEMA = "pdf2md.region-linear.v1"
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_CLASSES = 64
MAX_FEATURES = 65_536


@dataclass(frozen=True)
class Prediction:
    candidates: tuple[tuple[str, float], ...]
    ood: bool = False
    reason: str | None = None

    @property
    def top(self) -> tuple[str, float] | None:
        return self.candidates[0] if self.candidates else None


@dataclass(frozen=True)
class LinearRegionModel:
    kind: str
    classes: tuple[str, ...]
    feature_names: tuple[str, ...]
    weights: tuple[tuple[float, ...], ...]
    bias: tuple[float, ...]
    temperature: float = 1.0
    min_known_fraction: float = 0.0
    min_feature_l1: float = 0.0
    max_feature_l1: float = math.inf
    fingerprint: str = ""

    def predict(self, features: Mapping[str, float]) -> Prediction:
        if not isinstance(features, Mapping):
            return Prediction((), True, "invalid_features")
        vector: list[float] = []
        known_nonzero = 0
        l1 = 0.0
        for name in self.feature_names:
            raw = features.get(name, 0.0)
            if not _finite(raw):
                return Prediction((), True, "nonfinite_features")
            value = float(raw)
            vector.append(value)
            if value:
                known_nonzero += 1
                l1 += abs(value)
        supplied_nonzero = sum(
            1 for value in features.values() if _finite(value) and float(value) != 0.0
        )
        known_fraction = known_nonzero / max(1, supplied_nonzero)
        if (
            known_fraction < self.min_known_fraction
            or l1 < self.min_feature_l1
            or l1 > self.max_feature_l1
        ):
            return Prediction((), True, "out_of_distribution")
        logits = []
        for row, intercept in zip(self.weights, self.bias):
            value = intercept + sum(weight * feature for weight, feature in zip(row, vector))
            value /= self.temperature
            if not math.isfinite(value):
                return Prediction((), True, "nonfinite_logits")
            logits.append(value)
        probabilities = _softmax(logits)
        if probabilities is None:
            return Prediction((), True, "nonfinite_probabilities")
        candidates = tuple(
            sorted(zip(self.classes, probabilities), key=lambda item: (-item[1], item[0]))
        )
        return Prediction(candidates)


def load_model_artifact(path: str | Path | None, *, expected_kind: str | None = None) -> LinearRegionModel | None:
    """Load a JSON or NPZ artifact; any malformed/untrusted artifact is rejected."""
    if path is None:
        return None
    artifact_path = Path(path)
    try:
        if not artifact_path.is_file() or artifact_path.stat().st_size > MAX_ARTIFACT_BYTES:
            return None
        raw = artifact_path.read_bytes()
        if artifact_path.suffix.casefold() == ".json":
            value = json.loads(raw.decode("utf-8-sig"))
        elif artifact_path.suffix.casefold() == ".npz":
            value = _load_npz(artifact_path)
        else:
            return None
        return _parse_artifact(value, hashlib.sha256(raw).hexdigest(), expected_kind)
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError, ImportError):
        return None


def save_json_artifact(path: str | Path, artifact: Mapping[str, Any]) -> str:
    """Validate and atomically save the portable training/inference format."""
    model = _parse_artifact(dict(artifact), "", None)
    if model is None:
        raise ValueError("invalid region model artifact")
    normalized = {
        "schema": ARTIFACT_SCHEMA,
        "kind": model.kind,
        "classes": list(model.classes),
        "feature_names": list(model.feature_names),
        "weights": [list(row) for row in model.weights],
        "bias": list(model.bias),
        "temperature": model.temperature,
        "ood": {
            "min_known_fraction": model.min_known_fraction,
            "min_feature_l1": model.min_feature_l1,
            "max_feature_l1": (
                model.max_feature_l1 if math.isfinite(model.max_feature_l1) else None
            ),
        },
        "metadata": dict(artifact.get("metadata", {}))
        if isinstance(artifact.get("metadata", {}), Mapping) else {},
    }
    payload = (json.dumps(normalized, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(target)
    return hashlib.sha256(payload).hexdigest()


def artifact_fingerprint(path: str | Path | None) -> str | None:
    if path is None:
        return None
    try:
        target = Path(path)
        if not target.is_file() or target.stat().st_size > MAX_ARTIFACT_BYTES:
            return None
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return None


def resolve_artifact(model_dir: str | Path | None, kind: str) -> Path | None:
    if model_dir is None or kind not in {"layout", "text"}:
        return None
    root = Path(model_dir)
    for name in (f"{kind}.json", f"{kind}.npz"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _parse_artifact(value: Any, fingerprint: str, expected_kind: str | None) -> LinearRegionModel | None:
    if not isinstance(value, Mapping) or value.get("schema") != ARTIFACT_SCHEMA:
        return None
    kind = value.get("kind")
    if kind not in {"layout", "text"} or (expected_kind and kind != expected_kind):
        return None
    classes = _string_tuple(value.get("classes"), MAX_CLASSES)
    feature_names = _string_tuple(value.get("feature_names"), MAX_FEATURES)
    if not classes or len(set(classes)) != len(classes) or not feature_names or len(set(feature_names)) != len(feature_names):
        return None
    weights_raw = value.get("weights")
    bias_raw = value.get("bias")
    if not isinstance(weights_raw, (list, tuple)) or len(weights_raw) != len(classes):
        return None
    weights: list[tuple[float, ...]] = []
    for row in weights_raw:
        if not isinstance(row, (list, tuple)) or len(row) != len(feature_names) or not all(_finite(item) for item in row):
            return None
        weights.append(tuple(map(float, row)))
    if not isinstance(bias_raw, (list, tuple)) or len(bias_raw) != len(classes) or not all(_finite(item) for item in bias_raw):
        return None
    temperature = value.get("temperature", 1.0)
    if not _finite(temperature) or float(temperature) <= 0.0 or float(temperature) > 1000.0:
        return None
    ood = value.get("ood", {})
    if not isinstance(ood, Mapping):
        return None
    min_known = ood.get("min_known_fraction", 0.0)
    min_l1 = ood.get("min_feature_l1", 0.0)
    max_l1_raw = ood.get("max_feature_l1", None)
    max_l1 = math.inf if max_l1_raw is None else max_l1_raw
    if (
        not _finite(min_known) or not 0.0 <= float(min_known) <= 1.0
        or not _finite(min_l1) or float(min_l1) < 0.0
        or not isinstance(max_l1, (int, float)) or isinstance(max_l1, bool)
        or math.isnan(float(max_l1)) or float(max_l1) < float(min_l1)
    ):
        return None
    return LinearRegionModel(
        kind=str(kind), classes=classes, feature_names=feature_names,
        weights=tuple(weights), bias=tuple(map(float, bias_raw)),
        temperature=float(temperature), min_known_fraction=float(min_known),
        min_feature_l1=float(min_l1), max_feature_l1=float(max_l1),
        fingerprint=fingerprint,
    )


def _load_npz(path: Path) -> dict[str, Any]:
    import numpy as np  # optional; JSON remains the dependency-free format

    with np.load(path, allow_pickle=False) as archive:
        required = {"schema", "kind", "classes", "feature_names", "weights", "bias"}
        if not required.issubset(archive.files):
            raise ValueError("missing npz fields")
        value: dict[str, Any] = {
            key: archive[key].tolist() for key in required
        }
        for key in ("temperature", "min_known_fraction", "min_feature_l1", "max_feature_l1"):
            if key in archive.files:
                item = archive[key].tolist()
                value[key] = item
        value["ood"] = {
            "min_known_fraction": value.pop("min_known_fraction", 0.0),
            "min_feature_l1": value.pop("min_feature_l1", 0.0),
            "max_feature_l1": value.pop("max_feature_l1", None),
        }
        return value


def _string_tuple(value: Any, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= maximum:
        return ()
    result = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 160:
            return ()
        result.append(item)
    return tuple(result)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _softmax(logits: list[float]) -> tuple[float, ...] | None:
    if not logits or not all(math.isfinite(item) for item in logits):
        return None
    peak = max(logits)
    exps = [math.exp(max(-745.0, item - peak)) for item in logits]
    denominator = sum(exps)
    if not math.isfinite(denominator) or denominator <= 0.0:
        return None
    values = tuple(item / denominator for item in exps)
    return values if all(math.isfinite(item) for item in values) else None


__all__ = [
    "ARTIFACT_SCHEMA", "LinearRegionModel", "Prediction", "artifact_fingerprint",
    "load_model_artifact", "resolve_artifact", "save_json_artifact",
]
