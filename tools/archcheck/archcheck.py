#!/usr/bin/env python3
"""架构适应度检查（architecture fitness functions）。

把 docs/governance.md 中可以自动判定的架构不变量变成 CI 门禁：
  layout      顶层目录与架构文档一致                     INV-11
  adr         ADR 命名、状态、标题、索引                 INV-11
  dependency  zone 之间的依赖方向                        INV-03
  vendor-sdk  模型供应商 SDK 只在模型网关中使用          INV-01
  model-id    模型 ID 不硬编码在网关之外                 INV-01
  progress    路线图与进度文件的状态一致                 GOV

规则在 rules.toml，豁免在 exceptions.toml（必须有 owner 和到期日）。
只依赖标准库，Python >= 3.11。

用法：python3 tools/archcheck/archcheck.py [--root PATH] [--today YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fnmatch
import os
import posixpath
import re
import subprocess
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUIDE = "docs/governance.md"

sys.path.insert(0, str(HERE.parent / "workflow"))
import progress_state  # noqa: E402  进度格式的唯一解析实现（tools/workflow）

RULE_INVARIANT = {
    "layout": "INV-11",
    "adr": "INV-11",
    "dependency": "INV-03",
    "vendor-sdk": "INV-01",
    "model-id": "INV-01",
    "progress": "GOV",
    "exception": "GOV",
}

PY_EXTS = {".py"}
JS_EXTS = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".next", ".turbo", "coverage", ".pytest_cache", ".mypy_cache",
}

JS_IMPORT_RE = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)['"]([^'"\n]+)['"]""")
PY_IMPORT_RE = re.compile(r"^[ \t]*import[ \t]+([\w.]+(?:[ \t]+as[ \t]+\w+)?(?:[ \t]*,[ \t]*[\w.]+(?:[ \t]+as[ \t]+\w+)?)*)", re.M)
PY_FROM_RE = re.compile(r"^[ \t]*from[ \t]+(\.*[\w.]*)[ \t]+import\b", re.M)
ADR_NAME_RE = re.compile(r"^(\d{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
ADR_STATUS_RE = re.compile(r"^\s*[-*]?\s*\*\*状态\*\*\s*[:：]\s*([A-Za-z]+)", re.M)
ADR_REF_RE = re.compile(r"ADR-(\d{4})")


@dataclasses.dataclass(frozen=True)
class Violation:
    rule: str
    path: str
    line: int
    message: str

    def format(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line else self.path
        return f"[{RULE_INVARIANT[self.rule]} {self.rule}] {loc}: {self.message}"


@dataclasses.dataclass
class Report:
    violations: list[Violation]
    warnings: list[str]
    suppressed: int


# ---------------------------------------------------------------------------
# 文件枚举与配置
# ---------------------------------------------------------------------------

def list_files(root: Path) -> list[str]:
    """返回相对 root 的 posix 路径。优先用 git（尊重 .gitignore，包含未跟踪文件）。"""
    if (root / ".git").exists():
        try:
            out = subprocess.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                cwd=root, capture_output=True, check=True,
            ).stdout.decode("utf-8")
            return sorted(p for p in out.split("\0") if p and (root / p).is_file())
        except (OSError, subprocess.CalledProcessError):
            pass
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            files.append(Path(dirpath, name).relative_to(root).as_posix())
    return sorted(files)


def load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def zone_of(rel: str, zones: dict[str, str]) -> str | None:
    best, best_len = None, -1
    for name, prefix in zones.items():
        p = prefix.rstrip("/")
        if (rel == p or rel.startswith(p + "/")) and len(p) > best_len:
            best, best_len = name, len(p)
    return best


def alias_zone(spec: str, aliases: dict[str, str]) -> str | None:
    best, best_len = None, -1
    for key, zone in aliases.items():
        if key.endswith("*"):
            prefix = key[:-1]
            ok = spec.startswith(prefix)
        else:
            prefix = key
            ok = spec == key or spec.startswith(key + "/") or spec.startswith(key + ".")
        if ok and len(prefix) > best_len:
            best, best_len = zone, len(prefix)
    return best


def module_matches(spec: str, names: list[str], sep: str) -> str | None:
    for name in names:
        if spec == name or spec.startswith(name + sep):
            return name
    return None


# ---------------------------------------------------------------------------
# import 提取与解析
# ---------------------------------------------------------------------------

def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def extract_imports(rel: str, text: str) -> list[tuple[str, int]]:
    ext = posixpath.splitext(rel)[1]
    found: list[tuple[str, int]] = []
    if ext in JS_EXTS:
        for m in JS_IMPORT_RE.finditer(text):
            found.append((m.group(1), line_of(text, m.start())))
    elif ext in PY_EXTS:
        for m in PY_IMPORT_RE.finditer(text):
            line = line_of(text, m.start())
            for part in m.group(1).split(","):
                found.append((part.split()[0], line))
        for m in PY_FROM_RE.finditer(text):
            found.append((m.group(1), line_of(text, m.start())))
    return found


def resolve_relative(rel: str, spec: str) -> str | None:
    """把相对 import 解析为仓库内路径；越出仓库根目录时返回 None。"""
    base = posixpath.dirname(rel)
    if spec.startswith("."):
        if posixpath.splitext(rel)[1] in PY_EXTS:
            dots = len(spec) - len(spec.lstrip("."))
            rest = spec[dots:].replace(".", "/")
            for _ in range(dots - 1):
                base = posixpath.dirname(base)
            target = posixpath.join(base, rest) if rest else base
        else:
            target = posixpath.join(base, spec)
        target = posixpath.normpath(target)
        if target == ".." or target.startswith("../"):
            return None
        return target
    return None


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------

def check_layout(files: list[str], rules: dict) -> list[Violation]:
    allowed = set(rules.get("layout", {}).get("allowed_top_level", []))
    seen: dict[str, str] = {}
    for rel in files:
        if "/" in rel:
            top = rel.split("/", 1)[0]
            seen.setdefault(top, rel)
    return [
        Violation("layout", top, 0,
                  f"顶层目录 '{top}/' 不在架构约定的目录结构中（docs/architecture.md §15）")
        for top in sorted(seen) if top not in allowed
    ]


def check_code(root: Path, files: list[str], rules: dict) -> list[Violation]:
    zones = rules.get("zones", {})
    deps = rules.get("dependencies", {})
    aliases = rules.get("aliases", {})
    vendor = rules.get("vendor_sdk", {})
    model_ids = rules.get("model_ids", {})
    model_patterns = [re.compile(p) for p in model_ids.get("patterns", [])]

    out: list[Violation] = []
    for rel in files:
        ext = posixpath.splitext(rel)[1]
        if ext not in PY_EXTS | JS_EXTS:
            continue
        src_zone = zone_of(rel, zones)
        if src_zone is None:
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for spec, line in extract_imports(rel, text):
            # 供应商 SDK
            if src_zone not in vendor.get("allowed_zones", []):
                names = vendor.get("python" if ext in PY_EXTS else "node", [])
                hit = module_matches(spec, names, "." if ext in PY_EXTS else "/")
                if hit:
                    out.append(Violation(
                        "vendor-sdk", rel, line,
                        f"直接引用模型供应商 SDK '{hit}'；模型调用必须经由模型网关（services/model-gateway）"))
                    continue
            # 依赖方向
            target = resolve_relative(rel, spec)
            target_zone = zone_of(target, zones) if target else alias_zone(spec, aliases)
            if target_zone and target_zone != src_zone and target_zone not in deps.get(src_zone, []):
                reason = ("spikes/ 是一次性验证代码，产品代码不能依赖它" if target_zone == "spikes"
                          else "跨应用/服务的交互只能经 API、事件或 Temporal")
                out.append(Violation(
                    "dependency", rel, line,
                    f"zone '{src_zone}' 不允许依赖 zone '{target_zone}'（import '{spec}'）；{reason}"))

        # 硬编码模型 ID
        if src_zone not in model_ids.get("allowed_zones", []):
            for lineno, content in enumerate(text.splitlines(), 1):
                for pattern in model_patterns:
                    m = pattern.search(content)
                    if m:
                        out.append(Violation(
                            "model-id", rel, lineno,
                            f"硬编码模型 ID '{m.group(0)}…'；模型选择应放在模型网关的路由配置中"))
                        break
    return out


def check_adr(root: Path, files: list[str], rules: dict) -> list[Violation]:
    cfg = rules.get("adr", {})
    adr_dir = cfg.get("dir", "docs/adr").rstrip("/")
    index_name = cfg.get("index", "README.md")
    template_name = cfg.get("template", "template.md")
    statuses = set(cfg.get("statuses", []))

    index_rel = f"{adr_dir}/{index_name}"
    if index_rel not in files:
        return [Violation("adr", index_rel, 0, "缺少 ADR 索引文件")]
    index_text = (root / index_rel).read_text(encoding="utf-8")

    out: list[Violation] = []
    numbers: dict[str, str] = {}
    for rel in files:
        if posixpath.dirname(rel) != adr_dir or not rel.endswith(".md"):
            continue
        name = posixpath.basename(rel)
        if name in (index_name, template_name):
            continue
        m = ADR_NAME_RE.match(name)
        if not m:
            out.append(Violation("adr", rel, 0, "ADR 文件名应为 NNNN-小写短横线标题.md"))
            continue
        num = m.group(1)
        if num in numbers:
            out.append(Violation("adr", rel, 0, f"ADR 编号 {num} 与 {numbers[num]} 重复"))
        numbers.setdefault(num, rel)

        text = (root / rel).read_text(encoding="utf-8")
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        if not first.startswith(f"# ADR-{num}"):
            out.append(Violation("adr", rel, 1, f"标题应以 '# ADR-{num}' 开头"))

        sm = ADR_STATUS_RE.search(text)
        if not sm:
            out.append(Violation("adr", rel, 0, "缺少 '- **状态**：<状态>' 行"))
        else:
            status = sm.group(1)
            if status not in statuses:
                out.append(Violation("adr", rel, line_of(text, sm.start()),
                                     f"状态 '{status}' 无效，应为 {sorted(statuses)} 之一"))
            if status == "Superseded" and not any(r != num for r in ADR_REF_RE.findall(text)):
                out.append(Violation("adr", rel, 0, "状态为 Superseded 时必须注明被哪个 ADR 取代"))

        if name not in index_text:
            out.append(Violation("adr", rel, 0, f"未在 {index_rel} 中登记"))
    return out


def check_progress(root: Path, files: list[str], rules: dict) -> list[Violation]:
    cfg = rules.get("progress", {})
    roadmap_rel = cfg.get("roadmap", "docs/roadmap.md")
    progress_rel = cfg.get("progress", "docs/progress.md")
    if roadmap_rel not in files and progress_rel not in files:
        return []
    if roadmap_rel not in files or progress_rel not in files:
        missing = roadmap_rel if roadmap_rel not in files else progress_rel
        return [Violation("progress", missing, 0, "路线图与进度文件必须同时存在")]
    steps = progress_state.parse_roadmap((root / roadmap_rel).read_text(encoding="utf-8"))
    progress = progress_state.parse_progress((root / progress_rel).read_text(encoding="utf-8"))
    return [
        Violation("progress", progress_rel, 0, problem)
        for problem in progress_state.consistency_problems(steps, progress, cfg.get("max_in_progress", 2))
    ]


# ---------------------------------------------------------------------------
# 豁免
# ---------------------------------------------------------------------------

def apply_exceptions(violations: list[Violation], exceptions: list[dict], today: dt.date,
                     exceptions_path: str) -> Report:
    out: list[Violation] = []
    warnings: list[str] = []
    active: list[tuple[int, dict]] = []

    for i, exc in enumerate(exceptions):
        where = f"{exceptions_path}#{i + 1}"
        missing = [k for k in ("rule", "path", "reason", "owner", "expires") if not exc.get(k)]
        if missing:
            out.append(Violation("exception", where, 0, f"豁免缺少字段：{', '.join(missing)}"))
            continue
        if exc["rule"] not in RULE_INVARIANT or exc["rule"] == "exception":
            out.append(Violation("exception", where, 0, f"未知规则 '{exc['rule']}'"))
            continue
        if not isinstance(exc["expires"], dt.date):
            out.append(Violation("exception", where, 0, "expires 必须是日期（如 2026-12-31）"))
            continue
        if exc["expires"] < today:
            out.append(Violation(
                "exception", where, 0,
                f"豁免已于 {exc['expires']} 过期（{exc['rule']} {exc['path']}，owner {exc['owner']}）；"
                f"请偿还技术债或经架构负责人批准后续期"))
            continue
        active.append((i, exc))

    used: set[int] = set()
    suppressed = 0
    for v in violations:
        match = next((i for i, exc in active
                      if exc["rule"] == v.rule and fnmatch.fnmatch(v.path, exc["path"])), None)
        if match is None:
            out.append(v)
        else:
            used.add(match)
            suppressed += 1

    for i, exc in active:
        if i not in used:
            warnings.append(f"{exceptions_path}#{i + 1}: 豁免未被使用，可以删除（{exc['rule']} {exc['path']}）")
    return Report(out, warnings, suppressed)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def run(root: Path, rules_path: Path, exceptions_path: Path, today: dt.date) -> Report:
    rules = load_toml(rules_path)
    exceptions = load_toml(exceptions_path).get("exception", [])
    files = list_files(root)
    violations = (
        check_layout(files, rules)
        + check_adr(root, files, rules)
        + check_code(root, files, rules)
        + check_progress(root, files, rules)
    )
    try:
        exc_label = exceptions_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        exc_label = exceptions_path.name
    return apply_exceptions(violations, exceptions, today, exc_label)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dramio 架构适应度检查")
    parser.add_argument("--root", type=Path, default=HERE.parent.parent)
    parser.add_argument("--rules", type=Path, default=HERE / "rules.toml")
    parser.add_argument("--exceptions", type=Path, default=HERE / "exceptions.toml")
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    args = parser.parse_args(argv)

    report = run(args.root, args.rules, args.exceptions, args.today)
    for w in report.warnings:
        print(f"警告 {w}")
    for v in report.violations:
        print(v.format())
    if report.violations:
        print(f"\n架构检查失败：{len(report.violations)} 处违规（已豁免 {report.suppressed} 处）。"
              f"不变量说明见 {GUIDE} §3，豁免流程见 §6。")
        return 1
    print(f"架构检查通过（已豁免 {report.suppressed} 处）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
