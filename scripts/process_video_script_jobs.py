#!/usr/bin/env python3
"""Host-side writer for video-studio script jobs (mode='video').

Mirrors the structure of process_pending_voice_jobs.py:
- Triggered by .video-script-trigger (systemd path unit)
- Calls Claude directly (via llm_client) to generate the narration script
  + cover; the daemon writes runs/<id>/script.txt + runs/<id>/cover.json
  itself (no agent, no file-writing tools)
- On success: status -> ready_script, then touches .video-render-trigger

Differences from voice writer:
- Job dir is jobs/video/ (not jobs/voice/)
- Job id prefix is 'v_' (not arbitrary UUID)
- Status target is 'ready_script' (not 'ready')
- Min chars is 700 (not 3300) — video scripts target about 150 seconds
- On success, cascades to render trigger (voice writer has no successor)
"""

import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm_client


SKILL_DIR = Path(__file__).resolve().parents[1]
JOBS_DIR = SKILL_DIR / "jobs" / "video"
RUNS_DIR = SKILL_DIR / "runs"
LOCK_PATH = SKILL_DIR / ".video-script-writer.lock"
# Char-count tolerance band. The style guide targets 560-640 chars
# (see reference-style-video.md, 抖音科普短片节奏更紧凑), but LLM output
# is noisy — widened to 300-1200 to support both short-form (300+ chars
# e.g. 抖音小知识/科普短文案) and long-form (200s 抖音科普对标大约
# 1080 字, 上限 1200 留余量).
MIN_SCRIPT_CHARS = 300
MAX_SCRIPT_CHARS = 1200
DEFAULT_TARGET_SECONDS = 110
# Empirically calibrated from MiniMax-TTS (model=speech-2.8-hd) Radio_Host:
# 实测 628 字 / speed 1.15 → 112.9s (5.56 chars/sec), 644 字 → 110.1s (5.85)。
# 中文 TTS 实际节奏受标点/换气影响大；为了不过分欠长，多留 4% 余量。
ESTIMATED_CHARS_PER_SECOND = 5.4
# LLM 输出密度 vs TTS 读速: 实测 LLM 自然产出 ~5 chars/sec (300s 视频
# 产出 1515 字, 5.05 chars/sec)，跟 TTS 速率接近。所以 prompt target + 长度
# 校验都直接用 ESTIMATED_CHARS_PER_SECOND 算 (不再单独定义 LLM rate)。
DRIFT_SAFETY_SECONDS = 5
# 1 initial write + up to 2 targeted length-repair passes (expand/trim).
# writer_attempt doubles as the retry cap and the session-key suffix.
MAX_WRITER_ATTEMPTS = 3


def script_length_bounds(duration_sec: int) -> tuple[int, int]:
    """Duration-aware script char budget: (min_chars, max_chars).

    Replaces the old hardcoded 300-1200 cap. Long videos need proportionally
    more script (300s → 1620 字 target, accepted 1134-2206), and short
    videos get a 300-char floor so a 60s demo isn't forced to write 0.

    Floor: MIN_SCRIPT_CHARS=300 for short videos (target < ~430 chars);
    for longer videos, min scales to 70% of target so a 300s video must
    have ≥ 1134 字 to fill the runtime. Cap: 130% of target + 100-char
    buffer for LLM noise.
    """
    target = int(duration_sec * ESTIMATED_CHARS_PER_SECOND)
    if target >= 430:
        min_chars = int(target * 0.7)
    else:
        min_chars = MIN_SCRIPT_CHARS
    max_chars = int(target * 1.3) + 100
    return min_chars, max_chars

SCRIPT_TRIGGER = SKILL_DIR / ".video-script-trigger"
DIRECTOR_TRIGGER = SKILL_DIR / ".video-director-trigger"
RENDER_TRIGGER = SKILL_DIR / ".video-render-trigger"
NARRATE_TRIGGER = SKILL_DIR / ".video-narrate-trigger"
LAST_RUN_MARKER = SKILL_DIR / ".video-script-writer.lastrun"
REFERENCE_STYLE = SKILL_DIR / "reference-style-video.md"
LOG_FILE = Path("/var/log/video-studio/video-script-watcher.log")


