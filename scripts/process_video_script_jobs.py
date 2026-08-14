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
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm_client


SKILL_DIR = Path(__file__).resolve().parents[1]
JOBS_DIR = SKILL_DIR / "jobs" / "video"
RUNS_DIR = SKILL_DIR / "runs"
LOCK_PATH = SKILL_DIR / ".video-script-writer.lock"
# Char-count tolerance band. LLM output is noisy — widened to 300-1200 to
# support both short-form (300+ chars e.g. 抖音小知识/科普短文案) and
# long-form (200s 抖音科普大约 1080 字, 上限 1200 留余量).
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
LOG_FILE = Path("/var/log/video-studio/video-script-watcher.log")


# ----- 脚本正文创作风格已移除 (待定义新风格) -----
# 旧的段子风格 (MEME_GUIDE) + 风格/段子参考文件注入已从 build_prompt 中撤下,
# 相关语料/参考文件也已从仓库删除。当前脚本 prompt 只保留中立骨架 (主题 +
# 长度 + 纯文本 + JSON 输出)。封面 (COVER_INSTRUCTIONS) 及其校验逻辑保留不变。

# ----- Editorial request 校验 -----

VALID_TONE = ("auto", "故事感", "冷峻", "幽默克制", "观点解释")
VALID_GOAL = ("balanced", "completion", "share", "comment")
VALID_FACT_STRICTNESS = ("standard", "high")
REQUEST_FIELD_LIMITS = {"audience": 80, "tone": 40, "angle": 80}


def validate_editorial_request(data: dict) -> tuple[Optional[dict], str]:
    """校验并规范化 editorial request. 返回 (request_dict, None) 或 (None, err_msg).

    字段缺失或为空字符串 → 用默认值填充. 非法 enum / 超长 / 非字符串 → 拒绝.
    """
    if not isinstance(data, dict):
        return None, "editorial request 必须是 dict"
    out = {
        "audience": "普通用户",
        "tone": "auto",
        "angle": "auto",
        "goal": "balanced",
        "fact_strictness": "standard",
    }
    for k, limit in REQUEST_FIELD_LIMITS.items():
        v = data.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        if not isinstance(v, str):
            return None, f"{k} 必须是字符串"
        if len(v) > limit:
            return None, f"{k} 长度 {len(v)} 超过上限 {limit}"
        out[k] = v.strip()
    for k, choices in (("tone", VALID_TONE), ("goal", VALID_GOAL),
                       ("fact_strictness", VALID_FACT_STRICTNESS)):
        v = data.get(k)
        if v is None:
            continue
        if v not in choices:
            return None, f"{k} 必须是 {', '.join(choices)} 之一; 收到 {v!r}"
        out[k] = v
    return out, None


# ----- Story Brief -----

VALID_CONTENT_LANES = (
    "historical_power", "science_explainer", "business_economy",
    "people_story", "myth_busting",
)

STORY_BRIEF_SCHEMA_PROMPT = '''## 输出格式（严格遵守）
只输出一个 JSON 对象, 不要 markdown fence, 不要任何说明:
{
  "content_lane": "<historical_power | science_explainer | business_economy | people_story | myth_busting>",
  "candidate_angles": [
    {"angle": "<切入角度, 不超过 30 字>", "core_thesis": "<单句核心命题, 不超过 40 字>",
     "why_it_spreads": "<为什么这个角度容易传播, 一句话>"},
    // 共 3 条, 三条 angle 文本不能重复, core_thesis 不能空泛 (必须可证伪)
  ],
  "chosen_angle": "<最终选定的角度, 不超过 30 字>",
  "core_thesis": "<单句核心命题, 不超过 40 字, 必须可证伪>",
  "audience_misconception": "<目标受众对这个主题最常见的误解, 一句话>",
  "opening_scene": "<开场前 2 句必须呈现的具体物件/动作/数字, 一句话>",
  "evidence_chain": [
    "<因果链第 1 步, 必须包含 '因为/所以/因此/通过/从而' 等因果连接, ≥ 8 字>",
    "<因果链第 2 步, 同上>",
    "<因果链第 3 步, 同上>"
  ],  // 3-5 条, 因果顺序, 禁止纯名词列表
  "twist": "<认知翻转点, 一句话, 不写完整判断句>",
  "visual_anchors": ["<可拍摄或可绘制的具体物件>", "..."],  // 至少 2 个
  "risk_claims": [
    {"claim": "<具体断言, 一句话>", "risk": "<high | medium | low>",
     "instruction": "<写作时如何处理 (禁用 / 弱化 / 加范围 / 加来源占位)>"}
  ]  // 至少 1 条 high 风险的禁区断言
}
'''

