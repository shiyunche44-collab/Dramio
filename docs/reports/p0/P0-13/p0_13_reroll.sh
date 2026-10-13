#!/usr/bin/env bash
# P0-13 重抽实验：对首帧人脸相似度 < 0.45 的镜头各 --reroll 一次（其余节点命中缓存）。需要先跑完 p0_13_batch.sh。
# 注意：镜头 id 依赖 p0_13_batch.sh 产生的缓存（冷启动重跑时分镜可能不同，id 会失效）。
set -u
cd "$(dirname "$0")/.."
OUT_DIR="${P0_13_OUT_DIR:-runs/p0-13-reroll}"
L1="暴雨夜，被开除的实习生在电梯里捡到总裁遗落的U盘，里面是一份足以让整个集团覆灭的账本。"
L2="三年后，她以匿名买家的身份回到旧公司，在拍卖会上当众拍下那份毁掉她的合同，对面坐着当年的主谋。"
L3="他以为自己是替身，直到她在婚礼前夜递给他一份遗嘱，上面写着真正的继承人是他。"
run() { python3 -m poc run --logline "$1" --video placeholder --max-cost-cny 8 --out-dir "$OUT_DIR/$2" "${@:3}"; echo "exit=$? $2 $(date -u +%FT%TZ)"; }
run "$L1" ep1 --reroll keyframe:ep01_sc02_sh01 --reroll keyframe:ep01_sc01_sh01
run "$L2" ep2 --reroll keyframe:ep01_sc01_sh04 --reroll keyframe:ep01_sc04_sh03 --reroll keyframe:ep01_sc04_sh02 --reroll keyframe:ep01_sc01_sh02
run "$L3" ep3 --reroll keyframe:ep01_sc03_sh01 --reroll keyframe:ep01_sc02_sh04 --reroll keyframe:ep01_sc04_sh03
echo "=== 重抽全部结束 $(date -u +%FT%TZ) ==="