# ----- MEME_GUIDE: 5 段子 verbatim use rules (replaces old GOOD_EXAMPLES / HOOK_TEMPLATES / ANTI_PATTERNS) -----
MEME_GUIDE = '''## [xingzhe] 风格 + 段子 (完整对标, 必读)

完全模仿 benchmarks/xingzhe/analysis.md 的 20 篇顶部视频风格.
**风格 = 骨架, 段子 = 装饰, 装饰不能挤掉骨架**.

### 1. Hook 公式 (前 5 秒必落地一种)

- **A 反问 + 立即给答案** (主力 70%): "[X 能不能 Y?] 直接说答案, [Z]。"
- **B 假设 + 立即给答案** (~25%): "如果 X 会怎样? 直接说答案, [Y]。"
- **C 反问 + 反问** (~5%): "[X?] 难道 [Y] 吗? 唉, [真正原因]。"
- **D 反常识开场** (5%, 慎用): "你以为 X, 其实 Y。"

禁用: "今天我们来聊聊" / 自介 / 抒情陈述 / 第 1 段铺背景.

### 2. 编号结构 (主题偏职场/经济/常识时强制)

**Setup 必须有 (避免突兀):** Hook → 1 句 setup → 第一笔/第一层/第一波
- 模板: "**这事咱们拆 [N] 笔算, 从 X 到 Y 全过一遍.**" / "**这场战争分 [N] 波打.**" / "**这事分 [N] 层看.**"
- Setup 解释"为什么用编号", 10-25 字, **不要在 setup 里给答案**

**3 种编号选型** (按主题):
- **第一笔 / 第二笔 / 第三笔** (v_bench_new01/03 风格) → 经济/职场/历史账目
- **第一层 / 第二层 / 第三层** (v_bench_new02 风格) → 系统/机制/制度
- **第一波 / 第二波 / 第三波** (rank_06 狼群) → 攻击/演化/时间线

**每笔/层/波开场格式**:
- ✓ 1 句话**直接陈述本段主题** ("第一笔, 25 岁的 P5" / "第一笔, 雨." / "第一笔, 基层市集.")
- ✓ 然后 80-150 字展开 (用具体数字 + 数学对比 + 段子化比喻)
- 至少 2 段.

**反模式**:
- ❌ Hook → 直接跳 "第一笔" (无 setup, 突兀)
- ❌ "第一笔, 主角不是 X, 是 Y." (反转在开场用 = 浪费段中段尾的反转位)
- ❌ "第一笔, 来源." (抽象名词, 不具体)

### 3. 中段钩子 (10-15s 一卡, 3-4 个/视频)

4 选 1:
- 具体数字 + 单位 (数字密度 ≥10/视频)
- 段子化比喻 (历史人物 + 现代动作)
- 数学对比 (A 倍 / 1/N / 算一下 → N)
- 跨学科引用 (三体人 / 地球online / 恐怖直立猿)

**密度要求**: 数字 ≥10, 数学对比 ≥2, 反转词 (但是/其实/真相是) ≥3.

### 4. 段子化金句 4 种结构 (必背)

1. **X——Y 破折号反差** (主力 ≥50%): "夏侯惇鉴宝——一眼假" / "你买的房——其实是 30 年劳动期货"
2. **重复对称** (≥10%): "灵魂下班了, 肉体还在加班"
3. **跨作品引用** (≥15%): "三体人觉得这个算法很暴力"
4. **游戏化/段位化** (≥10%, 必递进 3 段): "恐怖直立猿 → 持械 → 热武器"

### 5. reference-memes.md 9 条段子 (verbatim, 一字不改)

#1 夏侯惇鉴宝——一眼假 | #2 恐怖直立猿 (3 段递进) | #3 地球online
#4 夏侯惇看司马迁——一眼望不到边 | #5 夏侯惇的不屑 | #6 夏侯惇看杨戬——四目相对
#7 太监开会——无稽之谈 | #8 路易十六的生日——过到头了 | #9 4 字 meme barrage

使用规则:
- 9 条全部 verbatim, 不改字不增字不仿写不自创
- 主题契合才用, 不强求密度 (0-9 条都行)
- 段子必须服务脚本主题, 严禁末尾/开头/中间单独加"冷知识/彩蛋/bonus"包装
- 同一种子不重复: 1 个脚本里夏侯惇最多 1 次 (段子 #1/4/5/6 同一脚本最多选 1 条)

### 6. 结尾公式

[最后一段: 用"段子化金句"砸一次核心论点]
+ (可选) 强反常识金句 (不用 [xingzhe] 的"说出吾名", 我们调性不同)
禁用: "以上就是…" / "希望对你有帮助" / 治愈系 / 抒情

### 7. 调性边界

- 准: 反派 + 戏说古人 (非正史英雄) + 段子化自创 (地球online)
- 不准: 悲剧英雄 (项羽/岳飞/关羽) + 革命先烈 + 受害者
- 不准: 治愈/松弛/愿你/希望 + 抒情 ("把 X 揉进 Y 里")
- 不准: 公众号爆款词堆砌 (KPI/甲方/群消息 5 个名词并排)
- 不准: "熬的不是夜, 是 X" 重复

### 8. 跟 style doc 的关系

reference-style-video.md = 主结构 (4 hook + 4 段子 + 编号 + 节奏 + 自检)
reference-memes.md = 装饰 (9 条 [xingzhe] 段子)
**装饰不能挤掉主结构**: 数字 ≥10, 数学 ≥2, 反转 ≥3 必须保留.
'''

