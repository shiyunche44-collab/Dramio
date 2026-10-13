#!/usr/bin/env bash
# P0-13 规模抽测：串行冷运行 3 集（--video placeholder）。可重复执行：已完成节点命中缓存，被打断后直接重跑即可。
# 用法：cd spikes/poc && nohup bash scripts/p0_13_batch.sh > <日志文件> 2>&1 &
set -u
cd "$(dirname "$0")/.."
OUT_DIR="${P0_13_OUT_DIR:-runs/p0-13}"
mkdir -p "$OUT_DIR"
LOGLINES=(
  "暴雨夜，被开除的实习生在电梯里捡到总裁遗落的U盘，里面是一份足以让整个集团覆灭的账本。"
  "三年后，她以匿名买家的身份回到旧公司，在拍卖会上当众拍下那份毁掉她的合同，对面坐着当年的主谋。"
  "他以为自己是替身，直到她在婚礼前夜递给他一份遗嘱，上面写着真正的继承人是他。"
)
i=0
for L in "${LOGLINES[@]}"; do
  i=$((i+1))
  echo "=== 第 $i 集 $(date -u +%FT%TZ) ==="
  python3 -m poc run --logline "$L" --video placeholder --max-cost-cny 20 --out-dir "$OUT_DIR/ep$i"
  echo "exit=$? 第 $i 集 $(date -u +%FT%TZ)"
done
echo "=== 全部结束 $(date -u +%FT%TZ) ==="
