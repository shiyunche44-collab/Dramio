# bgm.v1：场景背景音乐提示词（P0-10，豆包音乐 GenBGMForTime v5.0）

每个场景一条，`$变量` 在运行时从 DramaIR 的 `series` 与 `scenes[]` 渲染（不另建结构，INV-02）。
接口 `Text` 只支持中文，所以全文中文；v5.0 不再需要单独的曲风 / 心情 / 乐器参数，直接写进描述。
变量：$title（series.title）、$genre（series.genre）、$time_of_day（白天 / 夜晚 / 清晨 / 黄昏，来自 setting.time_of_day）、
$int_ext（室内 / 室外，来自 setting.int_ext）、$location、$setting_desc（setting.location、setting.description）；
$style、$emotion、$instruments、$tempo 来自 `poc/music.py` 的 `MOOD_TABLE`（按 scene.mood 查表，键与 schema 枚举一一对应，单测校验）。
不放 scene.summary：摘要里有人名与剧情，对纯音乐没有帮助，还可能被当成歌词。
写足描述是故意的：文档提示“入参简单的 30 秒短音乐容易触发版权校验（50000001）”。
解析规则：以 `## <节名>` 开头，到下一个 `## ` 为止；节内以 `>` 开头的行是说明，不进入提示词。

## bgm
> 场景 BGM（纯音乐）。
为短剧《$title》（类型：$genre）的一场$time_of_day$int_ext戏配背景音乐，地点是$location：$setting_desc。纯音乐，无人声，无歌词，没有旁白。曲风：$style。情绪：$emotion。乐器：$instruments。速度：$tempo。作为对白下方的背景音乐使用：旋律简洁，动态平稳，中低频不抢人声，不要突然的高潮或爆发，结尾自然收束并渐弱。