STORY_BRIEF_SYSTEM = (
    "You are a 抖音 short-video story editor preparing a STORY BRIEF before "
    "any narration is written. Output a single JSON object and nothing else."
)


def build_story_brief_prompt(job, request):
    """Story brief prompt: 主题 + 用户 editorial request + brief schema."""
    theme = job.get("theme") or ""
    audience = request.get("audience", "普通用户")
    tone = request.get("tone", "auto")
    angle = request.get("angle", "auto")
    goal = request.get("goal", "balanced")
    fact = request.get("fact_strictness", "standard")
    angle_note = (
        f"用户指定的切入角度: {angle}. 必须把 chosen_angle 设为这个角度, "
        f"但仍生成 3 个 candidate_angles 体现对比."
        if angle != "auto"
        else "angle 是 auto. 由你从 candidate_angles 中选一个最合适的作为 chosen_angle."
    )
    return (
        f"为 video-studio 短视频《{theme}》生成 STORY BRIEF (写稿前的选题策划).\n\n"
        f"## 用户创作设置\n"
        f"- 受众: {audience}\n"
        f"- 写法: {tone}\n"
        f"- 切入角度: {angle}\n"
        f"- 传播目标: {goal} (completion=完播 / share=转发 / comment=讨论 / balanced=均衡)\n"
        f"- 事实严格度: {fact} (high=减少无法验证数字和绝对化表述)\n\n"
        f"## 关键约束\n"
        f"1. {angle_note}\n"
        f"2. core_thesis 必须是单一可证伪命题, 禁止空泛 (如 'X 很重要' 'Y 影响深远').\n"
        f"3. evidence_chain 必须有 3-5 条, 每条都包含因果连接词, 禁止纯名词列表.\n"
        f"4. risk_claims 至少 1 条 high 风险, 列出写作时需禁用或弱化的断言.\n"
        f"5. visual_anchors 至少 2 个, 必须是可拍摄或可绘制动作的具体物件.\n"
        f"6. candidate_angles 三条 angle 文本不能重复.\n\n"
        f"{STORY_BRIEF_SCHEMA_PROMPT}"
    )


def _validate_story_brief_dict(brief):
    """Apply spec §4 validation rules to a parsed brief dict. Returns (cleaned, None) or (None, err)."""
    if not isinstance(brief, dict):
        return None, "brief 不是 dict"
    lane = brief.get("content_lane")
    if lane not in VALID_CONTENT_LANES:
        return None, f"content_lane 必须是 {', '.join(VALID_CONTENT_LANES)}; 收到 {lane!r}"
    angles = brief.get("candidate_angles")
    if not isinstance(angles, list) or len(angles) != 3:
        return None, "candidate_angles 必须正好 3 条"
    texts = []
    for i, a in enumerate(angles):
        if not isinstance(a, dict):
            return None, f"candidate_angles[{i}] 不是 dict"
        ang = a.get("angle")
        thesis = a.get("core_thesis")
        why = a.get("why_it_spreads")
        if not isinstance(ang, str) or not ang.strip():
            return None, f"candidate_angles[{i}].angle 缺失"
        if not isinstance(thesis, str) or not thesis.strip():
            return None, f"candidate_angles[{i}].core_thesis 缺失"
        if not isinstance(why, str) or not why.strip():
            return None, f"candidate_angles[{i}].why_it_spreads 缺失"
        texts.append(ang.strip())
    if len(set(texts)) != 3:
        return None, "candidate_angles 三条 angle 文本不能重复"
    for field in ("chosen_angle", "core_thesis", "audience_misconception",
                  "opening_scene", "twist"):
        v = brief.get(field)
        if not isinstance(v, str) or not v.strip():
            return None, f"{field} 缺失"
    chain = brief.get("evidence_chain")
    if not isinstance(chain, list) or not (3 <= len(chain) <= 5):
        return None, "evidence_chain 必须 3-5 条"
    for i, e in enumerate(chain):
        if not isinstance(e, str) or len(e.strip()) < 8:
            return None, f"evidence_chain[{i}] 长度 {len(e) if isinstance(e, str) else 0} < 8 (防纯名词列表)"
    anchors = brief.get("visual_anchors")
    if not isinstance(anchors, list) or len(anchors) < 2:
        return None, "visual_anchors 必须 ≥ 2 个"
    risks = brief.get("risk_claims")
    if not isinstance(risks, list) or len(risks) < 1:
        return None, "risk_claims 必须 ≥ 1 条"
    for i, r in enumerate(risks):
        if not isinstance(r, dict):
            return None, f"risk_claims[{i}] 不是 dict"
        if r.get("risk") not in ("high", "medium", "low"):
            return None, f"risk_claims[{i}].risk 非法"
        if not isinstance(r.get("claim"), str) or not r["claim"].strip():
            return None, f"risk_claims[{i}].claim 缺失"
        if not isinstance(r.get("instruction"), str) or not r["instruction"].strip():
            return None, f"risk_claims[{i}].instruction 缺失"
    return brief, None


