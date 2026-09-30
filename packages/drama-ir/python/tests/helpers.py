import copy
import json
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[2]  # packages/drama-ir/
SAMPLE = PACKAGE_DIR / "examples" / "v0" / "ep01.json"
SAMPLE_MD = PACKAGE_DIR / "examples" / "v0" / "ep01.md"
_SAMPLE_DOC = json.loads(SAMPLE.read_text(encoding="utf-8"))


def sample() -> dict:
    return copy.deepcopy(_SAMPLE_DOC)


def shots(doc: dict) -> list[dict]:
    return [s for ep in doc["episodes"] for sc in ep["scenes"] for s in sc["shots"]]


def lines(doc: dict) -> list[dict]:
    return [l for s in shots(doc) for l in s["dialogue"]]
