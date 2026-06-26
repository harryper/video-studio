#!/usr/bin/env python3
"""Host-side writer for video-studio script jobs (mode='video').

Mirrors the structure of process_pending_voice_jobs.py:
- Triggered by .video-script-trigger (systemd path unit)
- Dispatches an `openclaw agent` sub-session to write the narration script
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
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = SKILL_DIR.parents[1]
OPENCLAW_ROOT = WORKSPACE_DIR.parent
JOBS_DIR = SKILL_DIR / "jobs" / "video"
RUNS_DIR = Path("/root/.openclaw/workspace/skills/video-studio/runs")
LOCK_PATH = SKILL_DIR / ".video-script-writer.lock"
NODE = Path("/usr/bin/node")
OPENCLAW = Path("/usr/lib/node_modules/openclaw/openclaw.mjs")
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
RENDER_TRIGGER = SKILL_DIR / ".video-render-trigger"
NARRATE_TRIGGER = SKILL_DIR / ".video-narrate-trigger"
LAST_RUN_MARKER = SKILL_DIR / ".video-script-writer.lastrun"
REFERENCE_STYLE = Path("/root/.openclaw/workspace/skills/video-studio/reference-style-video.md")
LOG_FILE = Path("/var/log/video-studio/video-script-watcher.log")


# ----- 对标 [行者道荣] 风格 prompt 内联 (来自 benchmarks/xingzhe/analysis.md §3+§6) -----
# 20 篇真实稿子反推: 反问开场 70% + 段子化金句 + 12.9 数字/视频 + 数学对比 + 80% IP 签名
GOOD_EXAMPLES = '''[好脚本示例 — 学结构, 不要死抄文字]

示例 1 (反问 + 立即给答案, 核弹泰山 #1):
"核弹能不能抹平一座山, 比如泰山。直接说答案, 不能。咱们往往会过于高估核弹的威力。小时候经常听说把地球上所有的核弹一起引爆就能毁灭地球好几次, 这个谣言属于是夏侯惇鉴宝——一眼假, 泰山听了都能把体内的岩浆笑尿出来。"
→ 反问开场 + 立即给答案 + 段子化破折号金句

示例 2 (假设 + 立即给答案, 琥珀防腐 #2):
"既然琥珀里的昆虫万年不腐, 古人为什么不用琥珀来给尸体做防腐? 直接说答案, 因为琥珀它根本就不防腐, 里面的昆虫看上去栩栩如生, 而实际上内脏早就烂没了。"
→ 假设开场 + 立即给答案 + 反常识判断

示例 3 (反问 + 反问, 狼群牧羊犬 #6):
"为什么狼群不敢攻击有牧羊犬保护的羊群? 难道狼群还打不过一条狗吗? 唉, 当然是因为牧羊犬有个终极技能叫做召唤恐怖直立猿, 单个恐怖直立猿, 狼群不怕的话, 就会刷新出持械恐怖直立猿。"
→ 双反问开场 + 段子化比喻 (游戏机制)

示例 4 (假设 + 反常识, 熬夜 #20):
"如果每天都熬夜到 12 点之后再睡觉会怎样? 熬夜的后果想必大家都知道, 每熬夜六十秒就会少睡一分钟, 然而咱们打工人白天的时间并不属于自己, 到了晚上才有一点自己的时间。"
→ 假设开场 + 数字反差 (60秒少1分钟) + 站位打工人
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

HOOK_TEMPLATES = '''[开头 — 70% 反问开场, 选 1 个]

1. 反问 + 立即给答案: "X 能不能 Y? 直接说答案, 因为 Z。"  (王者, 14/20)
2. 反问 + 反问: "为什么 X? 难道 Y 吗?"
3. 假设 + 立即给答案: "如果 X 会怎样? 直接说答案, 因为 Y。"
4. 反常识: "你以为 X, 其实 Y。" (用得少, 易俗)
5. 假设: "如果 X 会怎样? 攻读过 X 的兄弟们都知道, Y。"  (跟"咱们"叙述者配套)

[中段钩子 — 每 10-15 秒一个]

- 具体数字: "9.2 公里 / 50 公里 / 200 公里 / 14 万吨 / 5 千万吨"
- 段子化破折号: "X——Y" (X反差 + Y降维) / "X, 属于是 Y"
- 数学对比: "A 倍 / 提升 X 倍 / 差 30 倍 / 约等于 X"
- 跨学科引用: 把科学/历史/游戏术语编织进同一段
- 短句节奏: 单句 ≤20 字, 标点密集 (不超 22 字, 跟参考 style 一致)

[结尾 — 80% IP 签名, 但本号可去掉]

- 段子化金句再砸一次核心论点
- (可选) "评论区 X" 唤起记忆/晒/站队
- (可选) IP 签名 (本号暂不立 IP, 跳过)
- ❌ 绝对不要: "以上就是..." / "希望对你有帮助" / "最后希望大家..." (治愈系)
'''

ANTI_PATTERNS = '''[必须避免 — 这些会让脚本"软"]

❌ 抒情陈述开头: "凌晨两点, 灯还亮着" / "你有多久没..."  (v_40376759 反例)
❌ 自我重复句式: "熬的不是夜, 是 X" / "你 X 的不是 Y, 是 Z" — 用了 2 次 = 没创意
❌ 公众号爆款词堆砌: "KPI / 甲方 / 群消息 / 妈的视频号 / 房东的租约" — 5 个名词并排念是朗读
❌ 烂大街金句: "早睡是奢侈品" / "成年人的世界没有容易二字" / "成年人的崩溃"
❌ 治愈系结尾: "最后希望大家都能如愿..." / "希望对你有帮助"  (#20 反例)
❌ 过度诗意: "把 X 揉进 Y 里" / "在 Z 里寻找 W" — 抒情挤压具体
❌ 开放式问号结尾: "你怎么看?" / "你有什么感悟?" — 不可秒回
❌ "治愈/安顿/松弛/温柔" 系 (被风格指南禁, 也会被读者反感)
❌ 自我感动式反转: "原来 X 一直没变, 变的是 Y" — 鸡汤
❌ 第一人称过多: "我" 连续出现 3+ 次 — 用"咱们/你"代
❌ "今天我们来聊聊" / "大家好我是 X" — 自我介绍式开头
❌ "首先/其次/最后/综上所述" — 论文结构词
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


def build_prompt(job):
    job_id = job["id"]
    theme = job.get("theme") or ""
    ref_path = REFERENCE_STYLE
    ref_relpath = str(ref_path.relative_to(WORKSPACE_DIR)) if ref_path.exists() else "(reference-style-video.md missing)"
    target_seconds = int(job.get("render", {}).get("duration_sec") or DEFAULT_TARGET_SECONDS)
    min_chars, max_chars = script_length_bounds(target_seconds)
    target_chars = int(target_seconds * ESTIMATED_CHARS_PER_SECOND)
    return (
        f"为 video-studio Web 项目写一段约 {target_seconds} 秒 ({target_chars} 字) 的短视频旁白稿。\n"
        f"主题：{theme}\n\n"
        f"## 参考风格\n"
        f"先读 {ref_relpath} (如果存在, 作为扩展参考)。\n"
        f"本项目内已基于 20 篇对标稿子反推出具体规则, 见下文。\n\n"

        f"## 好脚本示例 (4 条锚点 — 学结构, 不要死抄)\n"
        f"{GOOD_EXAMPLES}\n\n"

        f"## 钩子模板 (开头 + 中段 + 结尾, 选 1-2 个组合, 不要堆砌)\n"
        f"{HOOK_TEMPLATES}\n\n"

        f"## 必须避免 (会让脚本软)\n"
        f"{ANTI_PATTERNS}\n\n"

        f"{COVER_INSTRUCTIONS}\n"

        f"## 硬约束 (优先级最高)\n"
        f"1. 字数硬上限 {max_chars} 字, 目标 {target_chars} 字. **超过 1500 字直接判失败**, 不要尝试写 3000+ 字长稿\n"
        f"2. 纯文本输出, 不要 markdown / 编号 / 标题 / 空行分隔\n"
        f"3. 开头 60-90 字内必须出现: 反问 + 立即给答案 / 假设 + 给答案 / 反常识判断 (三选一)\n"
        f"4. 中段每 10-15 秒一个钩子: 具体数字 / 段子化破折号 / 数学对比 / 跨学科引用 (四选一)\n"
        f"5. 数字密度: >= {int(target_chars/100)} 个数字 (含中文) 在全文, 数学对比 >=2 个 (A 倍 / 约等于)\n"
        f"6. 反转密度: 但是/其实/真相是/实际上 类词 >=3 个\n"
        f"7. 结尾禁止: 开放式问号 / 治愈系 / 以上就是... / 希望对你有帮助\n"
        f"8. 写完自检: 5 个连续名词并排? 同一句式用了 2 次? 有治愈/松弛/温柔吗? 字数是否在 {min_chars}-{max_chars} 区间? 任何一项不通过就重写\n"
        f"9. 不要尝试用 N 段完整 4 层结构堆长度, 一段层只算一个反转, 4 层反转 + 中间段子 = 600-800 字就够\n\n"

        f"## 执行\n"
        f"1. 写入 skills/video-studio/runs/{job_id}/script.txt\n"
        f"2. 更新 jobs/video/{job_id}.json: status=\"ready_script\", script=<全文>, "
        f"script_meta={{char_count, target_seconds, actual_seconds=null}}, error=null\n"
        f"3. job_id={job_id}\n\n"

        f"## 纪律\n"
        f"- 首次写入即终稿, 不要反复自我检查 / 改写 / 重写\n"
        f"- 不要把全文写在 thinking 或最终回复里, 必须用文件写入工具落盘\n"
        f"- 文稿字数应符合动态区间: {min_chars}-{max_chars} 字 (基于 {target_seconds}s 目标时长)\n"
        f"- 不要生成音频, 不要发布, 不要给用户发消息\n"
        f"- 最终回复只允许一句话: '已写入 <路径>'"
    )


def build_repair_prompt(job, current_script, min_chars, max_chars):
    """Targeted length-repair prompt: feed the existing script back to the
    agent with a directional nudge (too short → expand, too long → trim),
    instead of discarding the whole attempt and re-rolling from scratch.
    """
    job_id = job["id"]
    target_seconds = int(job.get("render", {}).get("duration_sec") or DEFAULT_TARGET_SECONDS)
    target_chars = int(target_seconds * ESTIMATED_CHARS_PER_SECOND)
    cur_len = len(current_script)
    if cur_len < min_chars:
        gap = min_chars - cur_len
        direction = (
            f"当前 {cur_len} 字, 比下限少 {gap} 字. 在保留开头钩子 / 中段钩子 / 结尾风格的前提下, "
            f"补充具体数字 / 段子化金句 / 跨学科细节来扩写, 不要堆砌空洞名词, 不要改写已有好句子. "
        )
    else:
        gap = cur_len - max_chars
        direction = (
            f"当前 {cur_len} 字, 比上限多 {gap} 字. 删减冗余 / 重复 / 空洞处, 保留所有钩子和数字密度, "
            f"不要改写已有好句子. "
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
        f"## 执行\n"
        f"1. 覆盖写入 skills/video-studio/runs/{job_id}/script.txt (整篇终稿, 含已改部分)\n"
        f"2. 最终回复只允许一句话: '已修复 <路径>'\n"
        f"3. 不要生成音频, 不要发布, 不要给用户发消息\n"
    )


def run_agent(job):
    prompt = build_prompt(job)
    attempt = int(job.get("writer_attempt") or 0) + 1
    job["writer_attempt"] = attempt
    save_job(job)
    cmd = [
        str(NODE),
        str(OPENCLAW),
        "agent",
        "--agent",
        "main",
        "--session-key",
        f"agent:main:video-studio-writer-{job['id']}-a{attempt}",
        "--message",
        prompt,
        "--thinking",
        "off",
        "--json",
        "--timeout",
        "300",  # 5 min — much shorter than voice writer's 900s
    ]
    return subprocess.run(
        cmd,
        cwd=str(WORKSPACE_DIR),
        text=True,
        capture_output=True,
        timeout=360,
    )


def _session_jsonl_path(job_id):
    """Same lookup pattern as voice writer."""
    sessions_index = OPENCLAW_ROOT / "agents" / "main" / "sessions" / "sessions.json"
    if not sessions_index.exists():
        return None
    try:
        with sessions_index.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    needle = f"agent:main:video-studio-writer-{job_id}"
    info = data.get(needle) or {}
    session_file = info.get("sessionFile")
    if not session_file:
        return None
    return Path(session_file)


def scrape_session_error(job_id, result):
    """Same pattern as voice writer — pull real error from session jsonl."""
    fallback = ((result.stderr or result.stdout or "").strip() or "unknown error")[:800]
    fallback_msg = f"openclaw agent failed on host: {fallback}"

    session_file = _session_jsonl_path(job_id)
    if not session_file or not session_file.exists():
        return fallback_msg

    last_assistant = None
    last_texts = []
    tool_call_count = 0
    had_tool_error = False
    try:
        with session_file.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("message") or {}
                if msg.get("role") == "assistant":
                    last_assistant = msg
                    last_texts = [
                        c.get("text", "") for c in msg.get("content", [])
                        if c.get("type") == "text" and c.get("text")
                    ]
                    for c in msg.get("content", []):
                        if c.get("type") == "toolCall":
                            tool_call_count += 1
                if msg.get("role") == "toolResult" and msg.get("isError"):
                    had_tool_error = True
    except OSError:
        return fallback_msg

    if not last_assistant:
        return fallback_msg

    err_msg = last_assistant.get("errorMessage")
    if err_msg:
        return f"openclaw agent failed: {err_msg}"

    stop_reason = last_assistant.get("stopReason")
    if stop_reason == "error" and not err_msg:
        return f"openclaw agent failed: stopReason=error (no errorMessage); rc={result.returncode}"

    last_text = " ".join(last_texts).strip()

    if stop_reason == "stop" and tool_call_count == 0 and last_text:
        preview = last_text[:160].replace("\n", " ")
        return (
            "openclaw agent returned without any tool call but reported done "
            f"(model hallucination, last text: \"{preview}\")"
        )

    if not last_text and not had_tool_error:
        return (
            f"openclaw agent returned no assistant text and no tool calls; "
            f"rc={result.returncode}; stderr={fallback[:200]}"
        )

    return fallback_msg


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
    """Targeted length-repair loop.

    When the initial writer produces a script outside [min_chars, max_chars],
    feed the existing on-disk script back to the agent with a directional
    nudge instead of erroring out. Reuses writer_attempt as a real retry cap
    (1 initial + up to MAX_WRITER_ATTEMPTS-1 repairs). Returns True if
    finalize_from_script_file succeeds (job finalized on disk), False on
    exhaustion — caller then records the hard error.
    """
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
        prompt = build_repair_prompt(job, current_script, min_chars, max_chars)
        cmd = [
            str(NODE), str(OPENCLAW), "agent",
            "--agent", "main",
            "--session-key", f"agent:main:video-studio-writer-{job['id']}-a{attempt}",
            "--message", prompt,
            "--thinking", "off",
            "--json",
            "--timeout", "300",
        ]
        try:
            subprocess.run(
                cmd, cwd=str(WORKSPACE_DIR), text=True,
                capture_output=True, timeout=360,
            )
        except subprocess.TimeoutExpired:
            log(f"{job['id']} repair attempt {attempt} timed out")
        # Trust the on-disk artefact: finalize re-validates length and writes
        # status/script_meta/cover. Returns False if still out of range.
        fresh = load_job(job_path(job["id"]))
        if finalize_from_script_file(fresh):
            save_job(fresh)
            log(f"{job['id']} repaired to {len(fresh['script'])} chars (attempt {attempt})")
            return True
        # Still out of range — read the latest script.txt for the next pass,
        # since finalize bails before copying script into the job dict.
        sp = RUNS_DIR / job["id"] / "script.txt"
        if sp.exists():
            latest = sp.read_text(encoding="utf-8").strip()
            if latest:
                current_script = latest
        job = fresh
    return False


def process_one(job):
    job["status"] = "writing"
    job["error"] = None
    save_job(job)

    try:
        result = run_agent(job)
    except subprocess.TimeoutExpired:
        current = load_job(job_path(job["id"]))
        if finalize_from_script_file(current):
            log(f"{job['id']} ready_script from script file after agent timeout")
            return True
        current["status"] = "error"
        current["error"] = "openclaw agent timed out after 360s"
        save_job(current)
        log(f"{job['id']} timed out")
        return False

    try:
        updated = load_job(job_path(job["id"]))
    except (OSError, json.JSONDecodeError) as exc:
        updated = dict(job)
        log(f"{job['id']} job json unreadable after agent run: {exc}")
        if result.returncode == 0 and finalize_from_script_file(updated):
            log(f"{job['id']} ready from script file after json repair")
            return True
        updated["status"] = "error"
        updated["error"] = f"job json unreadable after agent run: {exc}"
        save_job(updated)
        return False

    if updated.get("status") == "ready_script" and (updated.get("script") or "").strip():
        # preview_only: skip the duration-aware minimum (10s demo scripts are short)
        is_preview = bool((updated.get("render") or {}).get("preview_only", False))
        if is_preview:
            min_chars = 50
            max_chars = max(MAX_SCRIPT_CHARS, int((updated.get("render") or {}).get("duration_sec", 10) * ESTIMATED_CHARS_PER_SECOND * 1.3) + 100)
        else:
            target_seconds = int((updated.get("render") or {}).get("duration_sec") or DEFAULT_TARGET_SECONDS)
            min_chars, max_chars = script_length_bounds(target_seconds)
        if not min_chars <= len(updated["script"]) <= max_chars:
            # Length miss → targeted repair pass instead of a hard error.
            # The agent already wrote a usable script; nudge it back into
            # range (expand/trim) rather than discarding the whole attempt.
            log(
                f"{job['id']} length miss {len(updated['script'])} "
                f"(need {min_chars}-{max_chars}, preview={is_preview}), attempting repair"
            )
            if repair_script_length(updated, min_chars, max_chars):
                updated = load_job(job_path(job["id"]))
                log(
                    f"{job['id']} ready_script ({len(updated['script'])} chars "
                    f"after repair, preview={is_preview})"
                )
                return True
            updated = load_job(job_path(job["id"]))
            final_len = len(updated.get("script") or "")
            updated["status"] = "error"
            updated["error"] = (
                f"script length {final_len} outside "
                f"{min_chars}-{max_chars} chars (after repair)"
            )
            save_job(updated)
            log(f"{job['id']} failed length check after repair: {final_len} (preview={is_preview})")
            return False
        # 封面: agent 直接写 status=ready_script 时也走一遍解析, 缺的字段填 None
        existing_meta = updated.get("script_meta") or {}
        if not isinstance(existing_meta, dict) or "cover" not in existing_meta:
            existing_meta = {
                **existing_meta,
                "cover": parse_cover_from_agent_result(job["id"]),
            }
            updated["script_meta"] = existing_meta
            save_job(updated)
        # preview_only: downgrade from ready_script -> rendered so the
        # cascade below does not touch the render trigger (which would
        # kick off the full image-fetch pipeline). Narrate daemon picks
        # up status=rendered jobs and runs preview_caption_ffmpeg.
        if is_preview:
            updated["status"] = "rendered"
            save_job(updated)
            log(f"{job['id']} rendered (preview_only, {len(updated['script'])} chars)")
            return True
        log(f"{job['id']} ready_script ({len(updated['script'])} chars)")
        return True

    if result.returncode == 0 and finalize_from_script_file(updated):
        log(f"{job['id']} ready_script from script file")
        return True

    # Belt-and-suspenders: even when the agent sub-process exits non-zero,
    # trust the on-disk artefact. If the agent wrote a valid script.txt we
    # treat it as success — the sub-process returncode can be misleading
    # when the agent completes via a final write-tool call.
    if finalize_from_script_file(updated):
        log(f"{job['id']} ready_script from script file (rc={result.returncode})")
        return True

    real_err = scrape_session_error(job["id"], result)
    updated["status"] = "error"
    updated["error"] = real_err
    save_job(updated)
    tail = real_err.replace("\n", " ")[:300]
    log(f"{job['id']} failed: {tail}")
    return False


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
    # drain loop. Touch render trigger if any hit ready_script, or
    # narrate trigger for preview_only jobs that finished rendering.
    # Called after every process_one and on every idle poll, NOT only at
    # main-loop exit — otherwise ready_script jobs sit idle for the
    # remaining 6h of the cron window with no render daemon running.
    touched_render = False
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
            RENDER_TRIGGER.touch()
            touched_render = True
    if touched_render:
        log(f"touched {RENDER_TRIGGER.name}")
    if touched_narrate:
        log(f"touched {NARRATE_TRIGGER.name} (preview_only)")


if __name__ == "__main__":
    raise SystemExit(main())
