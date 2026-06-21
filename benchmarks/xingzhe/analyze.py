#!/usr/bin/env python3
"""Style analysis over 20 transcripts of the benchmark account.

Outputs analysis.md and a machine-readable stats.json.
"""
import json
import os
import re
from collections import Counter
from pathlib import Path

TX_DIR = Path("/root/.openclaw/workspace/skills/video-studio/benchmarks/xingzhe/transcripts")
MANIFEST = Path("/root/.openclaw/workspace/skills/video-studio/benchmarks/xingzhe/manifest.json")
OUT_MD = Path("/root/.openclaw/workspace/skills/video-studio/benchmarks/xingzhe/analysis.md")
OUT_JSON = Path("/root/.openclaw/workspace/skills/video-studio/benchmarks/xingzhe/stats.json")

# Regex patterns
# Bare number (with possible decimals)
RE_NUMBER = re.compile(r'\d+(?:\.\d+)?')
# Chinese numbers with quantifier
RE_CN_NUMBER = re.compile(r'[一二三四五六七八九十百千万半]+(?:多)?[十百千万亿]?[个颗条只倍层块把人吨公斤克米公里里厘毫米分秒天月年岁分钱]')
# Big number with magnitude/quantifier — actual punchy stats
RE_BIG_NUMBER = re.compile(r'\d+(?:\.\d+)?(?:[万亿千]|[万亿千]?[吨公斤克米公里里厘毫米分秒天月年岁个颗条只倍层块把人元]|\s?倍|\s?%|\s?°|块钱|块)')
# Math comparison: "X倍", "提升X", "差X", "X%" — but not "100%" alone
RE_MATH = re.compile(r'(?:[距]?[相达提]?\s?\d+(?:\.\d+)?\s*(?:倍|%|％|分之|x))|(?:[一-鿿]{0,6}\d+(?:\.\d+)?[亿万千百吨米公里秒个颗条只])|(?:粗略算|算一下|算一算|约等于|差不多)')
RE_QUESTION = re.compile(r'[？?]')
RE_INTERROGATIVE = re.compile(r'(为什么|怎么|是不是|能不能|会不会|到底|究竟|真的|如何)')
RE_BUT = re.compile(r'(但是|不过|然而|其实|真相是|事实上|实际上|别看|别以为|却|然而|只不过)')
RE_PERIOD = re.compile(r'[。！？!?]')
RE_SENT_END = re.compile(r'[。！？!?]')
RE_COMMA = re.compile(r'[,，、]')

# Cross-disciplinary keywords
TOPIC_KEYWORDS = {
    '物理': ['核弹', '原子弹', '氢弹', '引力', '光速', '热力学', '量子', '能量', '爆炸', '火球', 'TNT', '牛顿', '物理', '力学', '辐射', '物质'],
    '生物/进化': ['进化', '基因', '细胞', '物种', '演化', '病毒', '细菌', '生态', '捕食', '繁殖', '耐毒', '毒素', '雄狮', '雌狮', '蚊子', '河马', '泰森', '雄性', '雌性', '动物'],
    '历史/文化': ['古人', '古代', '历史', '文明', '唐朝', '宋朝', '三国', '夏侯惇', '太监', '琥珀', '宝刀', '泰拳', '宋朝', '唐朝', '皇帝', '大清', '王朝', '遗址'],
    '医学/健康': ['病', '毒', '癌', '细胞', '免疫', '脂肪', '熬夜', '睡眠', '健康', '医疗', '医生', '埃博拉', '防腐'],
    '地理/天文': ['地球', '火星', '宇宙', '星系', '太阳', '行星', '海洋', '沙漠', '海拔', '火山', '岩浆', '山峰', '泰森', '藤壶', '藤壶'],
    '数学/计算': ['算', '倍', '差', '提升', '对比', '约等于', '粗略算', '乘', '除', '等于', '总量', '比例', '%', '除以'],
    '段子/反差': ['夏侯惇', '太监', '笑尿', '懒得', '一眼', '一眼假', '一眼望不到', '按在地上', '惊', '离谱', '笑死', '笑哭'],
}

