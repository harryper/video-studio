#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
video-studio Web 后端

本应用是抖音短视频自动创作链路的核心 Web 入口，支持:
  - 主题驱动自动创作（pending → ready_script → rendered → final）
  - 三个 systemd 阶段守护进程（script / render / narrate）通过 trigger 文件联动
  - 三档画幅（16:9 / 9:16 / 1:1）
  - 历史 job 列表 + 详情面板

数据流:
  用户创建 job → app.py 触摸 .video-script-trigger → systemd 启动 script 守护进程
  script 守护进程写好旁白稿 → 触摸 .video-render-trigger → render 守护进程起
  render 守护进程渲染视频 → 触摸 .video-narrate-trigger → narrate 守护进程起
  narrate 守护进程 TTS + BGM + 合并 → 状态变 final

跨 repo 依赖: minimax_tts.py / mix_with_bgm.py / voice_registry.json
  通过 systemd Environment=PATH 同时指向 voice-studio/scripts/，本应用不 import。
"""

import json
import os
import re
import shutil
import sys
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

# ── 路径常量 ───────────────────────────────────────────────────────────
SKILL_DIR = Path(__file__).resolve().parent
JOBS_DIR = SKILL_DIR / 'jobs' / 'video'
ARCHIVE_DIR = SKILL_DIR / 'archive' / 'video'
RUNS_DIR = SKILL_DIR / 'runs'
TRIGGERS_DIR = SKILL_DIR
REFERENCE_SCRIPT_DIR = SKILL_DIR / 'reference-scripts'

# 三档画幅预设
ASPECT_PRESETS = {
    '16:9': (1920, 1080),
    '9:16': (1080, 1920),
    '1:1': (1080, 1080),
}

# 视频阶段触发器映射（systemd path unit 监听这几个文件）
VIDEO_TRIGGER_MAP = {
    'script': '.video-script-trigger',
    'render': '.video-render-trigger',
    'narrate': '.video-narrate-trigger',
}

# ── Flask 应用 ───────────────────────────────────────────────────────
app = Flask(__name__, template_folder='templates', static_folder='static')
app.config['JSON_AS_ASCII'] = False
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False


# ── 工具函数 ─────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now().isoformat(timespec='seconds')


def _job_path(job_id: str) -> Path:
    return JOBS_DIR / f'{job_id}.json'


def save_job(job: dict) -> None:
    """原子写入 job JSON（先写 .tmp 再 rename，避免读到半截）。"""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    path = _job_path(job['id'])
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def load_job(job_id: str) -> dict | None:
    path = _job_path(job_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as e:
        print(f'[video-studio] failed to load job {job_id}: {e}', file=sys.stderr)
        return None


def delete_job(job: dict) -> None:
    """删除 job JSON + 清理生成的本地产物。"""
    job_id = job['id']
    path = _job_path(job_id)
    if path.exists():
        path.unlink()
    # 清理 runs/<job_id>/ 目录
    run_dir = RUNS_DIR / job_id
    if run_dir.exists() and run_dir.is_dir():
        try:
            shutil.rmtree(run_dir)
        except OSError as e:
            print(f'[video-studio] failed to remove {run_dir}: {e}', file=sys.stderr)


def archive_job(job: dict) -> None:
    """归档到 archive/video/<id>.json。"""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    src = _job_path(job['id'])
    dst = ARCHIVE_DIR / f'{job["id"]}.json'
    if src.exists():
        shutil.move(str(src), str(dst))


def job_response(job: dict) -> dict:
    """对外返回的 job 字段（前端可消费的子集）。"""
    return {
        'id': job.get('id'),
        'mode': job.get('mode', 'video'),
        'theme': job.get('theme'),
        'status': job.get('status'),
        'script': job.get('script'),
        'script_meta': job.get('script_meta'),
        'render': job.get('render'),
        'audio': job.get('audio'),
        'final': job.get('final'),
        'error': job.get('error'),
        'created_at': job.get('created_at'),
        'updated_at': job.get('updated_at'),
        'logs': job.get('logs', []),
    }


def _touch_video_trigger(stage: str) -> bool:
    """触摸指定阶段的 trigger 文件，唤醒对应的 systemd path unit。"""
    if stage not in VIDEO_TRIGGER_MAP:
        return False
    path = TRIGGERS_DIR / VIDEO_TRIGGER_MAP[stage]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()), encoding='utf-8')
    return True


def list_jobs(mode: str | None = None) -> list[dict]:
    """列出当前活跃任务（jobs/video/ 下所有 JSON）。

    Skip malformed/legacy job files (must have 'id' key) so a single bad
    JSON does not break the whole list endpoint.
    """
    if not JOBS_DIR.exists():
        return []
    out = []
    for p in sorted(JOBS_DIR.glob('v_*.json'), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            job = json.loads(p.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(job, dict) or 'id' not in job:
            # Skip legacy/malformed job files.
            continue
        if mode and job.get('mode') != mode:
            continue
        out.append(job_response(job))
    return out


# ── 路由 ─────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/health')
def health():
    return jsonify({
        'ok': True,
        'app': 'video-studio',
        'version': '1.0.0',
        'aspects': list(ASPECT_PRESETS.keys()),
        'jobs_count': len(list_jobs()),
    })


@app.route('/api/jobs', methods=['POST'])
def create_job():
    """创建 video job。Body: {theme, aspect_ratio}"""
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get('mode', 'video')
    if mode != 'video':
        return jsonify({'error': '本服务仅支持 mode=video'}), 400

    theme = (data.get('theme') or '').strip()
    if not theme:
        return jsonify({'error': '主题不能为空'}), 400

    aspect_ratio = (data.get('aspect_ratio') or '16:9').strip()
    if aspect_ratio not in ASPECT_PRESETS:
        return jsonify({'error': f'画幅必须是 {", ".join(ASPECT_PRESETS.keys())}'}), 400
    width, height = ASPECT_PRESETS[aspect_ratio]

    video_id = 'v_' + str(uuid.uuid4())[:8]
    job = {
        'id': video_id,
        'mode': 'video',
        'theme': theme,
        'status': 'pending',  # pending → ready_script → rendered → final
        'script': None,
        'script_meta': None,
        'render': {
            'width': width,
            'height': height,
            'aspect_ratio': aspect_ratio,
            'fps': 15,
            'duration_sec': 150,
        },
        'audio': {
            'voice': 'Chinese (Mandarin)_Radio_Host',
            'voice_display_name': '电台男主播',
            'speed': 1.0,
            'bgm_volume': 0.06,
            'bgm_asset': 'bgm_default.mp3',
        },
        'final': None,
        'error': None,
        'created_at': _now_iso(),
        'updated_at': _now_iso(),
        'logs': [],
    }
    save_job(job)
    # 唤醒 script 守护进程
    try:
        _touch_video_trigger('script')
    except OSError as e:
        print(f'[video-studio] failed to touch script trigger: {e}', file=sys.stderr)
    return jsonify({'job_id': video_id, 'job': job_response(job)})


@app.route('/api/jobs/<job_id>', methods=['GET'])
def get_job(job_id):
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    return jsonify(job_response(job))


@app.route('/api/jobs/<job_id>', methods=['PATCH'])
def update_job(job_id):
    """主 session 更新任务状态（script 守护进程/render 守护进程/narrate 守护进程 都用这个端点回报进度）。"""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404

    data = request.get_json(force=True, silent=True) or {}
    # 白名单：守护进程可能更新的字段
    for key in (
        'status', 'script', 'render', 'audio', 'final', 'error', 'script_meta',
    ):
        if key in data:
            job[key] = data[key]
    job['updated_at'] = _now_iso()
    job.setdefault('logs', []).append(f'{_now_iso()} PATCH: {list(data.keys())}')
    save_job(job)
    return jsonify(job_response(job))


@app.route('/api/jobs/<job_id>', methods=['DELETE'])
def delete_job_api(job_id):
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    delete_job(job)
    return jsonify({'ok': True})


@app.route('/api/jobs/<job_id>/archive', methods=['POST'])
def archive_job_api(job_id):
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    archive_job(job)
    return jsonify({'ok': True})


@app.route('/api/jobs/<job_id>/render', methods=['POST'])
def trigger_video_render(job_id):
    """重跑渲染：把 job 状态回到 ready_script（脚本保留），唤醒 render 守护进程。
    注意：script 守护进程会先跑（不写脚本，直接跳过），然后 render 接管。
    为了简化，这里我们直接唤醒 render 守护进程（要求 script 守护进程能识别 ready_script 状态）。
    """
    job = load_job(job_id)
    if not job or job.get('mode') != 'video':
        return jsonify({'error': 'video job not found'}), 404
    job['status'] = 'ready_script'
    job['error'] = None
    job.setdefault('logs', []).append(f'{_now_iso()} render re-triggered')
    save_job(job)
    _touch_video_trigger('render')
    return jsonify({'ok': True, 'job': job_response(job)})


@app.route('/api/jobs/<job_id>/narrate', methods=['POST'])
def trigger_video_narrate(job_id):
    """重跑配音：状态回到 rendered，唤醒 narrate 守护进程。"""
    job = load_job(job_id)
    if not job or job.get('mode') != 'video':
        return jsonify({'error': 'video job not found'}), 404
    job['status'] = 'rendered'
    job['error'] = None
    job.setdefault('logs', []).append(f'{_now_iso()} narrate re-triggered')
    save_job(job)
    _touch_video_trigger('narrate')
    return jsonify({'ok': True, 'job': job_response(job)})


@app.route('/__internal/touch-trigger', methods=['POST'])
def internal_touch_trigger():
    """内部端点，供守护进程或外部触发器调用。Body: {"trigger": "script"|"render"|"narrate"}"""
    data = request.get_json(force=True, silent=True) or {}
    trigger = data.get('trigger')
    if trigger not in VIDEO_TRIGGER_MAP:
        return jsonify({'error': f'unknown trigger {trigger!r}'}), 400
    _touch_video_trigger(trigger)
    return jsonify({'ok': True, 'trigger': trigger})


@app.route('/api/jobs', methods=['GET'])
def list_jobs_api():
    mode = request.args.get('mode', 'video')
    return jsonify(list_jobs(mode))


# ── 静态资源 ───────────────────────────────────────────────────────
@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)


# ── 错误处理 ───────────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'not found'}), 404
    return render_template('index.html'), 200


@app.errorhandler(500)
def server_error(e):
    print(f'[video-studio] 500: {e}\n{traceback.format_exc()}', file=sys.stderr)
    return jsonify({'error': 'internal server error'}), 500


# ── 入口 ───────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', '9998'))
    print(f'[video-studio] starting on :{port}, JOBS_DIR={JOBS_DIR}')
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
