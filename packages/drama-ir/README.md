# drama-ir：DramaIR 定义

DramaIR 是剧本数据的**唯一事实来源**（INV-02、ADR-0002）：Schema 只在本目录定义，类型只生成、不手写。

```
schema/v0/drama.schema.json   DramaIR v0 JSON Schema（P0 草案）
examples/v0/ep01.json         标准样例：P0 所有评测的固定输入
examples/v0/ep01.md           标准样例的可读版本（由 render 生成，勿手改）
python/dramio_drama_ir/       校验与渲染（只用标准库，Python ≥ 3.11）
python/tests/                 单元测试
```

## v0 的定位

- **P0 草案**：只含 P0-03 ~ P0-11 需要的最小字段（剧、角色、集、场、镜、台词）。P0-14 复盘时修订，P1-03 升级为 v1，同时引入生成类型（zod / pydantic）和兼容性检查。在此之前，本包用一个手写的子集校验器，登记为 [TD-001](../../docs/tech-debt.md#td-001)。
- **只用结构关键字**：`type`、`properties`、`required`、`additionalProperties`、`items`、`enum`、`$ref`（仅 `#/$defs/...`）、`$defs`，以及 `title`、`description`、`$schema`、`$id`。所有对象都 `additionalProperties: false`，所有字段都必填、不用 null。这样 Schema 可以直接交给 LLM 做严格结构化输出（P0-03）。校验器遇到其他关键字直接报错，不会悄悄漏检。
- **值域、格式与引用关系**由语义校验负责（见下文）。
- 与 JSON Schema 2020-12 一致：`integer` 接受 `1.0` 这类整数值；另外拒绝 `NaN` / `Infinity`（不是合法 JSON）。Schema 本身写错（未知 `type`、`enum` 不是列表、`$ref` 无法解析或旁边有其他关键字等）时直接报错。
- **与 P0-03 / P0-04 的衔接**：v0 的台词挂在镜头上，场至少有 1 个镜头，没有“已分场、未分镜”的形态。因此 P0-03 直接产出含镜头的 v0 文档，P0-04 在此基础上重拆或评测分镜；是否需要中间形态留给 P0-14 修订。
- P0-03 把 Schema 交给 LLM 做结构化输出时，如果供应商不接受根上的 `$schema`、`$id` 或 `$defs` 里的 `title`，由调用方剥离后再传，不改 Schema。
- 与 [architecture.md §4.2](../../docs/architecture.md#42-镜头shot示例) 示例的有意差异：v0 把地点和时间放在场的 `setting` 上（§4.2 在镜头上用 `location_id`、`time_of_day`）；没有 `order`（数组顺序即播出顺序）、`beat`、`lens_mm`、`position`、`look_id`、`location_id`、`duration.resolved_s`、`generation_policy`、`locks`，也没有 Take、Bible、Location、Prop、Look 实体。这些由 P0-14 / P1-03 决定 v1 的形态。

## 结构

| 对象 | 字段 | 说明 |
|---|---|---|
| 根 | `ir_version`、`series`、`characters[]`、`episodes[]` | v0 的 `ir_version` 为 `0.x.y` |
| series 剧 | `id`、`title`、`logline`、`genre`、`visual_style`、`aspect_ratio`、`language` | `logline` 是 P0-03 剧本生成的输入；`aspect_ratio` ∈ 9:16 / 16:9 / 1:1 |
| character 角色 | `id`、`name`、`gender`、`age`、`role`、`bio`、`appearance`、`costume`、`voice` | `appearance`、`costume` 供 P0-06 定妆；`voice` 供 P0-05 配音 |
| episode 集 | `id`、`number`、`title`、`synopsis`、`target_duration_s`、`scenes[]` | |
| scene 场 | `scene_id`、`setting`{`location`、`int_ext`、`time_of_day`、`description`}、`summary`、`mood`、`shots[]` | `mood` 供 P0-10 选 BGM |
| shot 镜头 | `shot_id`、`duration`{`hint_s`}、`framing`{`shot_size`、`angle`、`movement`、`composition`}、`description`、`lighting`、`characters[]`{`character_id`、`action`、`emotion`}、`dialogue[]`、`sfx[]` | `description` 是与模型无关的画面描述（P0-07）；空镜的 `characters` 为空 |
| line 台词 | `line_id`、`speaker`、`kind`、`text`、`delivery`{`emotion`、`intensity`、`speed`} | `kind`：`dialogue` 出镜说话、需要口型；`voiceover` 画外音或内心独白、不做口型（P0-09） |

枚举值见 Schema 中各字段的 `enum`。

## 校验

```bash
cd packages/drama-ir/python
python3 -m dramio_drama_ir validate ../examples/v0/ep01.json            # 退出码 0 通过 / 1 不合法 / 2 用法或读取错误
python3 -m dramio_drama_ir validate --strict ../examples/v0/ep01.json   # 警告也算失败
python3 -m dramio_drama_ir validate --json a.json b.json                # 机器可读输出
python3 -m dramio_drama_ir render ../examples/v0/ep01.json > ../examples/v0/ep01.md
python3 -m unittest discover -s tests -t .                              # 单元测试
```

在仓库根目录：`make drama-ir-check`（单元测试 + 对 `examples/v0/*.json` 做 `validate --strict`）。

先做结构校验；结构无误后再做语义校验。错误信息带 JSON 路径，例如：

```
ep01.json: 错误 $.episodes[0].scenes[1].shots[2].dialogue[0].speaker: 引用了不存在的角色 'char_x'
```

语义规则（阈值集中定义在 `python/dramio_drama_ir/checks.py`，P0-14 修订）：

| 级别 | 规则 |
|---|---|
| 错误 | `ir_version` 为 `0.x.y`；所有 id 匹配 `^[a-z][a-z0-9_]*$` 且在文档内唯一；台词 `speaker` 和镜头 `character_id` 指向已声明的角色；同一镜头内角色不重复；角色、集、场、镜头都至少 1 个；集数从 1 开始且不重复；`0 < hint_s ≤ 10`；`0 ≤ intensity ≤ 1`；`0.5 ≤ speed ≤ 2`；年龄 1–120；文本不能为空 |
| 警告 | 一集镜头时长合计与 `target_duration_s` 偏差超过 20%；台词估算朗读时长（每秒 4.5 字，不计标点）超过镜头时长；`kind=dialogue` 的说话人不在该镜头画面中；角色从未出镜也没有台词 |

## 在 spikes 中调用

```bash
cd spikes/poc
PYTHONPATH=../../packages/drama-ir/python python3 -c "import dramio_drama_ir"
```

```python
import json
from dramio_drama_ir import load_schema, validate

schema = load_schema("v0")        # dict，可直接交给 LLM 做结构化输出
report = validate(json.loads(text))
if not report.ok(strict=True):
    feedback = "\n".join(str(i) for i in report.errors + report.warnings)   # 回喂给 LLM 重试
```

## 标准样例（固定输入）

`examples/v0/ep01.json`：《雨夜反击》第 1 集，中文竖屏，2 个角色，3 场，13 个镜头，15 句台词，镜头时长合计 60 秒；包含空镜、道具插入、画外音、音效和多种场景情绪，结尾留有悬念。画面中需要出现的文字（如手机短信）标注为后期叠加，不交给图像模型生成。可读版本见 [ep01.md](examples/v0/ep01.md)。

- sha256：`cd02daef022c6e78227ef3bf366adb2b5142a979a803bcf4373cdb8765a6fb59`
- 经用户确认（待决事项 D-002）后**冻结**：P0-03 ~ P0-13 的评测都以它为固定输入，不再原地修改。需要修订时新增文件（如 `ep01.r2.json`），并在这里记录新的 sha256。