def parse_story_brief(text):
    """从 LLM 文本提取并校验 brief. (brief, None) 或 (None, err_msg)."""
    if not text:
        return None, "empty LLM response"
    for cand in _iter_json_objects(text):
        if not isinstance(cand, dict):
            continue
        cleaned, err = _validate_story_brief_dict(cand)
        if cleaned is not None:
            return cleaned, None
    return None, "could not parse story_brief (LLM response not valid JSON or missing required fields)"


def generate_story_brief(job, request):
    """LLM call: 返回 (brief_dict, None) 或 (None, err_msg)."""
    prompt = build_story_brief_prompt(job, request)
    try:
        text = llm_client.complete(
            system=STORY_BRIEF_SYSTEM,
            user=prompt,
            max_tokens=2048,
            timeout=180.0,
        )
    except Exception as e:
        return None, f"LLM call failed: {e}"

    brief, err = parse_story_brief(text)
    if brief is None:
        return None, err

    # 用户指定 angle 时, 强制 chosen_angle 等于用户输入 (即使 LLM 给了别的)
    if request.get("angle", "auto") != "auto":
        brief["chosen_angle"] = request["angle"]
    return brief, None


# ----- Quality Report -----

QUALITY_DIMENSION_MAX = {
    "factual_safety": 20,
    "distinctive_angle": 20,
    "opening_hook": 20,
    "causal_clarity": 20,
    "spoken_rhythm": 10,
    "ending_payoff": 10,
}
QUALITY_VALID_VERDICTS = ("pass", "repair", "fatal")

QUALITY_REPORT_SCHEMA_PROMPT = '''## 输出格式（严格遵守）
只输出一个 JSON 对象, 不要 markdown fence, 不要任何说明:
{
  "score": <0-100 整数, = sum(dimensions)>,
  "dimensions": {
    "factual_safety": <0-20, 不夸张绝对化; 不违背 risk_claims; 因果强度与论据匹配>,
    "distinctive_angle": <0-20, 是否围绕单一角度; 是否具备该主题专属视角>,
    "opening_hook": <0-20, 前 3 句有具体场景、反差和未完结的认知缺口>,
    "causal_clarity": <0-20, 论证逐层推进, 数字和事实服务于因果>,
    "spoken_rhythm": <0-10, 自然口语、短长句交替、符合 clause 约束>,
    "ending_payoff": <0-10, 回扣开场且给出理解升级, 不是复读结论>
  },  // 总和必须等于 score (LLM 浮点漂移容差 ±1)
  "must_fix": [
    {"category": "<同上 6 维度之一>", "instruction": "<具体修改指令, 不写「优化得更好」>"}
  ],  // 可空数组; 但非空时每项必须可执行
  "strengths": ["..."],
  "verdict": "<pass | repair | fatal>"
}
'''

QUALITY_SYSTEM = (
    "You are a 抖音 short-video editorial reviewer scoring a script on 6 "
    "dimensions. You only output JSON — never edit the script yourself. "
    "If factual_safety is below 12 OR score is below 60, verdict MUST be 'fatal'."
)


def build_quality_prompt(job, brief, script):
    """评审 prompt: brief + script + schema."""
    theme = job.get("theme") or ""
    risks = brief.get("risk_claims", [])
    risk_lines = "\n".join(
        f"- [{r.get('risk')}] {r.get('claim')}: {r.get('instruction')}"
        for r in risks
    ) or "- (无)"
    return (
        f"你是 video-studio 短视频编辑, 评审旁白稿. 主题: {theme}.\n\n"
        f"## 核心命题 (story brief)\n{brief.get('core_thesis', '')}\n\n"
        f"## 写作禁区 (risk_claims, 必须检查正文是否违反)\n{risk_lines}\n\n"
        f"## 待评审旁白稿 (共 {len(script)} 字)\n{script}\n\n"
        f"## 评分要求\n"
        f"1. factual_safety < 12 或 score < 60 → verdict='fatal'.\n"
        f"2. must_fix 每项必须可执行 (e.g. '将「最早配给」改为「与培根同批进入配给」'), "
        f"禁止写「优化得更生动」类空指令.\n"
        f"3. 维度总和必须等于 score, 不可漂移超过 ±1.\n\n"
        f"{QUALITY_REPORT_SCHEMA_PROMPT}"
    )


