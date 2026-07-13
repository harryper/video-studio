# Prompt 模板 · prompt-template

> 组装 prompt 的标准结构。**先填好骨架，再用具体内容替换变量**。

---

## 标准 Prompt 结构（5 段）

每张图的 prompt 由 5 段组成，按顺序拼接：

```
[1. 风格前缀] + [2. 主体描述] + [3. 角色描述] + [4. 动作与场景] + [5. 批注与结尾]
```

### 1. 风格前缀（必带，每张图都用同样一段）

```
A minimal ink-line illustration, pure white background, no paper texture, no shadow, no gradient. Thin black hand-drawn lines, slightly wobbly, deliberately imperfect.
```

### 2. 主体描述（根据构图模式填）

| 模式 | 主体描述骨架 |
|------|-------------|
| **钩子** | `[反常识对象] next to the cat, looking puzzled` |
| **数据对比** | `three simple outlined bar shapes of different heights labeled [标签 1], [标签 2], [标签 3]` |
| **流程** | `a [对象] with [动作 1] happening, with [动作 2] resulting in [结果]` |
| **排行榜** | `a simple hand-drawn ranking list with bars of decreasing height: [排名 1], [排名 2], ..., [被淘汰项] with a big red X crossing it out` |
| **反转** | `the frame split into two halves: left side [视角 A] with [结果 A], right side [视角 B] with [结果 B]` |

### 3. 角色描述（每张图都带，**完全照抄**以下模板）

```
A simple small anthropomorphic cat character (round head, two small triangle ears, two small dot eyes, single small round monocle on a thin gold chain, small bowtie collar) drawn in the style of a quirky worker. Cat is small in frame occupying about 30 to 40 percent of canvas, lots of negative white space.
```

### 4. 动作与场景（具体到每张图）

**关键修饰词**：
- 角色在做事：拿工具 / 指向 / 歪头 / 站着审视
- 物理动作：must be physically possible
- 物理隐喻：低科技、怪诞但成立

**禁止**：
- 大动作（跑、跳、夸张姿势）
- 复杂背景（多个道具挤在一起）
- 卖萌表情

### 5. 批注与结尾（必带）

```
A few small handwritten Chinese annotations in red and blue floating around: red [红批注], blue [蓝批注]. 16:9 horizontal, pure white background.
```

---

## 颜色规范（必守）

| 颜色 | 用途 | 关键词 |
|------|------|--------|
| 🔴 **红** `#E74C3C` | 结论 / 警告 / 否定 / 数字冲击 | "red annotation" |
| 🟠 **橙** `#F39C12` | 疑问 / 引导 | "orange annotation" |
| 🔵 **蓝** `#3498DB` | 标签 / 标题 / 中性 | "blue annotation" |

**禁止**：绿、紫、黄等其他颜色，印刷体批注。

---

## 批注长度规范

- 单条批注：**1-6 个字 / 1-2 个词**
- 总批注数：**3-5 条**（多了会乱）
- 关键词必须**放红/蓝批注**里（结论用红，标签用蓝）

---

## 完整 Prompt 示例

### 模式 1 - 钩子图

```text
A minimal ink-line illustration, pure white background, no paper texture, no shadow, no gradient. Thin black hand-drawn lines, slightly wobbly, deliberately imperfect.

A small simple drawn watermelon next to a puzzled cat with mouth forming a question mark shape.

A simple small anthropomorphic cat character (round head, two small triangle ears, two small dot eyes, single small round monocle on a thin gold chain, small bowtie collar) drawn in the style of a quirky worker. Cat is small in frame occupying about 30 to 40 percent of canvas, lots of negative white space. Cat is looking puzzled, head tilted, mouth forming "?".

A few small handwritten Chinese annotations in red and blue floating around: red text 甜？？, blue text 4-7 percent 蔗糖.

Clean, witty, slightly absurd but not cute. 16:9 horizontal, pure white background.
```

### 模式 2 - 数据对比（柱状）

