"""pipeline：一句话梗概 → 成片的一键串联（P0-12）。

阶段（串行，复用 P0-03 ~ P0-11 的现成函数，不重写）：
  script → shots → costume(主图 + neutral 特写) → voices → tts → keyframe(ref2) → video(H3 768P) → compose(OTIO → FFmpeg)

每个生成节点（LLM 一次、定妆每角色每张、配音每句、首帧每镜头、视频每镜头）在调用供应商前先查 `NodeCache`：
key = nodekey.node_key(...)（kind / 版本 / 参数 / 模板文件 sha256 / 相关 IR 字段 / 上游产物 sha256），命中就不调用供应商。
合成（compose）沿用 P0-11 自带的 segment / audio_mix / final 缓存，缓存目录放在同一个 cache_dir 下。

自动化选择规则（P0-06 ~ P0-11 的人工决定在这里固化）：
- 定妆：每角色 1 张 pro 主图（D-006 的 ref 派生只取 neutral 特写），自动取第 1 张，不挑选；
- 首帧：ref2 方案，每镜头 1 张，自动取第 1 张（selected），不做人脸相似度打分重试（留给 P1-12）；
- 配音：D-004 方案 C（instruct-speed）；音色按角色 gender 取固定音色池，同性别角色多于音色数时复用并告警；
- 视频：MiniMax-H3 768P（D-012）；额度用尽（429 2056）或账号级错误时停止提交，缺的镜头用首帧缓推占位并记入 degraded；
- 合成：原声 off、字幕与角标样式 A（D-014）。

人工干预点：无。BGM / SFX（P0-10）未做，OTIO 的 A3 / A4 保持空轨；口型路线 C（D-016）默认不接入。
spikes 代码：不被产品代码依赖（INV-03）；这里的 chat_fn / gen_fn 等可注入，离线单测用伪造函数。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from poc import (
    compose, costume, images, keyframe, llm, media, nodecache, nodekey, otio_timeline, pricing, providers, render, script, seedream,
    shots, speech, subtitles, tts, video,
)
from poc.runlog import Run

PIPELINE_VERSION = "pipeline.v1"
VOICE_POOL = {  # D-004：苏晚 知性灿灿 2.0、陆沉 高冷沉稳 2.0；其余音色未评测，P1 做音色库
    "female": ["zh_female_cancan_uranus_bigtts"],
    "male": ["zh_male_gaolengchenwen_uranus_bigtts"],
}
VIDEO_MODEL = "h3"
VIDEO_RESOLUTION = "768P"
H3_MIN_S, H3_MAX_S = 4, 15
BIG = 1e9  # 子 Runner 自己的费用上限不起作用，整次运行的保险由 Pipeline._budget 负责


class PipelineError(Exception):
    pass


@dataclass
class Fns:
    """可注入的供应商调用；None 用真实实现。"""

    chat_fn: Callable[..., Any] | None = None
    gen_fn: Callable[..., Any] | None = None
    tts_fn: Callable[..., Any] | None = None
    submit_fn: Callable[..., Any] | None = None
    query_fn: Callable[..., Any] | None = None
    video_fn: Callable[[video.Job, Path], dict[str, Any]] | None = None
    render_fn: Callable[..., Any] | None = None  # 默认 render.render；单测注入以免真实编码
    sleep: Callable[[float], None] = time.sleep


@dataclass
class Options:
    logline: str
    cache_dir: Path
    base_dir: Path | None = None
    video: str = "auto"  # auto / h3（= auto + require_real_video） / placeholder
    require_real_video: bool = False
    max_cost_cny: float = 50.0
    reroll: dict[str, int] = field(default_factory=dict)  # "stage" 或 "stage:id" → 重抽次数
    provider: str = "deepseek"
    model: str = script.DEFAULT_MODEL
    image_model: str = "pro"


@dataclass
class NodeLog:
    stage: str
    id: str
    key: str
    hit: bool
    cost_cny: float
    elapsed_s: float


def assign_voices(doc: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
    """台词里出现的角色 → 音色（按 characters[].gender 取池）；返回 (音色表, 告警)。"""
    genders = {c["id"]: c["gender"] for c in doc["characters"]}
    speakers = []
    for ln in tts.episode_lines(doc):
        if ln["speaker"] not in speakers:
            speakers.append(ln["speaker"])
    used: dict[str, int] = {}
    voices: dict[str, str] = {}
    warnings: list[str] = []
    for sp in speakers:
        g = genders.get(sp)
        pool = VOICE_POOL.get(g or "")
        if not pool:
            raise PipelineError(f"角色 {sp} 的 gender={g!r} 没有对应的音色池（可选 {', '.join(VOICE_POOL)}）")
        n = used.get(g, 0)
        voices[sp] = pool[n % len(pool)]
        if n >= len(pool):
            warnings.append(f"{g} 角色多于音色数，{sp} 复用 {voices[sp]}（听感可能与同性别角色混淆）")
        used[g] = n + 1
    return voices, warnings


def _ref(data: bytes, label: str) -> costume.Ref:
    info = images.image_info(data)
    return costume.Ref(f"cas:{label}:{info.sha256[:12]}", info.sha256, info.fmt, data)


def _ext(fmt: str) -> str:
    return "jpg" if fmt == "jpeg" else fmt


class Pipeline:
    def __init__(self, opts: Options, fns: Fns | None = None, env: Mapping[str, str] | None = None):
        self.o = opts
        self.fns = fns or Fns()
        self.env = dict(os.environ) if env is None else dict(env)
        self.cache = nodecache.NodeCache(opts.cache_dir)
        self.nodes: list[NodeLog] = []
        self.degraded: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.spent = 0.0
        self.stage_s: dict[str, float] = {}
        self.run: Run | None = None
        self.doc: dict[str, Any] = {}

    # ---- 公共 ----

    def _budget(self, est: float, what: str) -> None:
        if self.spent + est > self.o.max_cost_cny + 1e-9:
            raise PipelineError(f"费用保险：累计 ¥{self.spent:.2f} + {what} 预计 ¥{est:.2f} 将超过上限 ¥{self.o.max_cost_cny:g}")

    def _sample(self, stage: str, ident: str = "") -> int:
        return 1 + self.o.reroll.get(stage, 0) + (self.o.reroll.get(f"{stage}:{ident}", 0) if ident else 0)

    def _node(self, stage: str, ident: str, key: str, build: Callable[[], tuple[dict[str, bytes], float, dict[str, Any]]]) -> nodecache.Record:
        rec = self.cache.get(stage, key)
        if rec is not None:
            self.nodes.append(NodeLog(stage, ident, key, True, 0.0, 0.0))
            return rec
        t0 = time.monotonic()
        blobs, cost, meta = build()
        elapsed = time.monotonic() - t0
        rec = self.cache.put(stage, key, blobs, cost_cny=cost, elapsed_s=elapsed, meta=meta)
        self.spent += cost
        self.nodes.append(NodeLog(stage, ident, key, False, round(cost, 6), round(elapsed, 2)))
        return rec

    def _timed(self, stage: str, t0: float) -> None:
        self.stage_s[stage] = round(self.stage_s.get(stage, 0.0) + time.monotonic() - t0, 2)

    @property
    def work(self) -> Path:
        assert self.run is not None
        return self.run.dir / "work"

    # ---- 阶段 ----

    def script_settings_key(self) -> tuple[script.Settings, str]:
        o = self.o
        s = script.Settings(provider=o.provider, model=o.model, max_cost_cny=max(0.01, o.max_cost_cny - self.spent))
        tpl = nodekey.sha256_file(script.PROMPTS_DIR / f"{s.prompt_version}.md")
        params = {"provider": s.provider, "model": s.model, "thinking": s.thinking, "reasoning_effort": s.reasoning_effort, "temperature": s.temperature,
                  "target_s": s.target_s, "max_repairs": s.max_repairs, "max_tokens": s.max_tokens, "sample": self._sample("script")}
        return s, nodekey.node_key("script", s.prompt_version, params, template_sha256=tpl, ir={"logline": o.logline})

    def stage_script(self) -> dict[str, Any]:
        t0 = time.monotonic()
        o = self.o
        s, key = self.script_settings_key()

        def build():
            gen = script.Generator(self.run, s, o.logline, self.fns.chat_fn or llm.chat, self.env, sleep=self.fns.sleep)
            try:
                sample = gen.sample("script", self.work / "llm")
            except script.CostLimit as exc:
                raise PipelineError(f"script：{exc}") from None
            if not sample.passed:
                raise PipelineError("script：剧本生成未通过校验（" + " → ".join(a.outcome for a in sample.attempts) + "）")
            data = (self.work / "llm" / "script" / "final.json").read_bytes()
            return {"ir": data}, sample.cost_cny, {"repairs": sample.repairs, "stats": sample.stats}

        rec = self._node("script", "", key, build)
        self._timed("script", t0)
        self.script_sha = rec.artifacts["ir"]["sha256"]
        return json.loads(self.cache.read(rec, "ir"))

    def stage_shots(self, source: dict[str, Any]) -> dict[str, Any]:
        t0 = time.monotonic()
        o = self.o
        s = shots.ShotSettings(provider=o.provider, model=o.model, max_cost_cny=max(0.01, o.max_cost_cny - self.spent))
        tpl = nodekey.sha256_file(script.PROMPTS_DIR / f"{s.prompt_version}.md")
        params = {"provider": s.provider, "model": s.model, "thinking": s.thinking, "reasoning_effort": s.reasoning_effort, "temperature": s.temperature,
                  "max_repairs": s.max_repairs, "max_tokens": s.max_tokens, "sample": self._sample("shots")}
        key = nodekey.node_key("shots", s.prompt_version, params, template_sha256=tpl, upstream=[self.script_sha])

        def build():
            gen = shots.ShotGenerator(self.run, s, source, self.fns.chat_fn or llm.chat, self.env, sleep=self.fns.sleep)
            try:
                sample = gen.sample("shots", self.work / "llm")
            except script.CostLimit as exc:
                raise PipelineError(f"shots：{exc}") from None
            if not sample.passed:
                raise PipelineError("shots：" + " → ".join(a.outcome for a in sample.attempts))
            data = (self.work / "llm" / "shots" / "final.json").read_bytes()
            return {"ir": data}, sample.cost_cny, {"repairs": sample.repairs}

        try:
            rec = self._node("shots", "", key, build)
        except PipelineError as exc:
            # 分镜失败：回退使用 script 自带的镜头（它已通过 strict 校验），记入 degraded；已花的费用在 calls.jsonl 里
            self.degraded.append({"stage": "shots", "reason": str(exc), "fallback": "script 自带镜头"})
            self.spent = self._calls_total()
            self._timed("shots", t0)
            return source
        self._timed("shots", t0)
        return json.loads(self.cache.read(rec, "ir"))

    def _calls_total(self) -> float:
        assert self.run is not None
        total = 0.0
        for line in self.run.calls_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                total += float(json.loads(line).get("cost_cny") or 0.0)
        return round(total, 6)

    def stage_costume(self) -> dict[str, dict[str, costume.Ref]]:
        """每角色：主图 + neutral 特写 → keyframe 的 library（{角色: {main, neutral}}）。"""
        t0 = time.monotonic()
        doc, o = self.doc, self.o
        model_id = seedream.MODELS[o.image_model]
        tpl = nodekey.sha256_file(script.PROMPTS_DIR / f"{costume.PROMPT_VERSION}.md")
        common = {"provider": seedream.PROVIDER, "model": model_id, "watermark": costume.WATERMARK, "output_format": costume.OUTPUT_FORMAT}
        library: dict[str, dict[str, costume.Ref]] = {}
        for ch in doc["characters"]:
            cid = ch["id"]
            main_job = costume.plan_jobs(doc, "main", [cid], n=1)[0]
            main_key = nodekey.node_key(
                "costume_main", costume.PROMPT_VERSION, common | {"size": main_job.size, "sample": self._sample("costume", cid)},
                template_sha256=tpl, ir={"prompt": main_job.prompt},
            )
            main = self._image_node("costume_main", cid, main_key, main_job, model_id, costume.Runner, costume.Settings("main", o.image_model, [cid], n=1, max_cost_cny=BIG))
            main_ref = _ref(main, f"{cid}-main")
            neutral_job = next(
                j for j in costume.plan_jobs(doc, "derive", [cid], mode="ref", bases={cid: main_ref})
                if j.kind == "expression" and j.label == "neutral"
            )
            neutral_key = nodekey.node_key(
                "costume_neutral", costume.PROMPT_VERSION, common | {"size": neutral_job.size, "sample": self._sample("costume", cid)},
                template_sha256=tpl, ir={"prompt": neutral_job.prompt}, upstream=[main_ref.sha256],
            )
            neutral = self._image_node("costume_neutral", cid, neutral_key, neutral_job, model_id, costume.Runner,
                                       costume.Settings("derive", o.image_model, [cid], mode="ref", max_cost_cny=BIG))
            library[cid] = {"main": main_ref, "neutral": _ref(neutral, f"{cid}-neutral")}
        self._timed("costume", t0)
        return library

    def _image_node(self, stage: str, ident: str, key: str, job: costume.Job, model_id: str, runner_cls, settings) -> bytes:
        def build():
            price = pricing.image_cny(seedream.PROVIDER, model_id, pricing.size_pixels(job.size), len(job.refs))
            self._budget(price, f"{stage}/{ident}")
            runner = runner_cls(self.run, settings, self.env, self.fns.gen_fn, sleep=self.fns.sleep)
            out_dir = self.work / "images" / stage
            item = runner.item(job, 1, out_dir)
            if not item.ok:
                raise PipelineError(f"{stage}/{ident}：{item.error}")
            return {"image": (self.run.dir / item.file).read_bytes()}, item.cost_cny, {"size": job.size, "refs": [r.sha256 for r in job.refs]}

        try:
            rec = self._node(stage, ident, key, build)
        except costume.Abort as exc:
            raise PipelineError(f"{stage}/{ident}：{exc}") from None
        return self.cache.read(rec, "image")

    def stage_tts(self) -> tuple[Path, dict[str, str]]:
        t0 = time.monotonic()
        doc = self.doc
        voices, warns = assign_voices(doc)
        self.warnings += warns
        settings = tts.Settings(name="pipeline-instruct-speed", voices=voices, instruct=True, speed=True, rounds=1, max_cost_cny=BIG)
        out_dir = self.work / "tts"
        (out_dir / "asr").mkdir(parents=True, exist_ok=True)
        runner = tts.Runner(self.run, settings, self.env, self.fns.tts_fn, self.fns.submit_fn, self.fns.query_fn, sleep=self.fns.sleep)
        for line in tts.episode_lines(doc):
            lid = line["line_id"]
            voice = voices[line["speaker"]]
            params = {"resource": settings.resource, "asr": speech.ASR_RESOURCE, "voice": voice, "speech_rate": tts.speech_rate(line["delivery"]["speed"]),
                      "context_text": tts.instruction(line), "sample": self._sample("tts", lid)}
            key = nodekey.node_key("tts", settings.instruct_version, params, ir={"text": line["text"]})

            def build(line=line, lid=lid):
                est = pricing.tts_cny(speech.PROVIDER, settings.resource, len(line["text"]))
                self._budget(est, f"tts/{lid}")
                scratch = self.work / "tts-scratch"
                (scratch / "asr").mkdir(parents=True, exist_ok=True)
                try:
                    lr = runner.line(line, 1, scratch)
                except tts.CostLimit as exc:
                    raise PipelineError(f"tts/{lid}：{exc}") from None
                if not lr.ok:
                    raise PipelineError(f"tts/{lid}：{lr.stage} 失败：{lr.error}")
                return ({"mp3": (scratch / f"{lid}.mp3").read_bytes(), "asr": (scratch / "asr" / f"{lid}.json").read_bytes()}, lr.cost_cny,
                        {"cer": lr.metrics.get("cer"), "file_s": lr.metrics.get("file_s")})

            rec = self._node("tts", lid, key, build)
            (out_dir / f"{lid}.mp3").write_bytes(self.cache.read(rec, "mp3"))
            (out_dir / "asr" / f"{lid}.json").write_bytes(self.cache.read(rec, "asr"))
        self._timed("tts", t0)
        return out_dir, voices

    def stage_keyframe(self, library: dict[str, dict[str, costume.Ref]]) -> Path:
        t0 = time.monotonic()
        doc, o = self.doc, self.o
        model_id = seedream.MODELS[o.image_model]
        tpl = nodekey.sha256_file(script.PROMPTS_DIR / f"{keyframe.PROMPT_VERSION}.md")
        jobs = keyframe.plan_jobs(doc, ["ref2"], library=library)
        out_dir = self.work / "keyframes"
        out_dir.mkdir(parents=True, exist_ok=True)
        settings = keyframe.Settings(schemes=["ref2"], max_cost_cny=BIG, model=o.image_model)
        entries = []
        for job in jobs:
            params = {"provider": seedream.PROVIDER, "model": model_id, "size": job.size, "watermark": costume.WATERMARK, "output_format": costume.OUTPUT_FORMAT,
                      "scheme": job.scheme, "sample": self._sample("keyframe", job.shot_id)}
            key = nodekey.node_key("keyframe", keyframe.PROMPT_VERSION, params, template_sha256=tpl, ir={"prompt": job.prompt}, upstream=[r.sha256 for r in job.refs])
            data = self._image_node("keyframe", job.shot_id, key, job, model_id, keyframe.Runner, settings)
            info = images.image_info(data)
            name = f"{job.shot_id}.{_ext(info.fmt)}"
            (out_dir / name).write_bytes(data)
            entries.append({"shot_id": job.shot_id, "file": name, "sha256": info.sha256, "selected": True, "scheme": job.scheme})
        (out_dir / "manifest.json").write_text(json.dumps({"images": entries}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._timed("keyframe", t0)
        return out_dir / "manifest.json"

    def video_jobs(self, kf_manifest: Path) -> list[video.Job]:
        by_shot = {e["shot_id"]: e for e in json.loads(kf_manifest.read_text(encoding="utf-8"))["images"]}
        lookup = video._shots(self.doc)
        model = video.ALIASES[VIDEO_MODEL]
        jobs = []
        for sid, shot in lookup.items():
            e = by_shot[sid]
            path = kf_manifest.parent / e["file"]
            duration = min(H3_MAX_S, max(H3_MIN_S, int(shot["duration"]["hint_s"])))
            prompt = video.make_prompt(shot)
            jobs.append(video.Job(sid, str(path), e["sha256"], prompt, duration, model, VIDEO_RESOLUTION,
                                  video._node_key(model, VIDEO_RESOLUTION, duration, prompt, e["sha256"])))
        return jobs

    def stage_video(self, kf_manifest: Path) -> Path:
        t0 = time.monotonic()
        out_dir = self.work / "videos"
        out_dir.mkdir(parents=True, exist_ok=True)
        jobs = self.video_jobs(kf_manifest)
        if self.o.video == "placeholder":
            self.warnings.append("--video placeholder：全部镜头使用首帧缓推占位，没有调用视频接口")
            self._timed("video", t0)
            return out_dir
        stop: str | None = None
        for job in jobs:
            sid = job.shot_id
            key = job.node_key
            rec = self.cache.get("video", key)
            if rec is not None:
                self.nodes.append(NodeLog("video", sid, key, True, 0.0, 0.0))
                (out_dir / f"{sid}.mp4").write_bytes(self.cache.read(rec, "mp4"))
                continue
            if stop:
                self.degraded.append({"stage": "video", "shot_id": sid, "reason": stop, "fallback": "首帧缓推占位"})
                continue
            cost = video._cost(job).cny
            self._budget(video._cost(job).guard_cny, f"video/{sid}")
            t1 = time.monotonic()
            scratch = self.work / "video-scratch" / sid
            fn = self.fns.video_fn or _default_video_fn
            with self.run.call("minimax", "video", model=job.model, node_key=key) as call:
                call.extra = {"shot_id": sid, "resolution": job.resolution, "duration": job.duration}
                summary = fn(job, scratch)
                mp4 = scratch / "videos" / f"{sid}.mp4"
                ok = summary.get("success") == 1 and mp4.is_file()
                call.cost_cny, call.cost_basis = (cost, "estimate") if ok else (0.0, "estimate")
                if not ok:
                    call.status = "error"
                    call.error = _video_error(summary)
            if ok:
                rec = self.cache.put("video", key, {"mp4": mp4.read_bytes()}, cost_cny=cost, elapsed_s=time.monotonic() - t1, meta={"duration": job.duration})
                self.spent += cost
                self.nodes.append(NodeLog("video", sid, key, False, round(cost, 6), round(time.monotonic() - t1, 2)))
                (out_dir / f"{sid}.mp4").write_bytes(self.cache.read(rec, "mp4"))
                continue
            reason = _video_error(summary)
            aborted = any(r.get("status") == "aborted" for r in summary.get("jobs", []))
            if aborted:
                stop = reason  # 额度用尽 / 账号级错误：不再提交
            self.degraded.append({"stage": "video", "shot_id": sid, "reason": reason, "fallback": "首帧缓推占位"})
        self._timed("video", t0)
        return out_dir

    def stage_compose(self, ir_path: Path, tts_dir: Path, kf_manifest: Path, videos_dir: Path) -> dict[str, Any]:
        t0 = time.monotonic()
        out = self.run.dir
        try:
            plan = compose.plan(ir_path, videos_dir, tts_dir, kf_manifest)
        except compose.ComposeError as exc:
            raise PipelineError(f"compose 规划失败：{exc}") from None
        otio_timeline.require_otio()
        tl = otio_timeline.build(plan, out, "off")
        otio_path = out / "timeline.otio"
        otio_timeline.write(tl, otio_path)
        spec = otio_timeline.to_spec(otio_timeline.read(otio_path), out)
        cues = subtitles.cues(plan)
        ass_path = out / "subtitles.ass"
        ass_path.write_text(subtitles.ass(cues), encoding="utf-8")
        (out / "subtitles.srt").write_text(subtitles.srt(cues), encoding="utf-8")
        result = (self.fns.render_fn or render.render)(spec, out, self.o.cache_dir / "compose", ass_path, "off")
        for n in result.nodes:
            self.nodes.append(NodeLog("compose:" + n.kind, n.name, n.key, n.hit, 0.0, n.seconds))
        self._timed("compose", t0)
        placeholders = [s.shot_id for s in plan.shots if s.placeholder]
        return {"plan": plan.as_dict(), "placeholders": placeholders, "cues": [{"line_id": c.line_id, "shot_id": c.shot_id, "start_s": c.start_s, "end_s": c.end_s} for c in cues],
                "render": result.as_dict()}

    # ---- 编排 ----

    def run_all(self) -> dict[str, Any]:
        t0 = time.monotonic()
        with Run("run", {"logline": self.o.logline, "video": self.o.video, "max_cost_cny": self.o.max_cost_cny, "reroll": self.o.reroll},
                 base_dir=self.o.base_dir, secrets=providers.secret_values(self.env)) as run:
            self.run = run
            self.work.mkdir(parents=True, exist_ok=True)
            self.o.cache_dir.mkdir(parents=True, exist_ok=True)
            source = self.stage_script()
            self.doc = self.stage_shots(source)
            ir_path = run.dir / "drama.json"
            ir_path.write_text(json.dumps(self.doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            library = self.stage_costume()
            tts_dir, voices = self.stage_tts()
            kf_manifest = self.stage_keyframe(library)
            videos_dir = self.stage_video(kf_manifest)
            comp = self.stage_compose(ir_path, tts_dir, kf_manifest, videos_dir)
            final = run.dir / "final.mp4"
            info = media.probe(final)
            placeholders = comp["placeholders"]
            if self.o.video == "auto" or self.o.video == "h3":
                for sid in placeholders:  # 兜底：占位但没记 degraded（例如视频节点失败后被跳过）的镜头也要如实列出
                    if not any(d.get("shot_id") == sid for d in self.degraded):
                        self.degraded.append({"stage": "video", "shot_id": sid, "reason": "没有视频片段", "fallback": "首帧缓推占位"})
            strict = self.o.require_real_video or self.o.video == "h3"
            manifest = {
                "version": PIPELINE_VERSION,
                "logline": self.o.logline,
                "options": {"video": self.o.video, "require_real_video": strict, "max_cost_cny": self.o.max_cost_cny, "reroll": self.o.reroll,
                            "llm": f"{self.o.provider}/{self.o.model}", "image_model": self.o.image_model},
                "ir_sha256": nodekey.sha256_file(ir_path),
                "voices": voices,
                "nodes": [asdict(n) for n in self.nodes],
                "hits": sum(n.hit for n in self.nodes),
                "misses": sum(not n.hit for n in self.nodes),
                "cost_cny": round(self._calls_total(), 6),
                "stage_s": self.stage_s,
                "elapsed_s": round(time.monotonic() - t0, 2),
                "degraded": self.degraded,
                "warnings": self.warnings + list(comp["plan"].get("warnings", [])),
                "placeholders": placeholders,
                "plan": comp["plan"],
                "cues": comp["cues"],
                "final": {"file": "final.mp4", "sha256": nodekey.sha256_file(final), "bytes": final.stat().st_size, "width": info.width, "height": info.height,
                          "fps": info.fps, "duration_s": info.duration_s, "video_codec": info.video_codec, "audio_codec": info.audio_codec},
                "limits": ["BGM / SFX 未做（P0-10）", "口型路线 C 未接入（D-016）", "主图 / 首帧自动取第 1 张，不做一致性打分重试"],
            }
            (run.dir / "pipeline-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            run.write_json("pipeline-summary.json", {"cost_cny": manifest["cost_cny"], "hits": manifest["hits"], "misses": manifest["misses"]})
            manifest["strict_failed"] = bool(strict and placeholders)
            return manifest

    def dry_run(self) -> dict[str, Any]:
        """离线预估：不需要密钥、不发请求。给出 script 节点是否已缓存，以及冷运行的费用量级（取自 pricing，镜头数按 13 个估）。"""
        s, key = self.script_settings_key()
        img = pricing.image_cny(seedream.PROVIDER, seedream.MODELS[self.o.image_model], pricing.size_pixels(costume.SIZES["portrait"]), 2)
        est = {"llm": 0.5, "costume": round(4 * img, 2), "keyframe": round(13 * img, 2), "tts": 0.2,
               "video": 0.0 if self.o.video == "placeholder" else round(60 * 0.5, 2)}
        return {"script_node_key": key, "script_cached": self.cache.get("script", key) is not None, "cold_run_estimate_cny": est,
                "cold_run_total_cny": round(sum(est.values()), 2), "max_cost_cny": self.o.max_cost_cny, "video": self.o.video}


def _video_error(summary: Mapping[str, Any]) -> str:
    for r in summary.get("jobs", []):
        if r.get("status") == "aborted" or r.get("status") == "error":
            return str(r.get("error", "视频生成失败"))[:300]
    return "视频生成失败"


def _default_video_fn(job: video.Job, out_dir: Path) -> dict[str, Any]:
    return video.run_jobs([job], out_dir, max_cost_cny=BIG)