def _validate_quality_report_dict(rep):
    if not isinstance(rep, dict):
        return None, "report 不是 dict"
    score = rep.get("score")
    if not isinstance(score, int) or not (0 <= score <= 100):
        return None, f"score 必须是 0-100 整数; 收到 {score!r}"
    dims = rep.get("dimensions")
    if not isinstance(dims, dict):
        return None, "dimensions 缺失"
    dim_sum = 0
    for k, mx in QUALITY_DIMENSION_MAX.items():
        v = dims.get(k)
        if not isinstance(v, int) or not (0 <= v <= mx):
            return None, f"dimensions.{k} 必须是 0-{mx} 整数; 收到 {v!r}"
        dim_sum += v
    for k in dims:
        if k not in QUALITY_DIMENSION_MAX:
            return None, f"dimensions 包含未知键 {k!r}"
    if abs(dim_sum - score) > 1:
        return None, f"dimensions 总和 {dim_sum} 与 score {score} 差距超过 ±1"
    verdict = rep.get("verdict")
    if verdict not in QUALITY_VALID_VERDICTS:
        return None, f"verdict 必须是 {QUALITY_VALID_VERDICTS}; 收到 {verdict!r}"
    must = rep.get("must_fix")
    if not isinstance(must, list):
        return None, "must_fix 必须是 list"
    for i, m in enumerate(must):
        if not isinstance(m, dict):
            return None, f"must_fix[{i}] 不是 dict"
        cat = m.get("category")
        instr = m.get("instruction")
        if cat not in QUALITY_DIMENSION_MAX:
            return None, f"must_fix[{i}].category 必须是 6 维度之一; 收到 {cat!r}"
        if not isinstance(instr, str) or not instr.strip():
            return None, f"must_fix[{i}].instruction 缺失"
    strengths = rep.get("strengths")
    if not isinstance(strengths, list):
        return None, "strengths 必须是 list"
    return {
        "score": score,
        "dimensions": {k: dims[k] for k in QUALITY_DIMENSION_MAX},
        "must_fix": must,
        "strengths": strengths,
        "verdict": verdict,
    }, None


def parse_quality_report(text):
    if not text:
        return None, "empty LLM response"
    for cand in _iter_json_objects(text):
        if not isinstance(cand, dict):
            continue
        cleaned, err = _validate_quality_report_dict(cand)
        if cleaned is not None:
            return cleaned, None
    return None, "could not parse quality_report (LLM response not valid JSON or missing required fields)"


def generate_quality_report(job, brief, script):
    prompt = build_quality_prompt(job, brief, script)
    try:
        text = llm_client.complete(
            system=QUALITY_SYSTEM,
            user=prompt,
            max_tokens=1024,
            timeout=120.0,
        )
    except Exception as e:
        return None, f"LLM call failed ({type(e).__name__})"
    return parse_quality_report(text)


# ----- Repair Pass -----

REPAIR_SYSTEM = (
    "You revise an existing 抖音 short-video script based on explicit must_fix "
    "instructions. Return a single JSON object: {\"script\": ..., \"cover\": ...}. "
    "Do not rewrite paragraphs not flagged for revision."
)


def build_editorial_repair_prompt(job, brief, report, current_script,
                                  min_chars, max_chars, length_gap_str):
    """Targeted revision prompt. 必须传 must_fix + core_thesis + 字数范围 + length gap."""
    theme = job.get("theme") or ""
    target_seconds = int((job.get("render") or {}).get("duration_sec") or DEFAULT_TARGET_SECONDS)
    must_lines = "\n".join(
        f"- [{m.get('category')}] {m.get('instruction')}"
        for m in (report.get("must_fix") or [])
    ) or "- (无)"
    strengths = report.get("strengths") or []
    strength_lines = "\n".join(f"- {s}" for s in strengths) or "- (无)"
    cur_len = len(current_script or "")
    return (
        f"修改已写好的 video-studio 旁白稿. 主题: {theme}. "
        f"目标 {target_seconds}s, 字数必须落在 {min_chars}-{max_chars} 区间.\n\n"
        f"## 核心命题 (不能改)\n{brief.get('core_thesis', '')}\n\n"
        f"## 开场物件 (不能丢)\n{brief.get('opening_scene', '')}\n\n"
        f"## 因果链 (顺序不能乱)\n"
        + "\n".join(f"{i+1}. {e}" for i, e in enumerate(brief.get("evidence_chain") or []))
        + "\n\n"
        f"## 评审 must_fix (按条修)\n{must_lines}\n\n"
        f"## 已有优点 (不要改写这些段落)\n{strength_lines}\n\n"
        f"## 字数缺口\n{length_gap_str}\n\n"
        f"## 当前全文 ({cur_len} 字)\n{current_script}\n\n"
        f"## 硬约束\n"
        f"1. 输出长度必须落在 {min_chars}-{max_chars} 字区间内.\n"
        f"2. 必须逐条响应 must_fix, 不能跳过任何 category.\n"
        f"3. 不能改写「已有优点」段落 (避免整体重写).\n"
        f"4. 不能写开放式问号 / 抒情结尾 / '希望对你有帮助'.\n"
        f"5. 高风险 risk_claims 仍按原 instruction 处理.\n"
        f"6. cover 字段如未改动可保持原值, 但必须重新校验 main_highlight.\n\n"
        f"## 输出格式\n"
        f'{{"script": "<修订稿>", "cover": {{"main": "...", "main_highlight": [s, e], "sub": "..."}}}}'
    )