COVER_INSTRUCTIONS = '''## 封面文案 (独立于正文, 额外生成)

封面是视频前 2.5 秒的大字冲击: 不念出来, 视觉冲击用。基于主题 + 已写正文, 生成 3 个字段, 写入 jobs/video/<job_id>.json 的 script_meta.cover:

- main: 4-6 字主标 (中文按汉字计, 英文按单词计)。**必须是钩子** —— 反常识判断 / 数字冲击 / 跨学科对比 / 颠覆认知 (4 选 1), 不是平铺直叙。**不准问号/句号结尾** (问号句在主标上点击率低)。不准直接用正文首句, 必须是对全文主题的二次提炼 (先写完正文再回头写 main)。
  - 好例子: "糖不是调味品" (反常识) / "糖是战略物资" (颠覆认知)
  - 坏例子: "糖在二战被列为" (机械截断, 不是钩子)
  - 坏例子: "糖为什么被列" (问号句, 不准)
- main_highlight: [start, end) 半开区间, 标注 main 里**最关键的钩眼词**。**必须是 1 个语义完整的词, 不准是 0.5 个词** (e.g. "糖不是调味品" 应该高亮 "不是" [1,3], 不准高亮 "是调" [2,4] —— "是调" 不是 1 个词, 是 "是" 半个 + "调" 半个, 视觉上散)。允许的钩眼词类型 (4 选 1):
  1. **否定/转折单字**: 不 / 没 / 非 / 却 / 但 / 竟 / 倒 / 反 (整个 hl 就 1 个字, 强)
  2. **否定/转折双字**: 不是 / 实际 / 并非 / 然而 / 但是 / 不过 / 竟然 / 居然 / 根本 (2 字, 强)
  3. **数字/数字+单位**: 50% / 2 倍 / 2024 / 一半 / 十分之一
  4. **核心名词**: 战略 / 燃料 / 成本 / 命 / 真相
  - **不准落在第 1 字** (首字当 hook 冲击不够), **不准落在最后 1 字** (看不全), 范围 ≤3 字
  - LLM 自己挑, 但必须符合上述 4 类之一, 否则会被代码层 reject
- sub: 12-18 字副标。**严禁剧透主标答案** —— 不准用"因为...所以..."/"其实...就是..."/"真相是..."/"直接说答案..."这类把答案解释完的句式。做 3 件事之一:
  - (a) 加数字/事实: 主标抽象, 副标给具体 (例: main="糖不是调味品" sub="二战真相比你想的更狠")
  - (b) 抛问题/对比引好奇, 不剧透 (例: sub="可口可乐的配方里有它")
  - (c) 反差/颠覆细节 (例: sub="连监狱都限购")
  - 坏例子 (sub="直接说答案,因为糖的本质不是调味品" — 剧透主标答案, 封面杀手)
  - 坏例子 (sub="糖的本质不是调味品" — 跟 main 重复)
  - 坏例子 (sub="它的真相让你吃惊" — 治愈/松弛系, 不准)

例 1 (好, main 钩子 + hl 是 1 个完整词 + sub 留悬念):
  正文 = "糖在二战被列为战略物资, 并非调味品, 而是热量来源"
  cover = {"main": "糖不是调味品", "main_highlight": [1, 3], "sub": "二战真相比你想的更狠"}
  # hl="不是" 是 1 个完整词 (否定双字), 钩眼

例 2 (反例, hl 不是 1 个完整词, 会被代码 reject):
  正文同上
  cover = {"main": "糖不是调味品", "main_highlight": [2, 4], "sub": "..."} ✗
  # hl="是调" 不是 1 个完整词, 是 "是"(半个) + "调"(半个), reject

例 3 (反例, 不要这样写):
  正文同上
  cover = {"main": "糖为什么被列", "main_highlight": [0, 2], "sub": "直接说答案,因为糖的本质不是调味品"} ✗
  # main 是问号句; hl 落在第 1 字; sub 剧透主标答案

硬要求:
- 字符 index 必须在 main 字符串长度内 (防 OOB)
- main 高亮必须是 1 个语义完整的词 (4 类钩眼词之一, 见上)
- **高亮不准落在第 1 字 (start > 0), 也不准落在最后 1 字 (end < len(main))**
- main 不准问号/句号结尾 (钩子不准是问句)
- **sub 不准含 "因为 / 所以 / 其实 / 真相是 / 实际上 / 答案是 / 直接说 / 本质是" 这类剧透主标答案的词**
- 不准用治愈/松弛/愿你/希望你/愿大家类词

输出方式: 写入 runs/<job_id>/cover.json, 内容:

```json
{
  "main": "糖不是调味品",
  "main_highlight": [1, 3],
  "sub": "二战真相比你想的更狠"
}
```
'''

def log(msg):
    line = f"[video-script-writer] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def job_path(job_id):
    return JOBS_DIR / f"{job_id}.json"


def load_job(path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_job(job):
    job["updated_at"] = now_iso()
    tmp = job_path(job["id"]).with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)
    os.replace(tmp, job_path(job["id"]))


def pending_jobs():
    jobs = []
    if not JOBS_DIR.exists():
        return jobs
    for path in JOBS_DIR.glob("v_*.json"):
        try:
            job = load_job(path)
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("mode") == "video" and job.get("status") == "pending":
            jobs.append(job)
    return sorted(jobs, key=lambda j: j.get("created_at", ""))


def _read_reference_caches():
    """Load style + memes reference text once per call (small files,
    ~14KB combined). Missing files yield empty string — caller logs."""
    parts = []
    for label, path in (("reference-style-video.md", REFERENCE_STYLE), ("reference-memes.md", SKILL_DIR / "reference-memes.md")):
        try:
            txt = path.read_text(encoding="utf-8")
        except OSError:
            log(f"reference missing: {path}")
            txt = ""
        parts.append(f"## {label}\n\n{txt}")
    return "\n\n".join(parts)