# Hook openers (we'll classify first 30 chars of each)
def classify_opener(text):
    head = text[:30]
    if re.search(r'[？?]', head):
        return '反问开场'
    if re.search(r'^\d', head) or re.search(r'\d+(?:%|万|亿|倍)', head):
        return '数字开场'
    if any(kw in head for kw in ['如果', '假设', '假如']):
        return '假设开场'
    if any(kw in head for kw in ['你以为', '你觉得', '大家都以为', '大多数人', '很多人']):
        return '反常识开场'
    if any(kw in head for kw in ['为什么']):
        return '反问开场'
    return '其他开场'


def classify_ending(text):
    tail = text[-50:]
    if any(kw in tail for kw in ['评论区', '评论', '留言', '你站哪边', '信哪个', '说说', '晒晒', '你遇到过', '你试过', '你有过']):
        return '互动收尾'
    if re.search(r'[？?]', tail[-15:]):
        return '反问收尾'
    if any(kw in tail for kw in ['说出', '吾名', '吓', '点赞', '关注', '下期']):
        return 'IP签名收尾'
    return '叙事收尾'


def extract_short_punchy(text, max_len=20):
    """Find short (≤max_len) sentence-final clauses — potential '段子化' lines."""
    sentences = re.split(r'[。！？!?]', text)
    short = [s.strip() for s in sentences if 5 <= len(s.strip()) <= max_len]
    return short


def detect_punchline(short_sent):
    """Heuristic: a punchline contains noun+verb contrast, or uses rhyme/parallelism,
    or contains famous cultural reference."""
    triggers = [
        '夏侯惇', '太监', '一眼', '笑', '懒得', '离谱', '惊', '哭', '尿',
        '按在地上', '一言', '一眼', '一眼假', '一眼望不到', '一眼万年',
        '彪', '霸气', '大佬', 'P2P', '一派胡言',
        '反手', '擦', '怼', '服了', '麻了', '笑死', '笑哭',
        '艺术', '夸张', '过分', '弄啥嘞', '懂不懂',
        'online', '版本', '史书', '梗', '段子', '一言不合',
    ]
    # 短句 + 含"反差/对立"标点
    if any(t in short_sent for t in triggers):
        return True
    # 破折号
    if re.search(r'[—–\-]{1,3}', short_sent):
        return True
    # 短句开头反差词
    if short_sent.startswith(('别', '没', '不是', '不', '怎么', '居然', '竟然', '明明', '其实', '但', '只是')) and len(short_sent) <= 18:
        return True
    # 重复/对称结构
    if re.search(r'([^，。！？]{1,5})[,\s]+\1', short_sent):  # "X, X"
        return True
    return False