def generate_repair_pass(job, brief, report, current_script,
                         min_chars, max_chars, length_gap_str):
    prompt = build_editorial_repair_prompt(
        job, brief, report, current_script, min_chars, max_chars, length_gap_str,
    )
    try:
        text = llm_client.complete(
            system=REPAIR_SYSTEM,
            user=prompt,
            max_tokens=4096,
            timeout=300.0,
        )
    except Exception as e:
        return None, None, f"LLM call failed ({type(e).__name__})"
    script, cover, err = _parse_script_response(text)
    if script is None:
        return None, None, err
    validated_cover = parse_cover_validation(cover) if cover else None
    return script, validated_cover, None


# ----- 科普赛道正文风格 (4 个块, 拼装顺序见 build_prompt) -----

NARRATIVE_SKELETON = '''## 叙事骨架 (5 阶段, 不锁字数, 阶段间不写显式标记)

按这个顺序组织, 把事讲清楚, 每个阶段都是讲的人脑子里想过的, 不是给观众看的标签:

1. 钩子: 开场第一句直接抛反常识 / 数字冲击 / 具体场景. 第一句必须是信息本身.
   严禁 "今天我们来聊聊..." / "你有没有想过..." 这类冷启动套话.
2. 认知缺口: 点破 "你以为的 vs 实际的", 制造悬念. 说出常识误区, 但不给答案.
3. 逐层揭秘: 用因果链把原理讲清楚, 一层扣一层. 每层只推进一步, 用具体数字 / 实物锚定, 不堆抽象名词.
4. 反转 / 意外: 一个 "原来如此" 的点, 或反直觉延伸 (尺度对比 / 历史巧合 / "所以其实..."). 尽量有, 但宁缺毋滥, 没有强反转就弱化, 不硬凑.
5. 落点: 一句话收束, 回扣钩子. 严禁开放式问号 / 抒情 / "以上就是..." / "希望对你有帮助".

完整示例 (主题: 海水为什么是咸的, 不要照抄, 看骨架怎么走):

  海水是咸的, 但你可能没想到, 罪魁祸首根本不是海.
  一公斤海水里, 溶了 35 克盐, 全球海洋加起来有 50 万亿吨.
  你以为盐一直就在海里, 其实不是. 海本来是淡水.
  盐是河冲进来的. 雨水落到山上, 渗进岩缝, 一点点溶解矿物质, 然后汇成河, 一路冲进海.
  海里的水蒸发, 盐留下, 越攒越多, 一攒就是几十亿年.
  所以下次喝到咸水, 别骂海, 骂山.
'''

VOICE_GUIDE = '''## 口吻: 聪明朋友 (像人在讲, 不像稿子在念)

- 有观点, 不只报事实: 讲完事实带反应. "这就有意思了" / "离谱的是" / "这才是关键".
- 偶尔用 "我" / "你" 拉近距离, 适度不滥用.
- 具体优先于抽象: 不说 "含有大量矿物质", 说 "每公斤海水里溶了 35 克盐".
- 口语连接: 用 "但" / "结果" / "所以说" / "关键是", 不用 "然而" / "此外" / "综上所述".
- 偶尔短句砸下来给节奏: "就这么简单." / "没了."
- 距离感: 不刻意惊叹, 不连续感叹号, 不网络烂梗 ("家人们" / "绝绝子" 类). 信息密度撑吸引力, 不靠喊. 不说教, 不用 "我们应该" / "这告诉我们" / "值得深思" 类升华.
- 口吻服务于把事讲清楚 + 讲得有意思, 不是加戏.
'''