def build_prompt(job):
    """Single-completion prompt. Returns instruction text for the LLM to
    produce JSON {script, cover} in one response. The daemon does the
    file writes itself; the LLM is just the prose engine."""
    theme = job.get("theme") or ""
    target_seconds = int(job.get("render", {}).get("duration_sec") or DEFAULT_TARGET_SECONDS)
    min_chars, max_chars = script_length_bounds(target_seconds)
    target_chars = int(target_seconds * ESTIMATED_CHARS_PER_SECOND)
    references = _read_reference_caches()
    return (
        f"为 video-studio 写一段约 {target_seconds} 秒 ({target_chars} 字) 的短视频旁白稿。\n"
        f"主题：{theme}\n\n"
        f"## 参考风格 + 参考热梗 (已直接附在下面, 不需要再读文件)\n"
        f"风格 = 骨架, 热梗 = 装饰, 装饰不能挤掉骨架。\n\n"

        f"{references}\n\n"

        f"{MEME_GUIDE}\n\n"

        f"{COVER_INSTRUCTIONS}\n"

        f"## 硬约束 (优先级最高)\n"
        f"1. 字数硬上限 {max_chars} 字, 目标 {target_chars} 字. **超过 1500 字直接判失败**, 不要尝试写 3000+ 字长稿\n"
        f"2. 脚本正文 (script 字段) 纯文本, 不要 markdown / 编号 / 标题 / 空行分隔\n"
        f"3. 开头 60-90 字内必须出现: 反问 + 立即给答案 / 假设 + 给答案 / 反常识判断 (三选一, 必命中 A 主力 70%)\n"
        f"4. 中段每 10-15 秒一个钩子: 具体数字 / 段子化破折号 / 数学对比 / 跨学科引用 (四选一)\n"
        f"5. 数字密度: >= {int(target_chars/100)} 个数字 (含中文) 在全文, 数学对比 >=2 个 (A 倍 / 约等于)\n"
        f"6. 反转密度: 但是/其实/真相是/实际上 类词 >=3 个\n"
        f"7. 结尾禁止: 开放式问号 / 治愈系 / 以上就是... / 希望对你有帮助 / 抒情\n"
        f"8. 段子: 从 reference-memes.md 9 条 [xingzhe] 库里挑合适的直接用, 强约束见 MEME_GUIDE §5 (verbatim / 服务主题 / 同种子不重复)\n"
        f"9. 编号结构: 主题偏职场/经济/常识时, **Hook 后必须先有 1 句 setup** (如 '这事咱们拆三笔算') **再进第一笔/第一层/第一波**, 至少 2 段. 编号开场直接陈述本段主题, **不在编号开场用反转 ('主角不是 X, 是 Y' 浪费钩子, 严禁)**\n"
        f"10. 写完自检: 5 个连续名词并排? 同一句式用了 2 次? 有治愈/松弛/温柔吗? 字数是否在 {min_chars}-{max_chars} 区间? 段子是否服务主题 (没末尾冷知识/bonus)? 任何一项不通过就重写\n"
        f"11. 不要尝试用 N 段完整 4 层结构堆长度, 一段层只算一个反转, 4 层反转 + 中间段子 = 600-800 字就够\n\n"

        f"## 输出格式 (严格遵守)\n"
        f"只输出一个 JSON 对象, 不要 markdown fence, 不要任何额外说明文字:\n"
        f"{{\n"
        f'  "script": "<完整旁白稿, {min_chars}-{max_chars} 字, 纯文本, 无 markdown>",\n'
        f'  "cover": {{\n'
        f'    "main": "<4-6 字钩子主标>",\n'
        f'    "main_highlight": [<start>, <end>],\n'
        f'    "sub": "<12-18 字副标, 不剧透主标>"\n'
        f"  }}\n"
        f"}}\n\n"
        f"main_highlight 是 main 的 [start, end) 半开区间, 标出**最关键的钩眼词**; 高亮必须是 1 个语义完整的词 (见 COVER_INSTRUCTIONS §main_highlight 4 类钩眼词), 不准是 0.5 个词; start > 0 (不准落在第 1 字), end < len(main) (不准落在最后 1 字), end - start <= 3.\n"
        f"一次写完即终稿。不要在 JSON 前后加任何 prose / markdown / 解释。"
    )


def build_repair_prompt(job, current_script, min_chars, max_chars):
    """Targeted length-repair prompt. The LLM is asked to return a JSON
    {"script": <revised>, "cover": <unchanged or re-validated>}; the daemon
    writes the file. Replaces the old agent that self-overwrote script.txt."""
    target_seconds = int(job.get("render", {}).get("duration_sec") or DEFAULT_TARGET_SECONDS)
    target_chars = int(target_seconds * ESTIMATED_CHARS_PER_SECOND)
    cur_len = len(current_script)
    if cur_len < min_chars:
        gap = min_chars - cur_len
        direction = (
            f"当前 {cur_len} 字, 比下限少 {gap} 字. 在保留开头钩子 / 中段钩子 / 结尾风格的前提下, "
            f"补充具体数字 / 段子化金句 / 跨学科细节来扩写, 不要堆砌空洞名词, 不要改写已有好句子."
        )
    else:
        gap = cur_len - max_chars
        direction = (
            f"当前 {cur_len} 字, 比上限多 {gap} 字. 删减冗余 / 重复 / 空洞处, 保留所有钩子和数字密度, "
            f"不要改写已有好句子."
        )
    return (
        f"修复一篇已写好的视频旁白稿, 只调长度不改风格.\n"
        f"主题：{job.get('theme') or ''}\n"
        f"目标时长 {target_seconds}s, 字数必须落在 {min_chars}-{max_chars} 区间 (目标 {target_chars} 字).\n"
        f"{direction}\n\n"
        f"## 当前全文 ({cur_len} 字)\n{current_script}\n\n"
        f"## 硬约束\n"
        f"1. 输出长度必须在 {min_chars}-{max_chars} 字区间内\n"
        f"2. 纯文本, 不要 markdown / 编号 / 标题 / 空行\n"
        f"3. 保留原有钩子结构 (反问/假设/反常识开场, 中段数字/段子, 非开放式结尾)\n"
        f"4. 只在必要处增删, 不要整篇重写\n\n"
        f"## 输出格式 (严格遵守)\n"
        f"只输出一个 JSON 对象, 不要 markdown fence:\n"
        f'{{"script": "<修订稿全文>", "cover": {{"main": "...", "main_highlight": [s, e], "sub": "..."}}}}\n'
        f"cover 字段如未改动可保持原值, 但必须重新验证 main_highlight 是否仍是语义完整的钩眼词."
    )


