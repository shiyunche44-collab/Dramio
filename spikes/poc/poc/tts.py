"""tts：DramaIR 台词 → 逐句配音 + ASR 回检（P0-05）。

每句：TTS 合成（豆包语音 2.0）→ 解析 MP3 时长 → ASR 回转写（录音文件识别 2.0）→ 字错率（CER）与时长指标。
一次运行只跑一个方案（候选），多轮顺序执行，不挑选、不丢弃；全部句子成功时退出码为 0。

方案由三部分组成：每个角色的音色（--voice 角色=音色，必填）、是否加语音指令（--instruct）、是否映射语速（--speed）。
语音指令按台词的 delivery（emotion / intensity）和 kind（旁白）生成，模板版本见 INSTRUCT_VERSION。

瞬时故障（网络、流截断、429、5xx、服务端繁忙）同一句最多重试 TRANSIENT_RETRIES 次，每次请求都经 Run.call 记账。
费用为估算：TTS 按结束块返回的计费字符数（没有时按全部字符）× 单价；请求被拒（HTTP 4xx、业务错误码）不计费，
其余失败（网络中断、流截断）保守按全部字符计。ASR 按音频时长计，只在提交成功时计费；查询不计费。

离线模式：
- --metrics 目录：从已入库的 l_*.mp3 与 asr/l_*.json 重算时长、CER、镜头容纳，不调用任何接口；
- --export 运行目录 目标目录：把第 1 轮样本、ASR 结果、整集拼接 ep01.mp3、summary 与 calls 整理成入库证据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TextIO

from poc import audio, pricing, providers, script, speech
from poc.runlog import Run

TRANSIENT_RETRIES = 2
INSTRUCT_VERSION = "tts-instruct.v1"

# delivery.emotion（DramaIR v0 枚举）→ 语气描述
EMOTION_ZH: dict[str, str] = {
    "neutral": "平静自然",
    "happy": "开心得意",
    "sad": "难过低落",
    "angry": "愤怒",
    "fearful": "惊恐慌乱",
    "surprised": "惊讶",
    "disgusted": "厌恶嫌弃",
    "tender": "温柔",
    "anxious": "不安担忧",
    "sarcastic": "讥讽、阴阳怪气",
}


def degree(intensity: float) -> str:
    if intensity < 0.5:
        return "略带"
    if intensity >= 0.7:
        return "非常"
    return ""


def instruction(line: Mapping[str, Any]) -> str:
    """tts-instruct.v1：按情绪、强度和旁白生成一句语音指令（context_texts 只有第一个元素生效）。"""
    d = line["delivery"]
    tone = f"{degree(float(d['intensity']))}{EMOTION_ZH[d['emotion']]}的语气"
    if line["kind"] == "voiceover":
        return f"这是内心独白，你可以压低声音、用{tone}说这句话吗？"
    return f"你可以用{tone}说这句话吗？"


def speech_rate(speed: float) -> int:
    """delivery.speed（倍速）→ speech_rate（-50 为 0.5 倍，0 为 1 倍，100 为 2 倍），线性映射。"""
    return max(-50, min(100, round((speed - 1.0) * 100)))


# ---- 输入 ----


def load_episode(path: Path = script.SAMPLE_EP01) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def episode_lines(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """第 1 集的全部台词，按出现顺序；每项附带 shot_id。"""
    out = []
    for scene in doc["episodes"][0]["scenes"]:
        for shot in scene["shots"]:
            for line in shot.get("dialogue") or []:
                out.append({**line, "shot_id": shot["shot_id"]})
    return out


def shots_of(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [shot for scene in doc["episodes"][0]["scenes"] for shot in scene["shots"]]


# ---- 指标（生成模式与 --metrics 共用） ----


def line_metrics(line: Mapping[str, Any], mp3: bytes, asr_json: Mapping[str, Any] | None) -> dict[str, Any]:
    ir = script.drama_ir()
    info = audio.mp3_info(mp3)
    chars = ir.checks.speech_chars(line["text"])
    est = ir.checks.speech_seconds(line)
    m: dict[str, Any] = {
        "line_id": line["line_id"],
        "shot_id": line.get("shot_id"),
        "speaker": line["speaker"],
        "text": line["text"],
        "chars": chars,
        "sha256": hashlib.sha256(mp3).hexdigest(),
        "bytes": len(mp3),
        "mpeg_version": info.version,
        "sample_rate": info.sample_rate,
        "bitrate_kbps": info.bitrate_kbps_avg,
        "file_s": round(info.duration_s, 3),
        "est_s": round(est, 3),
        "file_vs_est": round(info.duration_s / est, 3) if est else None,
        "cps_file": round(chars / info.duration_s, 2),
    }
    if asr_json is None:
        return m
    utt = asr_json.get("utterances") or []
    hyp = asr_json.get("text") or ""
    m["asr_text"] = hyp
    m["asr_duration_s"] = round(asr_json["duration_ms"] / 1000, 3) if asr_json.get("duration_ms") is not None else None
    if utt:
        start = min(u["start_ms"] for u in utt) / 1000
        end = max(u["end_ms"] for u in utt) / 1000
        eff = max(end - start, 0.001)
        m.update(
            speech_start_s=round(start, 3),
            speech_end_s=round(end, 3),
            effective_s=round(eff, 3),
            lead_silence_s=round(start, 3),
            tail_silence_s=round(info.duration_s - end, 3),
            cps_effective=round(chars / eff, 2),
            effective_vs_est=round(eff / est, 3) if est else None,
        )
    raw = audio.cer("".join(line["text"].split()), "".join(hyp.split()))
    norm = audio.cer(audio.normalize(line["text"]), audio.normalize(hyp))
    folded = audio.cer(audio.fold_equivalents(audio.normalize(line["text"])), audio.fold_equivalents(audio.normalize(hyp)))
    m.update(
        cer=round(norm.rate, 3),
        cer_raw=round(raw.rate, 3),
        cer_equiv=round(folded.rate, 3),
        cer_detail={"ref_len": norm.ref_len, "sub": norm.substitutions, "del": norm.deletions, "ins": norm.insertions},
        digit_equivalence=audio.has_digit_mismatch(line["text"], hyp),
    )
    return m


def shot_fit(doc: Mapping[str, Any], per_line: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """有台词、且台词全部有音频的镜头：台词实测时长之和与 hint_s 对照（留白 = hint_s − 合计）。"""
    out = []
    for shot in shots_of(doc):
        ids = [ln["line_id"] for ln in shot.get("dialogue") or []]
        if not ids or any(i not in per_line for i in ids):
            continue
        hint = float(shot["duration"]["hint_s"])
        file_sum = sum(per_line[i]["file_s"] for i in ids)
        row = {"shot_id": shot["shot_id"], "lines": ids, "hint_s": hint, "file_s": round(file_sum, 3), "slack_file_s": round(hint - file_sum, 3)}
        if all("effective_s" in per_line[i] for i in ids):
            eff = sum(per_line[i]["effective_s"] for i in ids)
            row.update(effective_s=round(eff, 3), slack_effective_s=round(hint - eff, 3))
        row["over"] = file_sum > hint
        out.append(row)
    return out


def episode_stats(per_line: list[Mapping[str, Any]], fits: list[Mapping[str, Any]]) -> dict[str, Any]:
    chars = sum(m["chars"] for m in per_line)
    file_s = sum(m["file_s"] for m in per_line)
    est_s = sum(m["est_s"] for m in per_line)
    stats: dict[str, Any] = {
        "lines": len(per_line),
        "chars": chars,
        "file_s_total": round(file_s, 3),
        "est_s_total": round(est_s, 3),
        "file_vs_est": round(file_s / est_s, 3) if est_s else None,
        "cps_file": round(chars / file_s, 2) if file_s else None,
        "shots_checked": len(fits),
        "shots_over_hint": [f["shot_id"] for f in fits if f["over"]],
        "min_slack_file_s": min((f["slack_file_s"] for f in fits), default=None),
    }
    eff = [m for m in per_line if "effective_s" in m]
    if len(eff) == len(per_line) and per_line:
        eff_s = sum(m["effective_s"] for m in eff)
        stats.update(
            effective_s_total=round(eff_s, 3),
            cps_effective=round(chars / eff_s, 2),
            effective_vs_est=round(eff_s / est_s, 3) if est_s else None,
            lead_silence_s_avg=round(statistics.mean(m["lead_silence_s"] for m in eff), 3),
            tail_silence_s_avg=round(statistics.mean(m["tail_silence_s"] for m in eff), 3),
        )
    cers = [m["cer"] for m in per_line if "cer" in m]
    if cers:
        stats.update(
            cer_avg=round(statistics.mean(cers), 3),
            cer_max=max(cers),
            cer_nonzero=sum(1 for c in cers if c > 0),
            cer_raw_avg=round(statistics.mean(m["cer_raw"] for m in per_line if "cer_raw" in m), 3),
            cer_equiv_avg=round(statistics.mean(m["cer_equiv"] for m in per_line if "cer_equiv" in m), 3),
            cer_equiv_nonzero=sum(1 for m in per_line if m.get("cer_equiv", 0) > 0),
        )
    asr_diff = [abs(m["file_s"] - m["asr_duration_s"]) for m in per_line if m.get("asr_duration_s") is not None]
    if asr_diff:
        stats["asr_vs_file_abs_diff_s_max"] = round(max(asr_diff), 3)
    return stats


# ---- 生成 ----


@dataclass
class Settings:
    name: str
    voices: dict[str, str]  # 角色 id → 音色 id
    instruct: bool = False
    speed: bool = False
    rounds: int = 3
    only: str | None = None  # 只合成这个角色的台词（音色初选用）
    resource: str = speech.TTS_RESOURCE
    max_cost_cny: float = 10.0
    instruct_version: str = INSTRUCT_VERSION


@dataclass
class LineRun:
    round: int
    line_id: str
    speaker: str
    voice: str
    speech_rate: int
    context_text: str | None
    ok: bool = False
    stage: str | None = None  # 失败在 tts / asr
    error: str | None = None
    tts_requests: int = 0
    tts_first_ok: bool = False
    tts_elapsed_s: float = 0.0  # 含重试
    tts_first_elapsed_s: float | None = None
    text_words: int | None = None
    asr_requests: int = 0
    asr_polls: int = 0
    asr_first_ok: bool = False
    asr_elapsed_s: float = 0.0
    transient_failures: int = 0
    cost_cny: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)


class CostLimit(Exception):
    pass


class Runner:
    def __init__(self, run: Run, settings: Settings, env: Mapping[str, str], tts_fn=None, submit_fn=None, query_fn=None, sleep=time.sleep):
        self.run = run
        self.s = settings
        self.env = env
        self.tts_fn = tts_fn or speech.synthesize
        self.submit_fn = submit_fn or speech.asr_submit
        self.query_fn = query_fn or speech.asr_query
        self.sleep = sleep
        self.spent = 0.0

    def _check_budget(self) -> None:
        if self.spent >= self.s.max_cost_cny:
            raise CostLimit(f"累计估算费用 ¥{self.spent:.4f} 已达上限 ¥{self.s.max_cost_cny:g}")

    def _charge(self, call, lr: LineRun, cost: float) -> None:
        call.cost_cny = cost
        call.cost_basis = "estimate"
        self.spent += cost
        lr.cost_cny += cost

    def synth(self, line: Mapping[str, Any], lr: LineRun) -> bytes:
        t0 = time.monotonic()
        try:
            for retry in range(TRANSIENT_RETRIES + 1):
                self._check_budget()
                lr.tts_requests += 1
                t_req = time.monotonic()
                try:
                    with self.run.call(speech.PROVIDER, "tts", model=self.s.resource) as call:
                        call.extra = {"candidate": self.s.name, "round": lr.round, "line_id": lr.line_id, "retry": retry, "voice": lr.voice}
                        try:
                            res = self.tts_fn(
                                line["text"], lr.voice, resource=self.s.resource, speech_rate=lr.speech_rate,
                                context_text=lr.context_text, env=self.env,
                            )
                        except speech.SpeechError as exc:
                            call.extra.update(error_kind=exc.kind, http_status=exc.http_status, api_code=exc.api_code)
                            rejected = exc.kind in ("http", "api_error", "config") and not exc.transient
                            self._charge(call, lr, 0.0 if rejected else pricing.tts_cny(speech.PROVIDER, self.s.resource, len(line["text"])))
                            raise
                        chars = res.text_words if res.text_words is not None else len(line["text"])
                        self._charge(call, lr, pricing.tts_cny(speech.PROVIDER, self.s.resource, chars))
                        call.usage = {"text_words": res.text_words, "chunks": res.chunks, "bytes": len(res.audio)}
                        call.request_id = res.logid
                        lr.text_words = res.text_words
                        if retry == 0:
                            lr.tts_first_ok = True
                            lr.tts_first_elapsed_s = round(time.monotonic() - t_req, 3)
                        return res.audio
                except speech.SpeechError as exc:
                    if not exc.transient or retry == TRANSIENT_RETRIES:
                        raise
                    lr.transient_failures += 1
                    self.sleep(2 ** (retry + 1))
            raise AssertionError("unreachable")
        finally:
            lr.tts_elapsed_s = round(time.monotonic() - t0, 3)

    def recognize(self, mp3: bytes, seconds: float, lr: LineRun) -> speech.ASRResult:
        t0 = time.monotonic()
        try:
            for retry in range(TRANSIENT_RETRIES + 1):
                self._check_budget()
                lr.asr_requests += 1
                try:
                    with self.run.call(speech.PROVIDER, "asr", model=speech.ASR_RESOURCE) as call:
                        call.extra = {"candidate": self.s.name, "round": lr.round, "line_id": lr.line_id, "retry": retry, "op": "submit"}
                        try:
                            request_id = self.submit_fn(mp3, env=self.env)
                        except speech.SpeechError as exc:
                            call.extra.update(error_kind=exc.kind, http_status=exc.http_status, api_code=exc.api_code)
                            self._charge(call, lr, 0.0)
                            raise
                        call.request_id = request_id
                        self._charge(call, lr, pricing.asr_cny(speech.PROVIDER, speech.ASR_RESOURCE, seconds))
                        call.usage = {"audio_s": round(seconds, 3)}
                    result = speech.poll_asr(request_id, lambda rid: self._query(rid, lr, retry), sleep=self.sleep)
                    lr.asr_polls += result.polls
                    if retry == 0:
                        lr.asr_first_ok = True
                    return result
                except speech.SpeechError as exc:
                    if not exc.transient or retry == TRANSIENT_RETRIES:
                        raise
                    lr.transient_failures += 1
                    self.sleep(2 ** (retry + 1))
            raise AssertionError("unreachable")
        finally:
            lr.asr_elapsed_s = round(time.monotonic() - t0, 3)

    def _query(self, request_id: str, lr: LineRun, retry: int):
        with self.run.call(speech.PROVIDER, "asr", model=speech.ASR_RESOURCE) as call:
            call.cost_cny = 0.0
            call.cost_basis = "free"
            call.request_id = request_id
            call.extra = {"candidate": self.s.name, "round": lr.round, "line_id": lr.line_id, "retry": retry, "op": "query"}
            try:
                code, obj = self.query_fn(request_id, env=self.env)
            except speech.SpeechError as exc:
                call.extra.update(error_kind=exc.kind, http_status=exc.http_status, api_code=exc.api_code)
                raise
            call.extra["status_code"] = code
            return code, obj

    def line(self, line: Mapping[str, Any], rnd: int, out_dir: Path) -> LineRun:
        voice = self.s.voices[line["speaker"]]
        lr = LineRun(
            round=rnd,
            line_id=line["line_id"],
            speaker=line["speaker"],
            voice=voice,
            speech_rate=speech_rate(line["delivery"]["speed"]) if self.s.speed else 0,
            context_text=instruction(line) if self.s.instruct else None,
        )
        try:
            mp3 = self.synth(line, lr)
        except speech.SpeechError as exc:
            lr.stage, lr.error = "tts", str(exc)
            return lr
        (out_dir / f"{lr.line_id}.mp3").write_bytes(mp3)
        try:
            seconds = audio.mp3_info(mp3).duration_s
        except audio.MP3Error as exc:
            lr.stage, lr.error = "tts", f"mp3: {exc}"
            return lr
        try:
            asr = self.recognize(mp3, seconds, lr)
        except speech.SpeechError as exc:
            lr.stage, lr.error = "asr", str(exc)
            lr.metrics = line_metrics(line, mp3, None)
            return lr
        (out_dir / "asr").mkdir(exist_ok=True)
        (out_dir / "asr" / f"{lr.line_id}.json").write_text(
            json.dumps(asr.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        lr.metrics = line_metrics(line, mp3, asr.to_json())
        lr.ok = True
        return lr


def _spread(values: list[float]) -> float:
    return round(max(values) - min(values), 3) if len(values) > 1 else 0.0


def summarize(settings: Settings, doc: Mapping[str, Any], runs: list[LineRun], n_lines: int) -> dict[str, Any]:
    n = len(runs)
    ok = [r for r in runs if r.ok]
    first_ok = [r for r in ok if r.tts_first_ok and r.asr_first_ok]
    rounds: list[dict[str, Any]] = []
    for rnd in range(1, settings.rounds + 1):
        per = {r.line_id: r.metrics for r in runs if r.round == rnd and r.ok}
        if not per:
            continue
        ordered = [per[k] for k in sorted(per)]
        fits = shot_fit(doc, per) if settings.only is None else []
        rounds.append({"round": rnd, "complete": len(per) == n_lines, "stats": episode_stats(ordered, fits), "shots": fits})
    by_line: dict[str, list[float]] = {}
    for r in ok:
        by_line.setdefault(r.line_id, []).append(r.metrics["file_s"])
    spreads = [_spread(v) for v in by_line.values()]
    tts_t = [r.tts_elapsed_s for r in runs if r.tts_requests]
    cers = [r.metrics["cer"] for r in ok if "cer" in r.metrics]
    cers_equiv = [r.metrics["cer_equiv"] for r in ok if "cer_equiv" in r.metrics]
    costs = sum(r.cost_cny for r in runs)
    complete = [x for x in rounds if x["complete"]]
    return {
        "candidate": settings.name,
        "settings": asdict(settings),
        "n_lines": n_lines,
        "n_expected": n_lines * settings.rounds,
        "n_attempted": n,
        "ok": len(ok),
        "first_ok": len(first_ok),
        "transient_failures": sum(r.transient_failures for r in runs),
        "tts_requests": sum(r.tts_requests for r in runs),
        "asr_requests": sum(r.asr_requests for r in runs),
        "tts_elapsed_s_avg": round(statistics.mean(tts_t), 2) if tts_t else None,
        "tts_elapsed_s_max": round(max(tts_t), 2) if tts_t else None,
        "tts_first_elapsed_s_avg": (
            round(statistics.mean(r.tts_first_elapsed_s for r in runs if r.tts_first_elapsed_s is not None), 2)
            if any(r.tts_first_elapsed_s is not None for r in runs) else None
        ),
        "cost_cny_total": round(costs, 6),
        "cost_cny_per_round": round(costs / settings.rounds, 6),
        "file_s_per_round_avg": round(statistics.mean(x["stats"]["file_s_total"] for x in complete), 3) if complete else None,
        "effective_s_per_round_avg": (
            round(statistics.mean(x["stats"]["effective_s_total"] for x in complete), 3)
            if complete and all("effective_s_total" in x["stats"] for x in complete) else None
        ),
        "cps_effective_avg": (
            round(statistics.mean(x["stats"]["cps_effective"] for x in complete), 2)
            if complete and all("cps_effective" in x["stats"] for x in complete) else None
        ),
        "shots_over_hint_max": max((len(x["stats"]["shots_over_hint"]) for x in complete), default=None),
        "min_slack_file_s": min((x["stats"]["min_slack_file_s"] for x in complete if x["stats"]["min_slack_file_s"] is not None), default=None),
        "line_duration_spread_s_avg": round(statistics.mean(spreads), 3) if spreads else None,
        "line_duration_spread_s_max": max(spreads) if spreads else None,
        "cer_avg": round(statistics.mean(cers), 3) if cers else None,
        "cer_max": max(cers) if cers else None,
        "cer_nonzero": sum(1 for c in cers if c > 0),
        "cer_equiv_avg": round(statistics.mean(cers_equiv), 3) if cers_equiv else None,
        "cer_equiv_max": max(cers_equiv) if cers_equiv else None,
        "cer_equiv_nonzero": sum(1 for c in cers_equiv if c > 0),
        "rounds": rounds,
        "lines": [asdict(r) for r in runs],
    }


def run_tts(
    settings: Settings,
    env: Mapping[str, str] | None = None,
    base_dir: Path | None = None,
    out: TextIO | None = None,
    episode: Path = script.SAMPLE_EP01,
    **fns,
) -> int:
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    if not (env.get(speech.KEY_ENV) or "").strip():
        print(f"缺少 {speech.KEY_ENV}：在云环境设置或 spikes/poc/.env 中配置", file=out)
        return 2
    doc, sha = load_episode(episode)
    lines = episode_lines(doc)
    if settings.only:
        lines = [ln for ln in lines if ln["speaker"] == settings.only]
        if not lines:
            print(f"--only {settings.only}：没有该角色的台词", file=out)
            return 2
    missing = sorted({ln["speaker"] for ln in lines} - set(settings.voices))
    if missing:
        print(f"缺少这些角色的音色：{', '.join(missing)}（用 --voice 角色=音色 指定）", file=out)
        return 2
    if settings.rounds < 1:
        print("--rounds 至少为 1", file=out)
        return 2

    source = {"path": str(episode.relative_to(script.REPO_ROOT)) if episode.is_relative_to(script.REPO_ROOT) else str(episode), "sha256": sha}
    with Run("tts", {"source": source, **asdict(settings)}, base_dir=base_dir, secrets=providers.secret_values(env)) as run:
        runner = Runner(run, settings, env, **fns)
        print(f"run_id: {run.run_id}  方案: {settings.name}  音色: {settings.voices}  指令: {settings.instruct}  语速: {settings.speed}", file=out)
        runs: list[LineRun] = []
        aborted = None
        for rnd in range(1, settings.rounds + 1):
            out_dir = run.dir / "samples" / f"r{rnd}"
            out_dir.mkdir(parents=True)
            for line in lines:
                try:
                    lr = runner.line(line, rnd, out_dir)
                except CostLimit as exc:
                    aborted = str(exc)
                    print(f"r{rnd} {line['line_id']}: 中止：{aborted}", file=out)
                    break
                runs.append(lr)
                m = lr.metrics
                print(
                    f"r{rnd} {lr.line_id}: {'成功' if lr.ok else '失败 ' + (lr.error or '')}  "
                    f"{m.get('file_s', '-')}s  CER {m.get('cer', '-')}  重试 {lr.transient_failures}  ¥{lr.cost_cny:.5f}",
                    file=out,
                )
            if aborted:
                break
        summary = summarize(settings, doc, runs, len(lines))
        summary.update(
            run_id=run.run_id,
            source=source,
            aborted=aborted,
            spent_cny_total=round(runner.spent, 6),  # 与 calls.jsonl 的 cost_cny 合计一致
            price={
                "tts_cny_per_10k_chars": pricing.TTS_CNY_PER_10K_CHARS[(speech.PROVIDER, settings.resource)],
                "asr_cny_per_hour": pricing.ASR_CNY_PER_HOUR[(speech.PROVIDER, speech.ASR_RESOURCE)],
                "source": pricing.SPEECH_PRICE_SOURCE,
            },
            asr_resource=speech.ASR_RESOURCE,
            instructions={ln["line_id"]: instruction(ln) for ln in lines} if settings.instruct else None,
        )
        run.write_json("summary.json", summary)
        ok = aborted is None and summary["ok"] == summary["n_expected"]
        print(
            f"\n成功 {summary['ok']}/{summary['n_expected']}（首次成功 {summary['first_ok']}），瞬时失败 {summary['transient_failures']}，"
            f"CER 平均 {summary['cer_avg']}，估算费用 ¥{summary['cost_cny_total']:.4f}",
            file=out,
        )
        print(f"输出：{run.dir}", file=out)
        if not ok:
            run.meta["result"] = "failed"
    return 0 if ok else 1


# ---- 离线：--metrics 与 --export ----


def metrics_for_dir(sample_dir: Path, episode: Path = script.SAMPLE_EP01) -> dict[str, Any]:
    """目录中的 l_*.mp3（及 asr/l_*.json）→ 每句指标、镜头容纳、整集统计。"""
    doc, _ = load_episode(episode)
    lines = {ln["line_id"]: ln for ln in episode_lines(doc)}
    per: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for mp3 in sorted(sample_dir.glob("*.mp3")):
        if mp3.stem not in lines:
            if mp3.name != "ep01.mp3":
                problems.append(f"{mp3.name}：不是 ep01 的 line_id")
            continue
        asr_path = sample_dir / "asr" / f"{mp3.stem}.json"
        asr_json = json.loads(asr_path.read_text(encoding="utf-8")) if asr_path.exists() else None
        if asr_json is None:
            problems.append(f"{mp3.stem}：缺少 asr/{mp3.stem}.json")
        try:
            per[mp3.stem] = line_metrics(lines[mp3.stem], mp3.read_bytes(), asr_json)
        except audio.MP3Error as exc:
            problems.append(f"{mp3.name}：{exc}")
    if not per:
        problems.append("目录中没有逐句 mp3")
    ordered = [per[k] for k in lines if k in per]
    fits = shot_fit(doc, per)
    return {"dir": str(sample_dir), "lines": ordered, "shots": fits, "stats": episode_stats(ordered, fits) if ordered else {}, "problems": problems}


def run_metrics(sample_dir: Path, as_json: bool = False, out: TextIO | None = None) -> int:
    out = sys.stdout if out is None else out
    if not sample_dir.is_dir():
        print(f"不是目录：{sample_dir}", file=out)
        return 2
    res = metrics_for_dir(sample_dir)
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2), file=out)
    else:
        print("line_id  角色         字数  文件秒  有效秒  首静音 尾静音  字/秒(有效)  估算秒  CER    CER原始  CER等价  转写", file=out)
        for m in res["lines"]:
            print(
                f"{m['line_id']}  {m['speaker']:<11}  {m['chars']:>3}  {m['file_s']:>6.3f}  {m.get('effective_s', float('nan')):>6.3f}  "
                f"{m.get('lead_silence_s', float('nan')):>5.2f}  {m.get('tail_silence_s', float('nan')):>5.2f}  "
                f"{m.get('cps_effective', float('nan')):>6.2f}  {m['est_s']:>7.3f}  {m.get('cer', float('nan')):.3f}  "
                f"{m.get('cer_raw', float('nan')):.3f}  {m.get('cer_equiv', float('nan')):.3f}  {m.get('asr_text', '')}",
                file=out,
            )
        print("\n镜头        hint_s  文件秒合计  留白", file=out)
        for f in res["shots"]:
            print(f"{f['shot_id']}  {f['hint_s']:>5g}  {f['file_s']:>9.3f}  {f['slack_file_s']:>6.3f}{'  超出' if f['over'] else ''}", file=out)
        print("\n整集：" + json.dumps(res["stats"], ensure_ascii=False), file=out)
    for p in res["problems"]:
        print(f"问题：{p}", file=out)
    return 1 if res["problems"] else 0


def export_run(run_dir: Path, dest: Path, rnd: int = 1, episode: Path = script.SAMPLE_EP01, out: TextIO | None = None) -> int:
    """整理入库证据：第 rnd 轮逐句 mp3 与 asr、整集拼接 ep01.mp3、run-summary.json、run-calls.jsonl。"""
    out = sys.stdout if out is None else out
    src = run_dir / "samples" / f"r{rnd}"
    if not src.is_dir() or not (run_dir / "summary.json").exists():
        print(f"{run_dir} 不是完整的 tts 运行目录（缺少 samples/r{rnd} 或 summary.json）", file=out)
        return 2
    if dest.exists() and any(dest.iterdir()):
        print(f"目标目录非空：{dest}", file=out)
        return 2
    (dest / "asr").mkdir(parents=True, exist_ok=True)
    doc, _ = load_episode(episode)
    order = [ln["line_id"] for ln in episode_lines(doc)]
    parts = []
    for line_id in order:
        mp3 = src / f"{line_id}.mp3"
        if not mp3.exists():
            continue
        shutil.copyfile(mp3, dest / mp3.name)
        parts.append(mp3.read_bytes())
        asr_path = src / "asr" / f"{line_id}.json"
        if asr_path.exists():
            shutil.copyfile(asr_path, dest / "asr" / asr_path.name)
    if parts:
        (dest / "ep01.mp3").write_bytes(audio.concat(parts))
    shutil.copyfile(run_dir / "summary.json", dest / "run-summary.json")
    shutil.copyfile(run_dir / "calls.jsonl", dest / "run-calls.jsonl")
    print(f"已导出 {len(parts)} 句到 {dest}", file=out)
    return 0


# ---- CLI ----


def _parse_voices(items: list[str] | None, parser) -> dict[str, str]:
    voices: dict[str, str] = {}
    for item in items or []:
        char, sep, voice = item.partition("=")
        if not sep or not char or not voice:
            parser.error(f"--voice 格式应为 角色id=音色id：{item!r}")
        voices[char] = voice
    return voices


def _cmd(args: argparse.Namespace) -> int:
    generation = [o for o, v in (("--voice", args.voice), ("--name", args.name), ("--instruct", args.instruct), ("--speed", args.speed),
                                  ("--rounds", args.rounds), ("--only", args.only)) if v]
    if args.metrics or args.export:
        if args.metrics and args.export:
            args.parser.error("--metrics 与 --export 不能同时使用")
        if generation:
            args.parser.error(f"离线模式不能与生成参数一起用：{', '.join(generation)}")
        if args.metrics:
            return run_metrics(Path(args.metrics), args.json)
        return export_run(Path(args.export[0]), Path(args.export[1]))
    if args.json:
        args.parser.error("--json 只能与 --metrics 一起用（否则会发起付费生成）")
    if not args.name:
        args.parser.error("生成模式需要 --name（方案名，写入 summary）")
    settings = Settings(
        name=args.name,
        voices=_parse_voices(args.voice, args.parser),
        instruct=args.instruct,
        speed=args.speed,
        rounds=3 if args.rounds is None else args.rounds,
        only=args.only,
        max_cost_cny=args.max_cost_cny,
    )
    return run_tts(settings)


def add_parser(sub) -> None:
    p = sub.add_parser("tts", help="DramaIR 台词 → 逐句配音 + ASR 回检（P0-05，豆包语音）")
    p.add_argument("--name", help="方案名（写入 summary）")
    p.add_argument("--voice", action="append", metavar="角色=音色", help="角色 id → 豆包语音 2.0 音色 id，可重复")
    p.add_argument("--instruct", action="store_true", help=f"按 delivery 生成语音指令（{INSTRUCT_VERSION}）")
    p.add_argument("--speed", action="store_true", help="把 delivery.speed 映射到 speech_rate")
    p.add_argument("--rounds", type=int, default=None, help="轮数（默认 3）")
    p.add_argument("--only", metavar="角色", help="只合成该角色的台词（音色初选）")
    p.add_argument("--max-cost-cny", type=float, default=10.0, help="本次运行累计估算费用上限（元），达到即中止")
    p.add_argument("--metrics", metavar="目录", help="离线模式：从目录中的 l_*.mp3 与 asr/*.json 重算指标，不调用接口")
    p.add_argument("--json", action="store_true", help="与 --metrics 一起用：输出完整 JSON")
    p.add_argument("--export", nargs=2, metavar=("运行目录", "目标目录"), help="离线：把一次运行整理成入库证据")
    p.set_defaults(func=_cmd, parser=p)
