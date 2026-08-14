# README 现状同步设计

## 目标

让根目录 `README.md` 准确描述当前已经接入主流程的 video-studio，消除三阶段、零人工审核、缺少 director 等过期信息。

## 内容范围

- 将产品流程改为“生成提纲 → 用户确认 → 写稿 → Director 分镜 → Render 渲染 → Narrate 配音合成”。
- 说明完整渲染与 `preview_only` 两条路径。
- 补齐四个 systemd path/service、四个触发文件和完整 job 状态机。
- 更新目录布局，加入 director、shotlist 和提纲确认相关入口。
- 修正本地 TTS/音色依赖、默认时长、渲染参数及运行环境说明。
- 将测试说明改成符合当前事实的分层描述，不宣称全量测试无外部依赖或始终全绿。
- 保留封面、强制对齐、字幕切分和 R2 产物等仍然有效的说明。

## 明确不包含

- 不记录尚未接入生产主流程的 editorial engine。
- 不处理或扩写密码、Cookie、端口暴露等安全问题。
- 不修改 Python、前端、systemd、Docker 或测试代码。
- 不改变运行中的 job、触发器和生成产物。

## 文档结构

沿用现有 README 的阅读顺序：项目定位、产物、工作原理、快速开始、目录布局、测试、依赖与配置。架构图更新为当前真实数据流，避免进行与本次同步无关的全面重写。

## 验证标准

- README 不再出现“三阶段”“零人工审”或 script 直接触发 render 的描述。
- 安装命令包含 director watcher。
- 状态机包含提纲确认和 `ready_shotlist`。
- editorial engine 不出现在 README。
- README 中提到的文件、服务和触发器均在仓库中存在。
