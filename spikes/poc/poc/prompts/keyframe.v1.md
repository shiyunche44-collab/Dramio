# keyframe.v1：镜头首帧提示词（P0-07，Seedream 5.0 pro）

每节是一个模板，`$变量` 在运行时从 DramaIR 渲染（不另建结构，INV-02）：
- 公共变量：$style（series.visual_style）、$location / $int_ext / $time_of_day / $scene_description（scene.setting）、
  $shot_size / $angle / $composition（shot.framing，景别与机位译成中文）、$description（shot.description）、$lighting（shot.lighting）。
- shot 节追加 $characters（每个出镜人物一行，由 character 节渲染）、$reference（ref1 / ref2 方案由 reference 节渲染；text 方案为空，整行丢弃）。
- character 节变量：$name、$age、$gender、$appearance、$costume（characters[]），$action、$emotion（shot.characters[]）。
- reference 节变量：$mapping（“图1是苏晚的全身定妆照，图2是陆沉的全身定妆照”，图序固定为 characters[] 的顺序）。
解析规则：以 `## <节名>` 开头，到下一个 `## ` 为止；节内以 `>` 开头的行是说明，不进入提示词。

## shot
> 有角色的镜头（text / ref1 / ref2 三种方案共用同一模板，差别只在 $reference 这一行）。
竖构图 9:16 电影画面，写实电影感摄影，这是镜头的第一帧。剧集视觉风格：$style。
场景：$location（$int_ext，$time_of_day），$scene_description。
镜头：$shot_size，$angle；构图：$composition。
画面内容：$description。
光线：$lighting。
出镜人物：
$characters
$reference
只描述镜头起始瞬间的静止姿态，不画动作过程。画面中不出现任何字幕、文字、水印或标志；画面底部 1/4 只放环境或留白（字幕安全区），不放置人物面部、手和重要物体。

## character
> 一个出镜人物：外貌与服装以文字为准（参考图方案也一样）。
- $name（$age 岁中国$gender）：外貌 $appearance；服装 $costume；起始姿态 $action；神情 $emotion。

## reference
> 参考图说明：只用于人物身份。
参考图：$mapping。参考图只用于确定人物的身份（脸部五官、脸型、发型），场景、姿态、构图、服装一律以文字描述为准，不要照抄参考图的背景、姿态和构图。

## empty
> 空镜（无角色）：各方案共用 1 张，只描述场景。
竖构图 9:16 电影画面，写实电影感摄影，这是空镜头的第一帧。剧集视觉风格：$style。
场景：$location（$int_ext，$time_of_day），$scene_description。
镜头：$shot_size，$angle；构图：$composition。
画面内容：$description。
光线：$lighting。
画面中没有任何人物、人脸、人影、手或身体部位，只有环境。
只描述镜头起始瞬间的静止画面。画面中不出现任何字幕、文字、水印或标志；画面底部 1/4 只放环境或留白（字幕安全区）。