def _parse_script_response(text):
    """Extract {"script": str, "cover": dict} from one LLM response.

    Tolerant of: prose around the JSON, a wrapper envelope, a JSON array
    that contains the dict as its single element, truncation. Returns
    (script, cover, error_msg). On any parse failure, script=None and
    cover=None; caller decides whether to retry or fall back.
    """
    if not text:
        return None, None, "empty LLM response"

    def _as_cover(d):
        if not isinstance(d, dict):
            return None
        return d

    # 1) Direct JSON object
    for candidate in _iter_json_objects(text):
        if isinstance(candidate, dict) and isinstance(candidate.get("script"), str):
            return candidate["script"], _as_cover(candidate.get("cover")), None
        # Some models return a top-level array containing one such object
    # 2) Direct JSON array
    try:
        arr = json.loads(text)
    except json.JSONDecodeError:
        arr = None
    if isinstance(arr, list) and arr:
        for item in arr:
            if isinstance(item, dict) and isinstance(item.get("script"), str):
                return item["script"], _as_cover(item.get("cover")), None
    # 3) Recover a truncated object: if the response looks like `{"script": "..."`
    #     but was cut off, take whatever script field we can extract.
    # The regex matches any backslash escape OR any non-quote/non-backslash char,
    # so the captured group is the literal JSON string value with \" and \\
    # still present — just unescape those two, don't run unicode_escape on
    # UTF-8 bytes (that mojibakes CJK).
    m = re.search(r'"script"\s*:\s*"((?:\\.|[^"\\])*)', text)
    if m:
        recovered = m.group(1).replace('\\"', '"').replace("\\\\", "\\")
        return recovered, None, "truncated JSON, recovered partial script"
    return None, None, f"could not parse script JSON; tail={text[-200:]!r}"


def _iter_json_objects(text):
    """Yield candidate dicts parsed from text, in order of likelihood.

    Cheap strategies first: direct json.loads, then naive regex extraction
    of `{...}` blocks (validated by json.loads)."""
    try:
        v = json.loads(text)
        if isinstance(v, dict):
            yield v
        return
    except json.JSONDecodeError:
        pass
    # Naive scan for {...} blocks — won't handle nested braces, but the
    # expected shape is flat enough that this catches the common cases.
    for m in re.finditer(r"\{[^{}\n]{2,2000}\}", text):
        try:
            v = json.loads(m.group(0))
            if isinstance(v, dict):
                yield v
        except json.JSONDecodeError:
            continue