ANTI_AI_RULES = '''## 去 AI 味 (Humanizer-zh 蒸馏, 短视频适用)

### A. AI 高频词黑名单 (一个都别用)
此外, 值得注意的是, 至关重要, 深入探讨, 总的来说, 综上所述, 不难看出, 显而易见, 众所周知, 在当今社会, 扮演着重要角色, 息息相关, 有着密切联系.

### B. 禁止句式
- 三段式排比: "不仅...而且...还..." / "它是 A, 是 B, 也是 C" 这类工整三连, 打散.
- 否定式排比: "不是...而是...; 不是...而是..." 连用.
- 系动词回避: AI 爱绕开 "是", 写成 "扮演着...的角色" / "构成了...的基础". 能用 "是 / 有 / 会" 就直说.
- 虚假范围: "在很多方面" / "某种程度上" / "一定意义上".
- 强行升华: "这告诉我们" / "值得深思" / "不禁让人感慨".

### C. 正向要求 (让它读着像人)
- 长短句交替, 别每句都一样长.
- 事实后面带反应 (呼应口吻).
- 具体细节代替抽象名词.
- 允许一点不完美, 不追求每句工整对仗, 太齐整反而假.
'''

CLAUSE_RULES = '''## 断句习惯 (v9 字幕切分硬约束, 写稿时就遵守)

clause 之间用 ASCII "," 或全角 "、。！？；: " 隔开, 不写逗号连不断的 run-on 长句.
每个 clause 理想 2-12 字, **严禁单个 clause > 20 字** (超了会触发渲染兜底切分, 节奏乱).

正例 (clause 都短, 节奏碎):
  海水里的盐, 其实是石头泡出来的, 河水冲刷岩石, 把矿物质一路带进海.

反例 (一个 30+ 字 run-on, 会被腰斩):
  海水之所以是咸的是因为河流长期不断地冲刷地表岩石并将其中溶解的矿物质带入海洋中.
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

# ----- 提纲 (两阶段写稿: Phase 1) -----

OUTLINE_SCHEMA_PROMPT = '''## 输出格式（严格遵守）
只输出一个 JSON 对象，不要 markdown fence，不要任何说明：
{
  "facts": ["<知识点1，带具体数字>", "<知识点2>", ...],  // 3-5 条
  "angle": "<本视频选择的叙事角度，一句话>",
  "hook":  "<建议的开场钩子句，第一句就是信息本身，不超过 20 字>"
}
'''


def build_outline_prompt(job):
    """Pre-script outline prompt: ask LLM to enumerate facts + angle + hook
    BEFORE writing the narration. Output is a JSON dict validated by
    generate_outline(). Returned prompt includes the theme and a strict
    schema so the response is parseable and grounded in real numbers."""
    theme = job.get("theme") or ""
    return (
        f"你是一个科普短视频编导，正在为视频《{theme}》做写稿前的知识梳理。\n\n"
        f"任务：列出这个主题最值得讲的 3-5 个核心知识点（必须带具体数字/事实），"
        f"选择一个最有反常识冲击力的叙事角度，并写出开场钩子句。\n"
        f"不要写正文，只输出提纲。\n\n"
        f"{OUTLINE_SCHEMA_PROMPT}"
    )


def generate_outline(job):
    """One Messages-API call returning (outline_dict, err_msg).

    On success, outline_dict = {"facts": [str, ...], "angle": str, "hook": str}.
    On any failure (network, JSON parse, missing fields), returns
    (None, err_msg) so the daemon can mark the job 'error'."""
    prompt = build_outline_prompt(job)
    try:
        text = llm_client.complete(
            system=(
                "You are a 抖音 short-video editor preparing an outline. "
                "Output a single JSON object and nothing else — no markdown fence, "
                "no prose around it."
            ),
            user=prompt,
            max_tokens=1024,
            timeout=120.0,
        )
    except Exception as e:
        return None, f"LLM call failed: {e}"

    for candidate in _iter_json_objects(text):
        if not isinstance(candidate, dict):
            continue
        facts = candidate.get("facts")
        angle = candidate.get("angle")
        hook = candidate.get("hook")
        if (
            isinstance(facts, list) and all(isinstance(f, str) for f in facts)
            and isinstance(angle, str) and angle
            and isinstance(hook, str) and hook
        ):
            return {"facts": facts, "angle": angle, "hook": hook}, None
    return None, f"could not parse outline JSON; tail={text[-200:]!r}"


# ----- 科普赛道风格: lint 启发式 (守护进程落盘后做软警告, 不 reject) -----

# AI 高频词黑名单 (Humanizer-zh 蒸馏, 科普适用)
_KEPU_AI_WORDS = frozenset([
    "此外", "值得注意的是", "至关重要", "深入探讨", "总的来说",
    "综上所述", "不难看出", "显而易见", "众所周知", "在当今社会",
    "扮演着重要角色", "息息相关", "有着密切联系",
])

# 禁止句式正则 (三段式排比 / 否定式排比 / 系动词回避变体)
# - "不仅...而且...还..." / "是A，是B，也是C" 类工整三连
# - "不是A，而是B；不是C，而是D" 类否定式排比连用
_KEPU_BANNED_PATTERNS = [
    re.compile(r"不仅.{1,15}而且.{1,15}还"),
    re.compile(r"是.{1,8}，是.{1,8}，也是"),
    re.compile(r"不是.{1,8}而是.{1,8}[；;,，、]?不是"),
]

# 强行升华结尾 (跟 v9 硬约束的 "希望对你有帮助" 互补, 升级独立规则名)
_KEPU_BANNED_ENDINGS = [
    "这告诉我们", "值得深思", "不禁让人感慨",
    "希望对你有帮助", "以上就是", "愿你", "希望你",
]

# v9 字幕切分硬约束: 单 clause > 20 字触发 _split_long_clause 兜底, 节奏乱
_KEPU_MAX_CLAUSE_CHARS = 20


def _split_clauses(text):
    """按 v9 标点 (ASCII , 或全角 、，。！？；：) 或换行切 clause: 渲染每行独立, 不切就跨行合并触发 long_clause 误报.
    复用 project 约定的切分集, 跟 _SPLIT_PUNCT 对齐."""
    if not text:
        return []
    # 同时去掉空白后切, 空 clause 丢弃
    return [c.strip() for c in re.split(r"[、，。！？；：,\n]", text) if c.strip()]


def lint_script(script_text):
    """启发式风格 lint. 返回命中规则名列表 (空=干净).

    守护进程在 _write_run_artifacts 之后调用, 只 log 不 reject.
    单测也复用做 gold 稿锚点.
    """
    if not script_text:
        return []
    hits = []

    # A. AI 黑名单词
    for w in _KEPU_AI_WORDS:
        if w in script_text:
            hits.append("ai_word")
            break

    # B. 长 clause (>20 字)
    for clause in _split_clauses(script_text):
        if len(clause) > _KEPU_MAX_CLAUSE_CHARS:
            hits.append("long_clause")
            break

    # C. 禁止句式 (排比/三连)
    for pat in _KEPU_BANNED_PATTERNS:
        if pat.search(script_text):
            hits.append("banned_pattern")
            break

    # D. 强行升华结尾
    for phrase in _KEPU_BANNED_ENDINGS:
        if phrase in script_text:
            hits.append("banned_ending")
            break

    # 去重保持顺序 (按发现顺序, 便于 log 可读)
    seen = set()
    return [h for h in hits if not (h in seen or seen.add(h))]


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
        if job.get("mode") == "video" and job.get("status") in ("pending", "pending_script"):
            jobs.append(job)
    return sorted(jobs, key=lambda j: j.get("created_at", ""))


def build_prompt(job):
    """Single-completion prompt. Returns instruction text for the LLM to
    produce JSON {script, cover} in one response. The daemon does the
    file writes itself; the LLM is just the prose engine."""
    theme = job.get("theme") or ""
    target_seconds = int(job.get("render", {}).get("duration_sec") or DEFAULT_TARGET_SECONDS)
    min_chars, max_chars = script_length_bounds(target_seconds)
    target_chars = int(target_seconds * ESTIMATED_CHARS_PER_SECOND)

    # 注入用户已确认的提纲（如果存在）。强制 LLM 优先使用这些事实，
    # 不要编造其他数字。空 outline 字段（空字符串/None）一律不注入。
    outline = job.get("outline") or {}
    outline_block = ""
    if outline:
        facts = "\n".join(f"- {f}" for f in (outline.get("facts") or []) if f)
        angle = (outline.get("angle") or "").strip()
        hook = (outline.get("hook") or "").strip()
        outline_block = (
            "## 已确认的创作提纲（优先使用这些事实，不要编造其他数字）\n\n"
            f"### 核心知识点\n{facts}\n\n"
            f"### 叙事角度\n{angle}\n\n"
            f"### 建议钩子\n{hook}\n\n"
        )

    return (
        f"为 video-studio 写一段约 {target_seconds} 秒 ({target_chars} 字) 的短视频旁白稿。\n"
        f"主题：{theme}\n\n"

        f"{outline_block}"
        f"{NARRATIVE_SKELETON}\n\n"
        f"{VOICE_GUIDE}\n\n"
        f"{ANTI_AI_RULES}\n\n"
        f"{CLAUSE_RULES}\n\n"

        f"{COVER_INSTRUCTIONS}\n\n"

        f"## 硬约束 (优先级最高)\n"
        f"1. 字数目标 {target_chars} 字, 必须落在 {min_chars}-{max_chars} 字区间, **超过 1500 字直接判失败**\n"
        f"2. 脚本正文 (script 字段) 纯文本, 不要 markdown / 编号 / 标题 / 空行分隔\n"
        f"3. 围绕主题把话讲清楚, 结尾不要开放式问号 / 抒情 / '以上就是...' / '希望对你有帮助'\n\n"

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


def build_length_repair_prompt(job, current_script, min_chars, max_chars):
    """Targeted length-repair prompt. The LLM is asked to return a JSON
    {"script": <revised>, "cover": <unchanged or re-validated>}; the daemon
    writes the file. Replaces the old agent that self-overwrote script.txt."""
    target_seconds = int(job.get("render", {}).get("duration_sec") or DEFAULT_TARGET_SECONDS)
    target_chars = int(target_seconds * ESTIMATED_CHARS_PER_SECOND)
    cur_len = len(current_script)
    if cur_len < min_chars:
        gap = min_chars - cur_len
        direction = (
            f"当前 {cur_len} 字, 比下限少 {gap} 字. 在保留原意和结构的前提下补充细节扩写, "
            f"不要堆砌空洞名词, 不要改写已有好句子."
        )
    else:
        gap = cur_len - max_chars
        direction = (
            f"当前 {cur_len} 字, 比上限多 {gap} 字. 删减冗余 / 重复 / 空洞处, 保留原意和结构, "
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
        f"3. 保留原有内容结构和结尾, 不要开放式问号 / 抒情结尾\n"
        f"4. 只在必要处增删, 不要整篇重写\n\n"
        f"## 输出格式 (严格遵守)\n"
        f"只输出一个 JSON 对象, 不要 markdown fence:\n"
        f'{{"script": "<修订稿全文>", "cover": {{"main": "...", "main_highlight": [s, e], "sub": "..."}}}}\n'
        f"cover 字段如未改动可保持原值, 但必须重新验证 main_highlight 是否仍是语义完整的钩眼词."
    )


def build_repair_prompt(job, current_script, min_chars, max_chars):
    """Backward-compat thin wrapper. Delegates to build_length_repair_prompt so
    older callers (test_script_engine_decouple, test_script_repair) keep working
    with the original 4-arg signature."""
    return build_length_repair_prompt(job, current_script, min_chars, max_chars)


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
    # 科普风格软警告 (不 reject, 仅供人回看 job 日志时定位问题)
    lint_hits = lint_script(script)
    if lint_hits:
        log(f"kepu-lint hits={lint_hits} job={job['id']} (warning only, not rejected)")
    return script, validated_cover, None


def generate_script_repair(job, current_script, min_chars, max_chars):
    """One Messages-API repair pass. Same artifact contract as generate_script."""
    prompt = build_length_repair_prompt(job, current_script, min_chars, max_chars)
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
    """One script job end-to-end across two phases.

    Phase 1 (status='pending'): generate outline via LLM, save to job,
        leave status='ready_outline' for user confirmation. Do NOT write
        script yet.
    Phase 2 (status='pending_script'): user confirmed outline. Generate
        full narration script + cover, finalize to ready_script.
    Other statuses fall through to legacy script-only behavior (defensive
    fallback for any state that ever lands here without an outline)."""
    if job.get("status") == "pending":
        return _process_outline_phase(job)
    # pending_script OR legacy fallback — both treat as "write the script"
    return _process_script_phase(job)


def _process_outline_phase(job):
    """Phase 1: LLM outline → ready_outline. Does not write script.txt."""
    job["status"] = "outlining"
    job["error"] = None
    save_job(job)

    outline, err = generate_outline(job)
    if err:
        job["status"] = "error"
        job["error"] = err
        save_job(job)
        log(f"{job['id']} outline failed: {err[:300]}")
        return False

    job["outline"] = outline
    job["status"] = "ready_outline"
    job["error"] = None
    save_job(job)
    log(f"{job['id']} ready_outline (facts={len(outline.get('facts', []))})")
    return True


def _process_script_phase(job):
    """Phase 2: write the full script using the confirmed outline.

    Keeps the original generate_script + finalize_from_script_file +
    repair_script_length pipeline unchanged. writer_attempt counter is
    reset to 0 here so a re-confirmed outline starts the length-retry
    budget fresh."""
    job["writer_attempt"] = 0
    job["status"] = "writing"
    job["error"] = None
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