def analyze_one(meta, tx_json):
    full = tx_json['azure']['full_text']
    n_chars = len(full)
    duration = meta['duration_s']
    cps = n_chars / duration if duration else 0

    # Sentences
    sents = [s.strip() for s in RE_SENT_END.split(full) if s.strip()]
    n_sents = len(sents)
    avg_sent_len = sum(len(s) for s in sents) / max(n_sents, 1)
    short_sents = [s for s in sents if len(s) <= 20]
    short_ratio = len(short_sents) / max(n_sents, 1)

    # Numbers
    nums = RE_NUMBER.findall(full)
    cn_nums = RE_CN_NUMBER.findall(full)
    n_nums = len(nums) + len(cn_nums)
    num_per_100 = n_nums / (n_chars / 100)

    # Specific big numbers
    big_nums = RE_BIG_NUMBER.findall(full)
    big_cn = re.findall(r'\d+(?:\.\d+)?(?:万|亿|吨|公里|米|倍|颗|个|层)', full)
    n_big = len(big_nums) + len(big_cn)

    # Math comparison patterns
    math_patterns = RE_MATH.findall(full)
    n_math = len(math_patterns)

    # Questions and interrogatives
    n_q = len(RE_QUESTION.findall(full))
    n_int = len(RE_INTERROGATIVE.findall(full))

    # But/reversal
    n_but = len(RE_BUT.findall(full))

    # Topic distribution
    topic_hits = {}
    for cat, kws in TOPIC_KEYWORDS.items():
        topic_hits[cat] = sum(full.count(k) for k in kws)
    main_topics = sorted(topic_hits.items(), key=lambda x: -x[1])[:3]
    main_topics_str = ','.join(t[0] for t in main_topics if t[1] > 0) or 'none'

    # Openers / endings
    opener = classify_opener(full)
    ending = classify_ending(full)

    # Punchlines
    punchlines = [s for s in extract_short_punchy(full) if detect_punchline(s)]
    n_punch = len(punchlines)

    # IP signature
    has_signature = any(s in full for s in ['说出吾名', '吓汝一跳', '行者道荣', '荣哥', '荣说'])

    return {
        'rank': meta['rank'],
        'aweme_id': meta['aweme_id'],
        'desc': meta['desc'][:60],
        'duration_s': duration,
        'n_chars': n_chars,
        'cps': round(cps, 2),
        'n_sents': n_sents,
        'avg_sent_len': round(avg_sent_len, 1),
        'short_ratio': round(short_ratio, 2),
        'n_nums': n_nums,
        'num_per_100': round(num_per_100, 2),
        'n_big_nums': n_big,
        'n_math_patterns': n_math,
        'n_q': n_q,
        'n_int': n_int,
        'n_but': n_but,
        'n_punchlines': n_punch,
        'punchlines_sample': punchlines[:6],
        'main_topics': main_topics_str,
        'opener': opener,
        'ending': ending,
        'has_ip_sig': has_signature,
    }


def main():
    manifest = json.load(open(MANIFEST))
    results = []
    print(f"Analyzing {len(manifest)} videos...")
    for meta in manifest:
        rank = meta['rank']
        awid = meta['aweme_id']
        tx_json = f"{TX_DIR}/rank_{rank:02d}_{awid}.json"
        if not os.path.exists(tx_json):
            print(f"  [{rank}] no transcript file, skip")
            continue
        try:
            r = analyze_one(meta, json.load(open(tx_json)))
            results.append(r)
            print(f"  [{rank}] {r['n_chars']} chars, {r['cps']} cps, {r['n_nums']} nums, {r['n_punchlines']} punchlines, {r['opener']} / {r['ending']}")
        except Exception as e:
            print(f"  [{rank}] ERROR {e}")
    if not results:
        print("No results to analyze")
        return

    # Aggregate stats
    n = len(results)
    avg = lambda key: sum(r[key] for r in results) / n
    counter = lambda key: Counter(r[key] for r in results)

    summary = {
        'n_videos': n,
        'avg_chars': round(avg('n_chars'), 0),
        'avg_duration_s': round(avg('duration_s'), 0),
        'avg_cps': round(avg('cps'), 2),
        'avg_sents': round(avg('n_sents'), 0),
        'avg_sent_len': round(avg('avg_sent_len'), 1),
        'avg_short_ratio': round(avg('short_ratio'), 2),
        'avg_nums_per_video': round(avg('n_nums'), 1),
        'avg_nums_per_100_chars': round(avg('num_per_100'), 2),
        'avg_big_nums': round(avg('n_big_nums'), 1),
        'avg_math_patterns': round(avg('n_math_patterns'), 1),
        'avg_questions': round(avg('n_q'), 1),
        'avg_interrogatives': round(avg('n_int'), 1),
        'avg_but_count': round(avg('n_but'), 1),
        'avg_punchlines': round(avg('n_punchlines'), 1),
        'openers': counter('opener'),
        'endings': counter('ending'),
        'main_topics': counter('main_topics'),
    }

    # Persist stats
    OUT_JSON.write_text(json.dumps({'summary': summary, 'per_video': results}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"\nStats saved to {OUT_JSON}")
    print(f"\nKey averages:")
    for k, v in summary.items():
        if isinstance(v, (int, float)):
            print(f"  {k}: {v}")
    print(f"  openers: {dict(summary['openers'])}")
    print(f"  endings: {dict(summary['endings'])}")


if __name__ == "__main__":
    main()