def _write_run_artifacts(job_id, script_text, cover):
    """Write runs/<id>/script.txt + runs/<id>/cover.json. Daemon is the
    sole writer; the LLM only generates text."""
    run_dir = RUNS_DIR / job_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "script.txt").write_text(script_text, encoding="utf-8")
    if cover is not None:
        (run_dir / "cover.json").write_text(
            json.dumps(cover, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def generate_script(job):
    """One Messages-API call. Returns (script_text, cover_dict, error_msg).

    Writes script.txt + cover.json as a side effect so callers can read
    them back on retry without round-tripping through the LLM."""
    prompt = build_prompt(job)
    try:
        text = llm_client.complete(
            system=(
                "You are a 抖音 short-video script writer for the video-studio project. "
                "Follow every constraint literally. Output a single JSON object and nothing else."
            ),
            user=prompt,
            max_tokens=4096,
            timeout=300.0,
        )
    except Exception as e:
        return None, None, f"LLM call failed: {e}"

    script, cover, err = _parse_script_response(text)
    if script is None:
        return None, None, err

    validated_cover = parse_cover_validation(cover) if cover else None
    _write_run_artifacts(job["id"], script, validated_cover)
    return script, validated_cover, None


def generate_script_repair(job, current_script, min_chars, max_chars):
    """One Messages-API repair pass. Same artifact contract as generate_script."""
    prompt = build_repair_prompt(job, current_script, min_chars, max_chars)
    try:
        text = llm_client.complete(
            system=(
                "You revise a 抖音 short-video script for length only. "
                "Output a single JSON object: {\"script\": ..., \"cover\": ...}. No prose."
            ),
            user=prompt,
            max_tokens=4096,
            timeout=300.0,
        )
    except Exception as e:
        return None, None, f"LLM call failed: {e}"

    script, cover, err = _parse_script_response(text)
    if script is None:
        return None, None, err
    validated_cover = parse_cover_validation(cover) if cover else None
    _write_run_artifacts(job["id"], script, validated_cover)
    return script, validated_cover, None


def parse_cover_from_agent_result(job_id):
    """Read runs/<id>/cover.json written by the LLM agent.

    Returns a validated dict {main, main_highlight, sub} or None on any
    failure (file missing, JSON parse error, field validation).
    LLM output is noisy — every failure mode collapses to None and the
    render daemon falls back to cover_fallback(script).
    """
    cover_path = RUNS_DIR / job_id / "cover.json"
    if not cover_path.exists():
        return None
    try:
        data = json.loads(cover_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log(f"  cover.json unreadable for {job_id}")
        return None
    return parse_cover_validation(data)


def parse_cover_validation(data):
    """Validate LLM-produced cover dict. Returns the dict on pass, None on fail.

    Rules (v3):
    - main: non-empty string, 1-8 chars, must NOT end with ?/？/。/./!/！ (hook, not question)
    - main_highlight: 2-int [start, end), start > 0 (not first char), end < len(main)
      (not last char), end - start <= 3 (no full-span highlight)
    - sub: string <= 22 chars, must NOT contain spoiler phrases
      (因为/所以/其实/真相是/实际上/答案是/直接说/本质是)
    - v3.1 highlight must be a "semantic-complete word" — a hook char (negation /
      transition / number) or a known hook phrase. Rejects half-words like
      "是调" on "糖不是调味品" [2,4] — that's "是" (0.5 word) + "调" (start of
      "调味品" but truncated), not a complete semantic unit.
    """
    if not isinstance(data, dict):
        return None
    main = data.get("main")
    hl = data.get("main_highlight")
    sub = data.get("sub", "")
    if not isinstance(main, str) or not (1 <= len(main) <= 8):
        return None
    if not isinstance(hl, list) or len(hl) != 2:
        return None
    try:
        s, e = int(hl[0]), int(hl[1])
    except (TypeError, ValueError):
        return None
    if not (0 <= s < e <= len(main)):
        return None
    if not isinstance(sub, str) or len(sub) > 22:
        return None
    # v3: 高亮不准落在第 1 字 (首字当 hook 冲击不够)
    if s == 0:
        return None
    # v3: 高亮不准落在最后 1 字 (e 必须 < len(main))
    if e >= len(main):
        return None
    # v3: 高亮范围 ≤ 3 字 (不准全段高亮)
    if e - s > 3:
        return None
    # v3.1: 高亮必须是 1 个语义完整词 (钩眼词, 4 类之一)
    if not _is_valid_highlight(main[s:e]):
        return None
    # v3: main 不准问号/句号结尾 (钩子不准是问句)
    if main.rstrip().endswith(("?", "？", "。", ".", "!", "！")):
        return None
    # v3: sub 严禁剧透主标答案 (检测解释型句式词)
    _SPOILER = ("因为", "所以", "其实", "真相是", "实际上", "答案是", "直接说", "本质是")
    if any(p in sub for p in _SPOILER):
        return None
    return {"main": main, "main_highlight": [s, e], "sub": sub}


# v3.1 钩眼词白名单 —— 高亮必须是其中 1 类, 不在就 reject
_HOOK_SUBSTR = (
    # 否定单字
    "不", "没", "非", "未", "莫", "别", "无",
    # 转折单字
    "却", "但", "可", "倒", "反", "岂", "就", "才", "都", "竟", "正",
    # 否定/转折双字
    "不是", "并非", "然而", "但是", "不过", "可是", "当然", "竟然", "居然", "反而", "其实", "根本", "实际",
    # 程度/真假
    "真", "假", "最", "太", "极", "很", "再", "对", "错", "难", "虚", "实",
    # 数字
    "一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "百", "千", "万", "亿", "半", "双",
    # 核心钩眼名词 (反常识/颠覆)
    "战略", "成本", "燃料", "命", "底", "本质", "续命", "底层", "续", "真", "卡路里", "便宜", "贵",
)


def _is_valid_highlight(slice_):
    """v3.1: highlight slice must be a semantic-complete hook word.

    Accept if slice contains any hook substring (e.g. "不是" contains "不"
    AND "不是", "50%" contains "5"/"0" digits) OR is fully numeric/symbolic.
    Reject otherwise (e.g. "是调" — both are common chars with no hook value).
    """
    if not slice_:
        return False
    if any(c.isdigit() for c in slice_):
        return True
    if any(c in "%％" for c in slice_):
        return True
    if any(sub in slice_ for sub in _HOOK_SUBSTR):
        return True
    return False


def finalize_from_script_file(job):
    """If the agent wrote runs/<id>/script.txt, copy its content into the job."""
    script_path = RUNS_DIR / job["id"] / "script.txt"
    if not script_path.exists():
        return False
    script = script_path.read_text(encoding="utf-8").strip()
    if not script:
        return False
    # preview_only: accept shorter scripts (10s demo scripts can be <50 chars)
    is_preview = bool((job.get("render") or {}).get("preview_only", False))
    if is_preview:
        min_chars = 50
        max_chars = max(MAX_SCRIPT_CHARS, int((job.get("render") or {}).get("duration_sec", 10) * ESTIMATED_CHARS_PER_SECOND * 1.3) + 100)
    else:
        target_seconds = int((job.get("render") or {}).get("duration_sec") or DEFAULT_TARGET_SECONDS)
        min_chars, max_chars = script_length_bounds(target_seconds)
    if len(script) < min_chars or len(script) > max_chars:
        return False
    # RC2/RC5: prefer the rate measured by the last few final jobs (narrate
    # daemon writes script_meta.actual_rate = voice_seconds/char_count, i.e.
    # seconds per character). We need chars per second for the duration
    # formula, so flip it. Cold start falls back to the calibrated
    # 5.4 chars/sec × speed constant.
    char_count = len(script)
    speed = float((job.get("audio") or {}).get("speed", 1.0))
    history_sec_per_char = []
    try:
        for p in sorted(JOBS_DIR.glob("v_*.json"))[-10:]:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                spc = (d.get("script_meta") or {}).get("actual_rate")
                if (
                    d.get("status") == "final"
                    and isinstance(spc, (int, float))
                    and spc > 0
                ):
                    history_sec_per_char.append(float(spc))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
    except OSError:
        pass
    if history_sec_per_char:
        # 1 / mean(sec/char) = mean char/sec, but to stay robust against
        # outliers we convert each measurement back to char/sec then average.
        cps = [1.0 / s for s in history_sec_per_char]
        effective_rate = sum(cps) / len(cps)
    else:
        effective_rate = ESTIMATED_CHARS_PER_SECOND * speed
    target_seconds = round(char_count / effective_rate)
    # preview_only: respect user-specified duration exactly (10s demo =
    # user-supplied). Non-preview: keep a small 2% + 2s tail for
    # sentence-final pauses instead of the old 8% + 5s double buffer;
    # 30s minimum guards against microscopic shorts breaking the
    # hyperframes renderer.
    preview_only = bool((job.get("render") or {}).get("preview_only", False))
    if preview_only:
        video_duration_sec = int((job.get("render") or {}).get("duration_sec", 10))
        # Trust voice_seconds is close to user target; do NOT add +2 tail.
        # If TTS drifts shorter we let the mp4 end with a brief black tail.
    else:
        video_duration_sec = max(
            round(target_seconds * 1.02) + 2, 30,
        )
    # preview_only: skip the full render daemon (image fetch + hyperframes).
    # Status is set to "rendered" so the narrate daemon picks it up directly
    # and runs preview_caption_ffmpeg to produce a black-bg mp4.
    job["status"] = "rendered" if preview_only else "ready_script"
    job["script"] = script
    # 封面: LLM 写 runs/<id>/cover.json, 解析失败/字段越界就 None, render 端走 fallback
    cover = parse_cover_from_agent_result(job["id"])
    job["script_meta"] = {
        "char_count": char_count,
        "target_seconds": target_seconds,
        "effective_rate": effective_rate,
        "actual_seconds": None,
        "cover": cover,
    }
    # 单一时间预算：render 读这个值，TTS 后用 ffprobe 校准，drift 控制在 ±1s
    job.setdefault("render", {})["duration_sec"] = video_duration_sec
    job["error"] = None
    job["updated_at"] = now_iso()
    save_job(job)
    return True


def repair_script_length(job, min_chars, max_chars):
    """Targeted length-repair loop. Each iteration calls the LLM once and
    writes runs/<id>/script.txt; finalize re-validates length. writer_attempt
    is the retry cap (1 initial + up to MAX_WRITER_ATTEMPTS-1 repairs)."""
    current_script = (job.get("script") or "").strip()
    if not current_script:
        script_path = RUNS_DIR / job["id"] / "script.txt"
        if script_path.exists():
            current_script = script_path.read_text(encoding="utf-8").strip()
    if not current_script:
        return False  # nothing to repair from — let caller error out

    while int(job.get("writer_attempt") or 0) < MAX_WRITER_ATTEMPTS:
        attempt = int(job.get("writer_attempt") or 0) + 1
        job["writer_attempt"] = attempt
        job["status"] = "repairing"
        job["error"] = None
        save_job(job)

        script, cover, err = generate_script_repair(job, current_script, min_chars, max_chars)
        if err:
            log(f"{job['id']} repair attempt {attempt} API error: {err[:200]}")
        if script is not None:
            current_script = script

        fresh = load_job(job_path(job["id"]))
        if finalize_from_script_file(fresh):
            save_job(fresh)
            log(f"{job['id']} repaired to {len(fresh['script'])} chars (attempt {attempt})")
            return True

        # Still out of range — current_script is whatever the last pass
        # wrote to disk (or the previous version if the LLM failed).
        sp = RUNS_DIR / job["id"] / "script.txt"
        if sp.exists():
            latest = sp.read_text(encoding="utf-8").strip()
            if latest:
                current_script = latest
        job = fresh
    return False


def process_one(job):
    """One script job end-to-end: write status, call LLM, finalize.

    Replaces the old run_agent + scrape_session_error pipeline. The LLM
    is stateless — no agent session, no file-writing tools. The daemon
    owns every disk write."""
    job["status"] = "writing"
    job["error"] = None
    job["writer_attempt"] = int(job.get("writer_attempt") or 0) + 1
    save_job(job)

    script, cover, err = generate_script(job)
    if err:
        job["status"] = "error"
        job["error"] = err
        save_job(job)
        log(f"{job['id']} generate failed: {err[:300]}")
        return False

    # generate_script already wrote script.txt + cover.json. finalize
    # reads them back, validates length, computes duration, and writes
    # the full job record (status=ready_script|rendered, script, script_meta).
    fresh = load_job(job_path(job["id"]))
    if not finalize_from_script_file(fresh):
        # Length miss. Try targeted repair before hard-failing.
        is_preview = bool((fresh.get("render") or {}).get("preview_only", False))
        if is_preview:
            min_chars = 50
            max_chars = max(
                MAX_SCRIPT_CHARS,
                int((fresh.get("render") or {}).get("duration_sec", 10)
                    * ESTIMATED_CHARS_PER_SECOND * 1.3) + 100,
            )
        else:
            target_seconds = int((fresh.get("render") or {}).get("duration_sec") or DEFAULT_TARGET_SECONDS)
            min_chars, max_chars = script_length_bounds(target_seconds)
        cur_len = len((fresh.get("script") or "").strip() or script)
        log(
            f"{job['id']} length miss {cur_len} "
            f"(need {min_chars}-{max_chars}, preview={is_preview}), attempting repair"
        )
        if repair_script_length(fresh, min_chars, max_chars):
            fresh = load_job(job_path(job["id"]))
            log(
                f"{job['id']} ready_script ({len(fresh['script'])} chars "
                f"after repair, preview={is_preview})"
            )
            return True
        fresh = load_job(job_path(job["id"]))
        final_len = len(fresh.get("script") or "")
        fresh["status"] = "error"
        fresh["error"] = f"script length {final_len} outside {min_chars}-{max_chars} chars (after repair)"
        save_job(fresh)
        log(f"{job['id']} failed length check after repair: {final_len} (preview={is_preview})")
        return False

    save_job(fresh)
    is_preview = bool((fresh.get("render") or {}).get("preview_only", False))
    log(
        f"{job['id']} {fresh['status']} ({len(fresh['script'])} chars, "
        f"preview={is_preview})"
    )
    return True


def main():
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    with LOCK_PATH.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another writer is running, skipping")
            return 0

        # Debounce
        if SCRIPT_TRIGGER.exists():
            deadline = time.time() + 12
            while time.time() < deadline:
                mtime = SCRIPT_TRIGGER.stat().st_mtime
                age = time.time() - mtime
                if age >= 3:
                    break
                time.sleep(min(3, max(0.2, 3 - age)))

        # Throttle
        if LAST_RUN_MARKER.exists():
            try:
                last = float(LAST_RUN_MARKER.read_text(encoding="utf-8").strip() or "0")
            except ValueError:
                last = 0
            gap = time.time() - last
            if gap < 15 and last:
                wait = 15 - gap
                log(f"throttling: previous run {gap:.1f}s ago, sleeping {wait:.1f}s")
                time.sleep(wait)

        # Long-running drain + poll: process pending jobs serially through
        # the 7h window (cron at 1am triggers daemon, expected to stay up
        # until 8am). Inter-job cooldown protects the LLM agent host from
        # back-to-back hits; idle poll picks up jobs web submits mid-run.
        # Exit early only when we're near end-of-window AND idle.
        STARTED = time.time()
        WINDOW_SECONDS = 7 * 3600          # 7h, matches cron trigger
        EARLY_EXIT_GRACE = 600             # last 10min + idle → exit
        INTER_JOB_COOLDOWN = 5             # seconds between consecutive jobs
        IDLE_POLL_INTERVAL = 30            # poll cadence when no pending

        processed = 0
        while True:
            if time.time() - STARTED >= WINDOW_SECONDS:
                log("window elapsed (7h), exiting")
                break

            jobs = pending_jobs()
            if jobs:
                process_one(jobs[0])
                processed += 1
                _scan_and_touch_triggers()
                time.sleep(INTER_JOB_COOLDOWN)
                continue

            remaining = WINDOW_SECONDS - (time.time() - STARTED)
            if remaining < EARLY_EXIT_GRACE:
                log("near end of window + idle, exiting cleanly")
                break
            _scan_and_touch_triggers()
            time.sleep(IDLE_POLL_INTERVAL)

        LAST_RUN_MARKER.write_text(f"{time.time()}\n", encoding="utf-8")
        log(f"processed={processed} (drained over {(time.time()-STARTED)/60:.1f}min)")

        _scan_and_touch_triggers()
    return 0


def _scan_and_touch_triggers():
    # Cascade: scan ALL job files (not just the last batch's `jobs`) so
    # we don't miss earlier jobs that became ready_script during the
    # drain loop. Touch director trigger if any hit ready_script, or
    # narrate trigger for preview_only jobs that finished rendering.
    # Called after every process_one and on every idle poll, NOT only at
    # main-loop exit — otherwise ready_script jobs sit idle for the
    # remaining 6h of the cron window with no director daemon running.
    # 4-stage pipeline: ready_script → director → ready_shotlist → render.
    # The render trigger is now owned by the director daemon's own cascade.
    touched_director = False
    touched_narrate = False
    if not JOBS_DIR.exists():
        return
    for jp in JOBS_DIR.glob("v_*.json"):
        try:
            cur = load_job(jp)
        except (OSError, json.JSONDecodeError):
            continue
        if cur.get("mode") != "video":
            continue
        j_id = cur.get("id", jp.stem)
        st = cur.get("status")
        is_preview = bool((cur.get("render") or {}).get("preview_only", False))
        log(f"  cascade: {j_id} status={st!r} preview={is_preview}")
        if st == "rendered" and is_preview:
            NARRATE_TRIGGER.touch()
            touched_narrate = True
        elif st == "ready_script":
            DIRECTOR_TRIGGER.touch()
            touched_director = True
    if touched_director:
        log(f"touched {DIRECTOR_TRIGGER.name}")
    if touched_narrate:
        log(f"touched {NARRATE_TRIGGER.name} (preview_only)")


if __name__ == "__main__":
    raise SystemExit(main())
