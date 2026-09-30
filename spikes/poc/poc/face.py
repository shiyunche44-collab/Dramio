"""face：人脸检测 + 特征 + 余弦相似度，度量一批图里的角色一致性（P0-07）。

后端可插拔（Embedder 协议：detect(图像字节) → list[Face]）。当前后端 arcface：insightface 的 buffalo_l
（SCRFD 检测 + ArcFace w600k_r50，512 维，CPU 推理）；numpy / opencv / onnxruntime / insightface 只在该后端内懒加载，
其余逻辑只用标准库，没装依赖时 `face` 命令给出安装提示、单元测试用伪造 Embedder。权重与依赖不入库：
`pip install -e '.[face]'`；权重从 github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip 下载，
解压到 $POC_FACE_MODEL_ROOT/models/buffalo_l/（默认 ~/.cache/poc-face）。

度量口径：
- 每个角色一组参考图（基准库）：`--refs 角色=路径` 第 1 张是锚点（P0-07 用 P0-06 选定的 main-02），之后的是补充（如 neutral 特写）。
  相似度输出两种：anchor（只对锚点）与 bank（对基准库取最大）。
- 过滤：脸框短边 < --min-face-px（默认 40）记“脸太小”，检测分 < --min-det-score（默认 0.5）记“分数低”，没检出记“无脸”，
  这些脸不参与度量，对应（图像, 角色）实例记“不可度量”并写原因。
- 一张图里的人脸与该图应有的角色集合做最优指派（穷举，总相似度最大；默认按 bank 口径）：脸多于角色时取相似度最大的若干张，
  脸少于角色时缺失的角色记“未检出”。margin（指派置信差）= 该脸对被指派角色的相似度 − 它对其它角色的最高相似度，单角色时为空。
- 图像应有的角色：--manifest（keyframe 的 manifest.json，每张图带 shot_id / characters / scheme）> --expect > 图像所在目录名恰为某角色 id
  （P0-06 的证据目录按角色分目录）> 基准库里的全部角色。空镜（characters 为空）只记脸数，不计相似度。
- 统计只用标准库 statistics：n、最小最大、P10 / P25 / 中位数 / P75 / P90、均值、0.05 分箱的文本直方图；按整体 / 方案 / 角色分组。
输出确定性：按路径与镜头排序，不含时间戳，相似度取 4 位小数；同一批图两次结果逐字节一致。
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import importlib
import io
import itertools
import json
import math
import os
import statistics
import sys
import unicodedata
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, TextIO

from poc import script

BACKENDS = ("arcface",)
MODEL_ROOT_ENV = "POC_FACE_MODEL_ROOT"
DEFAULT_MODEL_ROOT = Path("~/.cache/poc-face")
MODEL_NAME = "buffalo_l"
WEIGHTS_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
DEFAULT_MIN_FACE_PX = 40
DEFAULT_MIN_DET_SCORE = 0.5
DEFAULT_DET_SIZE = 640
BIN_WIDTH = 0.05
BAR_WIDTH = 30
TOP_BIN = round(1 / BIN_WIDTH) - 1
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")
MAX_ASSIGN_CHARS = 3  # 指派用穷举

NO_FACE, SMALL_FACE, LOW_SCORE, MISSED, EMPTY_SHOT = "无脸", "脸太小", "分数低", "未检出", "空镜"
INSTALL_HINT = (
    "缺少人脸后端依赖：pip install -e '.[face]'（numpy、opencv-python-headless、onnxruntime、insightface），"
    f"并把 {MODEL_NAME} 权重（{WEIGHTS_URL}）解压到 ${MODEL_ROOT_ENV}/models/{MODEL_NAME}/（默认 {DEFAULT_MODEL_ROOT}）"
)


class FaceError(Exception):
    """输入有误（参考图无脸、角色不在基准库、图像无法解码等）。"""


class BackendError(FaceError):
    """后端不可用（缺依赖、缺权重）。"""


# ---- 后端 ----


@dataclass
class Face:
    bbox: tuple[float, float, float, float]  # x1, y1, x2, y2（像素）
    det_score: float
    embedding: list[float] = field(repr=False)  # 已 L2 归一化

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    @property
    def min_side(self) -> float:
        return min(self.width, self.height)


class Embedder(Protocol):
    name: str

    def detect(self, image_bytes: bytes) -> list[Face]: ...


class ArcFaceEmbedder:
    """insightface buffalo_l（只加载检测与识别模块）。依赖在构造时才导入。"""

    name = "arcface"

    def __init__(self, model_root: Path | str | None = None, det_size: int = DEFAULT_DET_SIZE, importer: Callable[[str], Any] = importlib.import_module):
        try:
            self._np = importer("numpy")
            self._cv2 = importer("cv2")
            importer("onnxruntime")
            analysis = importer("insightface.app")
        except ImportError as exc:
            raise BackendError(f"{INSTALL_HINT}（{type(exc).__name__}: {exc}）") from None
        root = Path(model_root or DEFAULT_MODEL_ROOT).expanduser()
        if not (root / "models" / MODEL_NAME).is_dir():
            raise BackendError(f"缺少人脸权重目录 {root / 'models' / MODEL_NAME}：下载 {WEIGHTS_URL} 并解压到该目录（或用 {MODEL_ROOT_ENV} 指定根目录）")
        with contextlib.redirect_stdout(io.StringIO()):  # insightface 加载时会打印模型路径
            self._app = analysis.FaceAnalysis(
                name=MODEL_NAME, root=str(root), allowed_modules=["detection", "recognition"], providers=["CPUExecutionProvider"],
            )
            self._app.prepare(ctx_id=-1, det_size=(det_size, det_size))

    def detect(self, image_bytes: bytes) -> list[Face]:
        img = self._cv2.imdecode(self._np.frombuffer(image_bytes, dtype=self._np.uint8), self._cv2.IMREAD_COLOR)
        if img is None:
            raise FaceError("无法解码图像")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)  # skimage 的 estimate 弃用提示，每张图都会打印
            found = self._app.get(img)
        return [Face(tuple(float(x) for x in f.bbox), float(f.det_score), [float(x) for x in f.normed_embedding]) for f in found]  # type: ignore[arg-type]


def make_backend(name: str, env: Mapping[str, str] | None = None, det_size: int = DEFAULT_DET_SIZE) -> Embedder:
    env = os.environ if env is None else env
    if name == "arcface":
        return ArcFaceEmbedder(env.get(MODEL_ROOT_ENV) or None, det_size=det_size)
    raise BackendError(f"未知后端 {name!r}（可选 {' / '.join(BACKENDS)}）")


# ---- 相似度与过滤 ----


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度；任一向量为零向量时记 0。"""
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass(frozen=True)
class Thresholds:
    min_face_px: float = DEFAULT_MIN_FACE_PX
    min_det_score: float = DEFAULT_MIN_DET_SCORE