```text
A minimal ink-line illustration, pure white background, no paper texture, no shadow, no gradient. Thin black hand-drawn lines, slightly wobbly, deliberately imperfect.

Three simple outlined bar shapes of different heights: a short bar labeled 西瓜 3000kg, a medium bar labeled 甜菜 4000kg, and a tall bar labeled 甘蔗 7000kg in small handwritten Chinese text.

A simple small anthropomorphic cat character (round head, two small triangle ears, two small dot eyes, single small round monocle on a thin gold chain, small bowtie collar) drawn in the style of a quirky worker. Cat is small in frame occupying about 30 percent of canvas, lots of negative white space. Cat stands beside the bars, one paw pointing at the tall bar.

A few small handwritten Chinese annotations in red and blue floating around: red text 6 倍差距 with arrow pointing up at the tall bar, blue text 一亩地 near the cat.

16:9 horizontal, pure white background.
```

### 模式 4 - 排行榜

```text
A minimal ink-line illustration, pure white background, no paper texture, no shadow, no gradient. Thin black hand-drawn lines, slightly wobbly, deliberately imperfect.

A simple hand-drawn ranking list with bars of decreasing height: tallest bar 甘蔗甜菜 80 percent, then shorter bar 玉米果葡糖浆, then 棕榈糖 椰枣糖, then at the bottom a tiny bar labeled 西瓜 with a big red X crossing it out.

A simple small anthropomorphic cat character (round head, two small triangle ears, two small dot eyes, single small round monocle on a thin gold chain, small bowtie collar) drawn in the style of a quirky worker. Cat is small in frame occupying about 30 percent of canvas, lots of negative white space. Cat stands beside the ranking, hands on hips, looking at the X.

A few small handwritten Chinese annotations in red and blue floating around: red text 连参赛资格都没有 with arrow pointing at the X, blue text 制糖赛道 near the top.

16:9 horizontal, pure white background.
```

### 模式 5 - 反转对比

```text
A minimal ink-line illustration, pure white background, no paper texture, no shadow, no gradient. Thin black hand-drawn lines, slightly wobbly, deliberately imperfect.

The frame is split into two halves by a simple vertical line: left side shows a small sugar factory with a watermelon being rejected at the door with a red X mark, right side shows a fresh fruit market stall with the same watermelon being sold.

A simple small anthropomorphic cat character (round head, two small triangle ears, two small dot eyes, single small round monocle on a thin gold chain, small bowtie collar) drawn in the style of a quirky worker. Cat is small in frame occupying about 30 percent of canvas, lots of negative white space. Cat stands in the middle looking between the two halves with a knowing expression.

A few small handwritten Chinese annotations in red and blue floating around: red text 上千亿流水 on the right, blue text 6-9 月 2 亿吨 on the right, blue text 制糖 vs 鲜食 at the top.

16:9 horizontal, pure white background.
```

---

## Prompt 优化技巧

### 1. 强调"工人感"

> 加 `drawn in the style of a quirky worker`，让 AI 把角色理解为"在做事的工人"而不是"萌系吉祥物"。

### 2. 强留白

> 重复两次"lots of negative white space"——AI 容易忽略一次。

### 3. 强纯白

> 重复"pure white background, no paper texture, no shadow, no gradient"——必须全 4 个否定词都在。

### 4. 弱化萌系

> 显式写"not cute, not childish, not adorable"——防止 AI 飘回萌系。

### 5. 物理隐喻落地

> 把每个抽象概念翻译成"具体物体 + 具体动作"：
> - "信息井" → `a well filled with paper scraps`
> - "想法压机" → `a press machine squeezing a thought into a brick`
> - "拉水" → `a truck 90 percent filled with water leaking out`

---

## 自检 prompt 是否合规

出 prompt 之前过一遍：

- [ ] 风格前缀完整（5 句都在）？
- [ ] 角色描述用了标准模板（不偷工减料）？
- [ ] 主体只占 30-50% 画面（已显式说明）？
- [ ] 批注只用了红 / 蓝（没有其他颜色）？
- [ ] 批注是手写感（handwritten）？
- [ ] 角色是 worker（不是 cute / kawaii）？
- [ ] 16:9 横版已说明？
- [ ] 纯白底已说明（4 个否定词都在）？