def face_reason(face: Face, th: Thresholds) -> str | None:
    """这张脸不可度量的原因（脸太小 / 分数低）；可度量返回 None。"""
    if face.min_side < th.min_face_px:
        return SMALL_FACE
    if face.det_score < th.min_det_score:
        return LOW_SCORE
    return None


@dataclass
class RefFace:
    path: str
    sha256: str
    face: Face = field(repr=False)


@dataclass
class Bank:
    """基准库：每个角色一组参考脸，第 1 张是锚点。"""

    refs: dict[str, list[RefFace]]

    def anchor(self, char_id: str, face: Face) -> float:
        return cosine(face.embedding, self.refs[char_id][0].face.embedding)

    def best(self, char_id: str, face: Face) -> float:
        return max(cosine(face.embedding, r.face.embedding) for r in self.refs[char_id])


def display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(script.REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def build_bank(embedder: Embedder, refs: Mapping[str, Sequence[Path]], th: Thresholds) -> Bank:
    """每张参考图取最大的可度量脸；没有可度量的脸则报错。"""
    bank: dict[str, list[RefFace]] = {}
    for cid, paths in refs.items():
        bank[cid] = []
        for path in paths:
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise FaceError(f"参考图不可读 {path}：{exc}") from None
            faces = embedder.detect(data)
            usable = [f for f in faces if face_reason(f, th) is None]
            if not usable:
                why = f"（检出 {len(faces)} 张脸，都{face_reason(faces[0], th)}）" if faces else "（没有检出人脸）"
                raise FaceError(f"参考图 {path} 没有可度量的脸{why}")
            best = max(usable, key=lambda f: (f.width * f.height, f.det_score))
            bank[cid].append(RefFace(display_path(path), hashlib.sha256(data).hexdigest(), best))
    return Bank(bank)


# ---- 指派与度量 ----


def best_assignment(sims: Sequence[Sequence[float]]) -> list[tuple[int, int]]:
    """sims[脸][角色] → 总相似度最大的一一指派 [(脸, 角色)]，按角色序号排序。穷举，角色数 ≤ MAX_ASSIGN_CHARS。

    脸多于角色时取相似度最大的若干张，脸少于角色时只指派部分角色。并列时取先枚举到的（脸按位置排序，结果确定）。
    """
    n_faces = len(sims)
    n_chars = len(sims[0]) if n_faces else 0
    if n_chars > MAX_ASSIGN_CHARS:
        raise ValueError(f"指派用穷举，角色数不能超过 {MAX_ASSIGN_CHARS}")
    if not n_faces or not n_chars:
        return []
    best: list[tuple[int, int]] = []
    best_total = -math.inf
    if n_faces >= n_chars:
        candidates = ([(f, c) for c, f in enumerate(faces)] for faces in itertools.permutations(range(n_faces), n_chars))
    else:
        candidates = ([(f, c) for f, c in enumerate(chars)] for chars in itertools.permutations(range(n_chars), n_faces))
    for pairs in candidates:
        total = sum(sims[f][c] for f, c in pairs)
        if total > best_total + 1e-12:
            best, best_total = pairs, total
    return sorted(best, key=lambda p: p[1])


@dataclass
class FaceInfo:
    bbox: list[float]
    det_score: float
    width: float
    height: float
    reason: str | None  # 脸太小 / 分数低；None = 可度量


@dataclass
class Instance:
    """一个（图像, 角色）实例。"""

    char_id: str
    measurable: bool
    reason: str | None = None
    anchor: float | None = None
    bank: float | None = None
    margin: float | None = None
    det_score: float | None = None
    face_px: float | None = None  # 脸框短边
    bbox: list[float] | None = None


@dataclass
class ImageResult:
    image: str
    n_faces: int
    faces: list[FaceInfo]
    instances: list[Instance]
    characters: list[str]
    shot_id: str | None = None
    scheme: str | None = None
    round: int | None = None
    pre_measurable: str | None = None  # keyframe manifest 的预标注


def _r4(x: float) -> float:
    return round(x, 4)


def measure_image(
    embedder: Embedder, data: bytes, image: str, expected: Sequence[str], bank: Bank, th: Thresholds, assign_by: str = "bank", **meta: Any,
) -> ImageResult:
    """检测 → 过滤 → 与应有角色做最优指派 → 每个角色一个实例。"""
    faces = sorted(embedder.detect(data), key=lambda f: (round(f.bbox[0], 1), round(f.bbox[1], 1), -f.det_score))
    infos = [FaceInfo([round(x, 1) for x in f.bbox], _r4(f.det_score), round(f.width, 1), round(f.height, 1), face_reason(f, th)) for f in faces]
    usable = [i for i, info in enumerate(infos) if info.reason is None]
    chars = list(expected)
    sims = [[(bank.best if assign_by == "bank" else bank.anchor)(c, faces[i]) for c in chars] for i in usable]
    assigned = {c: f for f, c in best_assignment(sims)}
    if not faces:
        missing = NO_FACE
    elif not usable:
        missing = max(infos, key=lambda x: x.det_score).reason or MISSED
    else:
        missing = MISSED
    instances = []
    for c_idx, cid in enumerate(chars):
        if c_idx not in assigned:
            instances.append(Instance(cid, False, missing))
            continue
        f_idx = assigned[c_idx]
        face = faces[usable[f_idx]]
        others = [sims[f_idx][j] for j in range(len(chars)) if j != c_idx]
        instances.append(Instance(
            cid, True, None, _r4(bank.anchor(cid, face)), _r4(bank.best(cid, face)),
            _r4(sims[f_idx][c_idx] - max(others)) if others else None,
            _r4(face.det_score), round(face.min_side, 1), [round(x, 1) for x in face.bbox],
        ))
    return ImageResult(image, len(faces), infos, instances, chars, **meta)


# ---- 统计 ----


def describe(values: Sequence[float]) -> dict[str, Any]:
    """n、最小最大、P10 / P25 / 中位数 / P75 / P90、均值（statistics，inclusive 分位法 = numpy 线性插值）。"""
    vals = sorted(values)
    keys = ("min", "p10", "p25", "median", "p75", "p90", "max", "mean")
    if not vals:
        return {"n": 0, **dict.fromkeys(keys)}
    if len(vals) == 1:
        cuts = [vals[0]] * 19
    else:
        cuts = statistics.quantiles(vals, n=20, method="inclusive")  # 每 5%：cuts[k] = P(5 × (k + 1))
    return {
        "n": len(vals), "min": _r4(vals[0]), "p10": _r4(cuts[1]), "p25": _r4(cuts[4]), "median": _r4(statistics.median(vals)),
        "p75": _r4(cuts[14]), "p90": _r4(cuts[17]), "max": _r4(vals[-1]), "mean": _r4(statistics.mean(vals)),
    }


def histogram_bins(values: Sequence[float]) -> list[list[float]]:
    """0.05 分箱 [[下界, 个数], ...]，覆盖数据范围（中间的空箱也列出）；1.0 归入最后一箱。"""
    per_bin: dict[int, int] = {}
    for v in values:
        k = min(math.floor(round(v / BIN_WIDTH, 6)), TOP_BIN)
        per_bin[k] = per_bin.get(k, 0) + 1
    if not per_bin:
        return []
    return [[round(k * BIN_WIDTH, 2), per_bin.get(k, 0)] for k in range(min(per_bin), max(per_bin) + 1)]


def render_bins(bins: Sequence[Sequence[float]]) -> list[str]:
    """文本直方图：区间、条形（█，最多 BAR_WIDTH 格，非空箱至少 1 格）、个数。"""
    top = max((n for _, n in bins), default=0)
    lines = []
    for lo, n in bins:
        bar = "█" * max(1, round(n / top * BAR_WIDTH)) if n else ""
        label = f"[{lo:+.2f}, {lo + BIN_WIDTH:+.2f}{']' if round(lo / BIN_WIDTH) == TOP_BIN else ')'}"
        lines.append(" ".join(x for x in (label, bar, str(int(n))) if x))
    return lines


def histogram(values: Sequence[float]) -> list[str]:
    return render_bins(histogram_bins(values))


def _group(instances: list[tuple[ImageResult, Instance]]) -> dict[str, Any]:
    measurable = [i for _, i in instances if i.measurable]
    return {
        "instances": len(instances),
        "measurable": len(measurable),
        "measurable_rate": _r4(len(measurable) / len(instances)) if instances else None,
        "anchor": describe([i.anchor for i in measurable]),  # type: ignore[misc]
        "bank": describe([i.bank for i in measurable]),  # type: ignore[misc]
        "histogram": {
            "anchor": histogram_bins([i.anchor for i in measurable]),  # type: ignore[misc]
            "bank": histogram_bins([i.bank for i in measurable]),  # type: ignore[misc]
        },
    }


def group_stats(results: Sequence[ImageResult]) -> dict[str, Any]:
    """整体、按方案（有 manifest 时）、按角色三组统计；每组 n 是可度量实例数。"""
    pairs = [(r, i) for r in results for i in r.instances]
    out: dict[str, Any] = {"overall": _group(pairs)}
    schemes = sorted({r.scheme for r, _ in pairs if r.scheme})
    if schemes:
        out["by_scheme"] = {s: _group([p for p in pairs if p[0].scheme == s]) for s in schemes}
    out["by_char"] = {c: _group([p for p in pairs if p[1].char_id == c]) for c in sorted({i.char_id for _, i in pairs})}
    return out


# ---- 输出 ----


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _num(x: float | None, nd: int = 4) -> str:
    return "" if x is None else f"{x:.{nd}f}"


def build_report(
    backend: str, th: Thresholds, bank: Bank, results: Sequence[ImageResult], assign_by: str = "bank", skipped: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "backend": backend,
        "thresholds": asdict(th),
        "assign_by": assign_by,
        "references": {
            cid: [{"path": r.path, "sha256": r.sha256, "det_score": _r4(r.face.det_score), "face_px": round(r.face.min_side, 1)} for r in refs]
            for cid, refs in bank.refs.items()
        },
        "skipped_same_as_reference": list(skipped),
        "images": [asdict(r) for r in results],
        "stats": group_stats(results),
    }


CSV_COLUMNS = (
    "image", "shot_id", "scheme", "round", "pre_measurable", "char_id", "n_faces", "measurable", "reason",
    "anchor", "bank", "margin", "det_score", "face_px", "bbox",
)


def csv_rows(results: Sequence[ImageResult]) -> list[list[str]]:
    rows = []
    for r in results:
        head = [r.image, r.shot_id or "", r.scheme or "", "" if r.round is None else str(r.round), r.pre_measurable or ""]
        for i in r.instances or [Instance("", False, EMPTY_SHOT)]:
            rows.append(head + [
                i.char_id, str(r.n_faces), "1" if i.measurable else "0", i.reason or "", _num(i.anchor), _num(i.bank), _num(i.margin),
                _num(i.det_score), _num(i.face_px, 1), " ".join(f"{x:.1f}" for x in i.bbox or []),
            ])
    return rows


def render_csv(results: Sequence[ImageResult]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    writer.writerows(csv_rows(results))
    return buf.getvalue()


def render_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(_width(x) for x in col) for col in zip(header, *rows)]
    return ["  ".join(_pad(c, w) for c, w in zip(row, widths)).rstrip() for row in [header, *rows]]


def render_text(report: Mapping[str, Any], results: Sequence[ImageResult]) -> str:
    th = report["thresholds"]
    lines = [
        f"后端 {report['backend']}  最小人脸 {th['min_face_px']:g} px  最小检测分 {th['min_det_score']:g}  指派口径 {report['assign_by']}",
        "基准库（第 1 张为锚点）：",
    ]
    for cid, refs in report["references"].items():
        for n, r in enumerate(refs):
            lines.append(f"  {cid} {'锚点' if n == 0 else '补充'} {r['path']}  脸 {r['face_px']:g} px  检测分 {r['det_score']:.2f}")
    if report["skipped_same_as_reference"]:
        lines.append(f"已跳过 {len(report['skipped_same_as_reference'])} 张与参考图字节相同的图像（自比相似度恒为 1，不计入统计）")
    with_shot = any(r.shot_id for r in results)
    header = (["镜头", "方案", "轮", "预标注"] if with_shot else ["图像"]) + ["角色", "脸数", "anchor", "bank", "margin", "脸px", "检测分", "状态"]
    rows = []
    for r in results:
        head = [r.shot_id or "", r.scheme or "", "" if r.round is None else str(r.round), r.pre_measurable or ""] if with_shot else [r.image]
        for i in r.instances or [Instance("", False, EMPTY_SHOT)]:
            rows.append(head + [
                i.char_id or "-", str(r.n_faces), _num(i.anchor, 3), _num(i.bank, 3), _num(i.margin, 3), _num(i.face_px, 0), _num(i.det_score, 2),
                "可度量" if i.measurable else f"不可度量：{i.reason}",
            ])
    lines += ["", f"逐{'镜头' if with_shot else '图像'}（{len(results)} 张图，{len(rows)} 行）：", *_table(header, rows)]
    groups: list[tuple[str, dict[str, Any]]] = [("整体", report["stats"]["overall"])]
    groups += [(f"方案 {k}", v) for k, v in report["stats"].get("by_scheme", {}).items()]
    groups += [(f"角色 {k}", v) for k, v in report["stats"]["by_char"].items()]
    for title, g in groups:
        rate = "" if g["measurable_rate"] is None else f"（可度量率 {g['measurable_rate'] * 100:.1f}%）"
        lines += ["", f"[{title}] 实例 {g['instances']}，可度量 {g['measurable']}{rate}"]
        if not g["measurable"]:
            continue
        for metric in ("anchor", "bank"):
            d = g[metric]
            lines.append(
                f"  {metric:<6} n={d['n']}  min={d['min']:.3f}  P10={d['p10']:.3f}  P25={d['p25']:.3f}  中位={d['median']:.3f}  "
                f"P75={d['p75']:.3f}  P90={d['p90']:.3f}  max={d['max']:.3f}  均值={d['mean']:.3f}"
            )
        for metric in ("anchor", "bank"):
            lines += [f"  直方图 {metric}：", *[f"    {ln}" for ln in render_bins(g["histogram"][metric])]]
    return "\n".join(lines) + "\n"


# ---- 输入收集与 CLI ----


@dataclass
class Target:
    path: Path
    expected: list[str] | None  # None = 按规则推断
    meta: dict[str, Any] = field(default_factory=dict)


def collect_images(paths: Sequence[str]) -> list[Path]:
    """目录（递归）或文件；按路径排序去重。"""
    found: set[Path] = set()
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            found.update(f for f in p.rglob("*") if f.suffix.lower() in IMAGE_SUFFIXES and f.is_file())
        elif p.is_file():
            found.add(p)
        else:
            raise FaceError(f"找不到图像或目录：{raw}")
    return sorted(found, key=lambda f: f.as_posix())


def load_manifests(paths: Sequence[str]) -> list[Target]:
    targets = []
    for raw in paths:
        mpath = Path(raw)
        try:
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
            entries = manifest["images"]
        except (OSError, ValueError, KeyError) as exc:
            raise FaceError(f"manifest 不可用 {raw}：{exc}") from None
        for e in entries:
            if "shot_id" not in e or "characters" not in e:
                raise FaceError(f"{raw} 不是 keyframe 的 manifest（图像项缺少 shot_id / characters）")
            targets.append(Target(mpath.parent / e["file"], list(e["characters"]), {
                "shot_id": e["shot_id"], "scheme": e.get("scheme"), "round": e.get("round"), "pre_measurable": e.get("measurable"),
            }))
    schemes = ("text", "ref1", "ref2", "shared")
    order = lambda t: (t.meta["shot_id"], schemes.index(t.meta["scheme"]) if t.meta["scheme"] in schemes else len(schemes), t.meta["round"] or 0, t.path.as_posix())  # noqa: E731
    return sorted(targets, key=order)


def parse_refs(items: Sequence[str]) -> dict[str, list[Path]]:
    refs: dict[str, list[Path]] = {}
    for item in items:
        cid, sep, path = item.partition("=")
        if not sep or not cid or not path:
            raise FaceError(f"--refs 格式应为 角色id=图像路径：{item!r}")
        refs.setdefault(cid, []).append(Path(path))
    return refs


def run_face(
    refs: Mapping[str, Sequence[Path]],
    targets: Sequence[Target],
    embedder: Embedder,
    th: Thresholds = Thresholds(),
    expect: Sequence[str] | None = None,
    assign_by: str = "bank",
    json_path: Path | None = None,
    csv_path: Path | None = None,
    out: TextIO | None = None,
) -> int:
    out = sys.stdout if out is None else out
    try:
        bank = build_bank(embedder, refs, th)
        ref_shas = {r.sha256 for rs in bank.refs.values() for r in rs}
        results: list[ImageResult] = []
        skipped: list[str] = []
        for t in targets:
            if t.expected is not None:
                expected = t.expected
            elif expect:
                expected = list(expect)
            elif t.path.parent.name in bank.refs:
                expected = [t.path.parent.name]
            else:
                expected = list(bank.refs)
            unknown = [c for c in expected if c not in bank.refs]
            if unknown:
                raise FaceError(f"{display_path(t.path)} 需要角色 {', '.join(unknown)}，但基准库里没有（用 --refs 提供）")
            try:
                data = t.path.read_bytes()
            except OSError as exc:
                raise FaceError(f"图像不可读 {t.path}：{exc}") from None
            if hashlib.sha256(data).hexdigest() in ref_shas:
                skipped.append(display_path(t.path))
                continue
            try:
                results.append(measure_image(embedder, data, display_path(t.path), expected, bank, th, assign_by, **t.meta))
            except FaceError as exc:
                raise FaceError(f"{display_path(t.path)}：{exc}") from None
    except FaceError as exc:
        print(str(exc), file=out)
        return 2
    report = build_report(embedder.name, th, bank, results, assign_by, skipped)
    out.write(render_text(report, results))
    if json_path is not None:
        json_path.write_text(render_json(report), encoding="utf-8")
        print(f"已写 {json_path}", file=out)
    if csv_path is not None:
        csv_path.write_text(render_csv(results), encoding="utf-8")
        print(f"已写 {csv_path}", file=out)
    return 0


def _cmd(args: argparse.Namespace) -> int:
    if not args.refs:
        args.parser.error("需要 --refs 角色id=图像路径（至少一个角色）")
    if bool(args.images) == bool(args.manifest):
        args.parser.error("--images 与 --manifest 二选一")
    if args.manifest and args.expect:
        args.parser.error("--manifest 已带每张图的角色，不能再用 --expect")
    try:
        refs = parse_refs(args.refs)
        targets = load_manifests(args.manifest) if args.manifest else [Target(p, None) for p in collect_images(args.images)]
        if not targets:
            raise FaceError("没有找到图像")
        embedder = make_backend(args.backend, det_size=args.det_size)
    except FaceError as exc:  # BackendError（缺依赖 / 缺权重）也在这里
        print(str(exc))
        return 2
    return run_face(
        refs, targets, embedder, Thresholds(args.min_face_px, args.min_det_score),
        expect=[c.strip() for c in args.expect.split(",") if c.strip()] if args.expect else None,
        json_path=Path(args.json) if args.json else None, csv_path=Path(args.csv) if args.csv else None,
    )


def add_parser(sub) -> None:
    p = sub.add_parser("face", help="人脸检测 + 特征 + 余弦相似度，度量角色一致性（P0-07；依赖 pip install -e '.[face]'）")
    p.add_argument("--refs", action="append", metavar="角色=图像", help="基准库：每个角色的参考图，可重复；同一角色的第 1 张是锚点（anchor），其余补充进 bank")
    p.add_argument("--images", nargs="+", metavar="目录或文件", help="要度量的图像（目录递归）")
    p.add_argument("--manifest", action="append", metavar="manifest.json", help="改用 keyframe 的 manifest（带 shot_id / characters / 方案），按镜头出表；可重复")
    p.add_argument("--expect", metavar="角色,角色", help="这批图里应有的角色（默认：图像所在目录名是角色 id 就用它，否则用基准库全部角色）")
    p.add_argument("--backend", choices=BACKENDS, default="arcface", help="人脸后端（默认 arcface）")
    p.add_argument("--json", metavar="PATH", help="写出 JSON（含逐图、逐实例与统计）")
    p.add_argument("--csv", metavar="PATH", help="写出 CSV（每个（图像, 角色）实例一行）")
    p.add_argument("--min-face-px", type=float, default=DEFAULT_MIN_FACE_PX, help=f"脸框短边小于它记“脸太小”（默认 {DEFAULT_MIN_FACE_PX}）")
    p.add_argument("--min-det-score", type=float, default=DEFAULT_MIN_DET_SCORE, help=f"检测分低于它记“分数低”（默认 {DEFAULT_MIN_DET_SCORE}）")
    p.add_argument("--det-size", type=int, default=DEFAULT_DET_SIZE, help=f"arcface 检测输入边长（默认 {DEFAULT_DET_SIZE}；脸很小时调大）")
    p.set_defaults(func=_cmd, parser=p)
