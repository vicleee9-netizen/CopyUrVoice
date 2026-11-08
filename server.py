# -*- coding: utf-8 -*-
"""MiMo 配音台 - 本地服务器
纯 Python 标准库，无需安装任何依赖。
双击「启动.bat」即可运行。
"""
import base64
import io
import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
import wave
from urllib.parse import unquote
import webbrowser
import http.server
import socketserver

BASE = os.path.dirname(os.path.abspath(__file__))
SAMPLES_DIR = os.path.join(BASE, "samples")
OUTPUT_DIR = os.path.join(BASE, "output")
CONFIG_PATH = os.path.join(BASE, "config.json")
API_URL = "https://api.xiaomimimo.com/v1/chat/completions"
PORT = 8765

for d in (SAMPLES_DIR, OUTPUT_DIR):
    os.makedirs(d, exist_ok=True)

PRESET_VOICES = [
    {"id": "冰糖", "label": "冰糖 · 中文女声"},
    {"id": "茉莉", "label": "茉莉 · 中文女声"},
    {"id": "苏打", "label": "苏打 · 中文男声"},
    {"id": "白桦", "label": "白桦 · 中文男声"},
    {"id": "Mia", "label": "Mia · 英文女声"},
    {"id": "Chloe", "label": "Chloe · 英文女声"},
    {"id": "Milo", "label": "Milo · 英文男声"},
    {"id": "Dean", "label": "Dean · 英文男声"},
]


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf8") as f:
            return json.load(f)
    except Exception:
        return {"api_key": ""}


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


ERROR_LOG = os.path.join(BASE, "错误日志.txt")

# 不可见字符：从微信/网页/PDF 粘稿子时最容易混进来的一批，
# 也是接口报 400 Invalid request parameters 的常见来源
_INVISIBLE_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f"
    "\u200b-\u200f\u202a-\u202e\u2060\ufeff\u00ad]"
)


def clean_text(s):
    """清洗稿子：去掉控制字符、BOM、零宽字符和不配对的代理项。
    这些字符人眼看不见，但会让接口判定参数非法（HTTP 400）。"""
    s = s or ""
    s = _INVISIBLE_RE.sub("", s)
    # 落单的代理项（emoji 被截断时产生）无法编码，接口会直接 400
    s = s.encode("utf8", "ignore").decode("utf8", "ignore")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t\u3000]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _log_fail(model, text, style, prev, msg):
    try:
        with open(ERROR_LOG, "a", encoding="utf8") as f:
            f.write(
                "[%s] model=%s 字数=%d 风格=%d字 上文=%d段\n  错误: %s\n  正文前60字: %s\n\n"
                % (
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    model,
                    len(text or ""),
                    len(style or ""),
                    len(prev or []),
                    msg,
                    (text or "")[:60].replace("\n", " "),
                )
            )
    except Exception:
        pass


def _post_api(api_key, body, timeout=300):
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# 接口偶尔会无缘无故返回 400 Invalid request parameters（同样的请求重发就好），
# 这几类状态码都按「临时故障」处理，退避重试
RETRY_CODES = (400, 408, 409, 422, 429, 500, 502, 503, 504)


def _post_retry(api_key, body, tries=4, base_wait=1.5):
    """带退避的重试：400/429/5xx 都算临时故障"""
    last = ""
    for i in range(tries):
        try:
            return _post_api(api_key, body), None
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf8", errors="ignore")[:300]
            except Exception:
                pass
            last = "HTTP %d: %s" % (e.code, detail or e.reason)
            if e.code not in RETRY_CODES:
                return None, last
        except Exception as e:
            last = "网络错误: %s" % e
        if i < tries - 1:
            time.sleep(base_wait * (i + 1))
    return None, last


def _split_for_fallback(text, limit=450):
    """按句号切块，尽量不在句子中间断开"""
    pieces, cur = [], ""
    for seg in re.split(r"(?<=[。！？!?\n])", text):
        if not seg:
            continue
        if len(cur) + len(seg) <= limit:
            cur += seg
            continue
        if cur:
            pieces.append(cur)
            cur = ""
        while len(seg) > limit:
            cut = seg.rfind("，", 0, limit)
            if cut < limit // 2:
                cut = limit - 1
            pieces.append(seg[: cut + 1])
            seg = seg[cut + 1 :]
        cur = seg
    if cur:
        pieces.append(cur)
    return [p for p in pieces if p.strip()]


def _one_tts(api_key, model, style, voice, text, prevs):
    """单次调用，返回 wav 字节"""
    messages = [{"role": "user", "content": style}]
    for p in (prevs or [])[-2:]:
        messages.append({"role": "assistant", "content": p})
        messages.append(
            {"role": "user", "content": "接着刚才的语气，保持同样的音高、语速和节奏继续说。"}
        )
    messages.append({"role": "assistant", "content": text})
    body = {"model": model, "messages": messages, "audio": {"format": "wav", "voice": voice}}
    d, err = _post_retry(api_key, body, tries=3)
    if d is None:
        raise ValueError(err)
    a = d["choices"][0]["message"].get("audio")
    if not a or "data" not in a:
        raise ValueError("返回里没有音频")
    return base64.b64decode(a["data"])


def long_text_fallback(api_key, model, style, text, voice):
    """长稿被接口拒收时的兜底：拆成 450 字左右的块逐段生成，再拼成一条。
    返回 (wav_bytes, 段数)。语气接续照旧，段间留 0.22 秒气口。"""
    pieces = _split_for_fallback(text)
    frames, sr, done = [], None, []
    for i, p in enumerate(pieces):
        try:
            raw = _one_tts(api_key, model, style, voice, p, done[-2:])
        except Exception:
            raw = _one_tts(api_key, model, "", voice, p, done[-2:])
        with wave.open(io.BytesIO(raw), "rb") as w:
            if sr is None:
                sr = w.getframerate()
            if w.getframerate() != sr or w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise ValueError("音频参数不一致，无法合并")
            frames.append(w.readframes(w.getnframes()))
        done.append(p)
        print("  [兜底] 第 %d/%d 段完成" % (i + 1, len(pieces)))
    gap = b"\x00\x00" * int(sr * 0.22)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(gap.join(frames))
    return buf.getvalue(), len(pieces)


def call_mimo(api_key, model, style, text, voice, prev=None):
    """调用 MiMo TTS，返回 (audio_dict, error_str)

    prev: 前几段的原文。作为对话历史一起发送，让模型"接着上一段的语气说"。
    实测：独立生成两段音高差 41.8Hz(19.4%)，带上下文后降到 12.4Hz(5.7%)，
    且只合成最后一段，不会重念历史内容。

    容错：接口偶发 400（参数被判定非法）时自动降级重试——
    第 1 次原样发；第 2 次去掉语气接续上下文；第 3 次再去掉风格指令并剥离行内标签。
    三次都失败才报错，并把出错请求写进「错误日志.txt」便于定位。
    """
    text = clean_text(text)
    style = clean_text(style or "")
    prevs = [clean_text(p) for p in (prev or [])]
    prevs = [p for p in prevs if p][-2:]

    # 逐级降级的三个版本：完整 → 去上下文 → 最简（去风格、去标签）
    plain = re.sub(r"[（(][^）)]{0,8}[）)]", "", text).strip() or text
    variants = [
        ("完整请求", style, prevs, text),
        ("去掉接续上下文", style, [], text),
        ("最简请求", "", [], plain),
    ]

    last = ""
    for i, (label, st, pv, tx) in enumerate(variants):
        messages = [{"role": "user", "content": st}]
        for p in pv:
            messages.append({"role": "assistant", "content": p})
            messages.append(
                {"role": "user", "content": "接着刚才的语气，保持同样的音高、语速和节奏继续说。"}
            )
        messages.append({"role": "assistant", "content": tx})
        body = {"model": model, "messages": messages, "audio": {"format": "wav", "voice": voice}}
        d, last = _post_retry(api_key, body, tries=3)
        if d is not None:
            audio = d.get("choices", [{}])[0].get("message", {}).get("audio")
            if audio and "data" in audio:
                if i > 0:
                    print("[重试成功] %s 生成了音频" % label)
                return audio, None
            last = "返回里没有音频: %s" % json.dumps(d, ensure_ascii=False)[:200]
        else:
            fatal = last.startswith("HTTP") and not any(
                ("HTTP %d" % c) in last for c in RETRY_CODES
            )
            if fatal:  # 密钥失效之类，换发法也没用
                _log_fail(model, tx, st, pv, last)
                return None, last
    # 音色名失效（模型改名或下架）时退回默认音色再试一次（仅预置音色模式）
    if "Unknown voice" in last and model == "mimo-v2.5-tts" and voice not in ("冰糖", ""):
        print("[重试] 音色不可用，改用默认音色 冰糖")
        body = {
            "model": model,
            "messages": [
                {"role": "user", "content": style},
                {"role": "assistant", "content": text},
            ],
            "audio": {"format": "wav", "voice": "冰糖"},
        }
        d, err2 = _post_retry(api_key, body, tries=2)
        if d is not None:
            audio = d.get("choices", [{}])[0].get("message", {}).get("audio")
            if audio and "data" in audio:
                return audio, None
        last = err2 or last

    _log_fail(model, text, style, prevs, last)

    # 最后一招：长稿被拒收就自动拆段生成再拼成一条，别让用户白等
    if "400" in last and len(text) > 700:
        try:
            print("[兜底] 长稿被拒收（%d 字），改为自动拆段生成" % len(text))
            wav_bytes, n = long_text_fallback(api_key, model, style, text, voice)
            return {"data": base64.b64encode(wav_bytes).decode(), "format": "wav"}, None
        except Exception as e:
            last += " | 自动拆段也失败: %s" % str(e)[:120]

    if "400" in last:
        return None, (
            "接口连续拒收（HTTP 400 Invalid request parameters，含自动重试 3 轮）。"
            "这是小米接口偶发的抽风，通常等一下再点一次就好；"
            "如果每次都报，把「错误日志.txt」发我。原始返回：" + last[:160]
        )
    return None, last


# ---------------- 音频清晰化（纯标准库，无依赖） ----------------
# 处理链：去直流 → 高通 90Hz（去低频轰鸣）→ 300Hz 减浑浊
#        → 3.2kHz 提存在感 → 响度归一 + 软限幅 → 首尾静音裁剪
import math
import array


def _biquad(kind, f0, q, gain_db, sr):
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f0 / sr
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2.0 * q)
    if kind == "peak":
        b0, b1, b2 = 1 + alpha * A, -2 * cw, 1 - alpha * A
        a0, a1, a2 = 1 + alpha / A, -2 * cw, 1 - alpha / A
    elif kind == "lowshelf":
        sa = 2.0 * math.sqrt(A) * alpha
        b0 = A * ((A + 1) - (A - 1) * cw + sa)
        b1 = 2 * A * ((A - 1) - (A + 1) * cw)
        b2 = A * ((A + 1) - (A - 1) * cw - sa)
        a0 = (A + 1) + (A - 1) * cw + sa
        a1 = -2 * ((A - 1) + (A + 1) * cw)
        a2 = (A + 1) + (A - 1) * cw - sa
    else:  # hp
        b0, b1, b2 = (1 + cw) / 2.0, -(1 + cw), (1 + cw) / 2.0
        a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    return (b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


class _Filter:
    """流式双二阶滤波器，分块处理省内存"""

    def __init__(self, coeffs):
        self.b0, self.b1, self.b2, self.a1, self.a2 = coeffs
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0

    def run(self, buf):
        b0, b1, b2, a1, a2 = self.b0, self.b1, self.b2, self.a1, self.a2
        x1 = self.x1; x2 = self.x2; y1 = self.y1; y2 = self.y2
        out = [0.0] * len(buf)
        for i, x in enumerate(buf):
            y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1 = x1, x
            y2, y1 = y1, y
            out[i] = y
        self.x1, self.x2, self.y1, self.y2 = x1, x2, y1, y2
        return out


def analyze_audio(wav_bytes, text_len=0):
    """量每段的音高/语速/响度，用于检查多段一致性。
    参考 GPT-SoVITS 社区的验收标准：F0 偏差 <10%、语速波动 ±5%。
    """
    import io
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            sr = w.getframerate()
            ch = w.getnchannels()
            raw = w.readframes(min(w.getnframes(), sr * 8))  # 只分析前 8 秒，够代表
            dur = w.getnframes() / float(sr)
    except Exception:
        return {}
    a = array.array("h")
    a.frombytes(raw)
    if ch == 2:
        a = a[::2]
    if not a:
        return {}

    # 音高：自相关法，40ms 一帧
    N = int(sr * 0.04)
    lo, hi = int(sr / 400.0), int(sr / 90.0)
    pitches = []
    for i in range(0, len(a) - N, N * 2):
        seg = a[i:i + N]
        if sum(v * v for v in seg) / N < 1e6:
            continue
        best, bl = 0.0, 0
        for lag in range(lo, hi):
            s = 0
            for j in range(0, N - lag, 6):
                s += seg[j] * seg[j + lag]
            if s > best:
                best, bl = s, lag
        if bl:
            pitches.append(sr / bl)
    pitches.sort()
    f0 = pitches[len(pitches) // 2] if pitches else 0

    rms = 0.0
    if a:
        rms = math.sqrt(sum(v * v for v in a[::5]) / max(1, len(a[::5]))) / 32768.0
    return {
        "f0": round(f0),
        "rate": round(text_len / dur, 1) if dur else 0,
        "rms": round(20 * math.log10(max(rms, 1e-9)), 1),
        "dur": round(dur, 1),
    }


def enhance_pcm(wav_bytes, strength=1.0):
    """输入完整 wav 字节流，做清晰化，返回新的 wav 字节流"""
    import io
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        return wav_bytes, {"skipped": True, "reason": "非16bit"}
    a = array.array("h")
    a.frombytes(raw)
    if ch == 2:  # 立体声取左声道（TTS 正常都是单声道，兜底）
        a = a[::2]
    n = len(a)
    if n < sr // 10:
        return wav_bytes, {"skipped": True}

    x = [v / 32768.0 for v in a]
    # 估算原始响度（只统计有声帧）
    stats = []
    w = max(1, sr // 50)  # 20ms
    for i in range(0, n - w, w):
        s = 0.0
        for v in x[i:i + w]:
            s += v * v
        stats.append(math.sqrt(s / w))
    active = [r for r in stats if r > 0.003]
    rms0 = math.sqrt(sum(r * r for r in active) / len(active)) if active else 0.0
    peak0 = max(abs(v) for v in x) or 1e-9

    # 1) 高通 90Hz，两级（-24dB/oct），切掉次低频
    hp = _Filter(_biquad("hp", 90.0, 0.707, 0, sr))
    x = hp.run(x)
    x = hp.run(x)
    # 2) 低频搁架 -4dB @170Hz，压掉 90-140Hz 那股轰鸣（闷的主因）
    x = _Filter(_biquad("lowshelf", 170.0, 0.9, -4.0 * strength, sr)).run(x)
    # 3) 300Hz 减一点浑浊，别过头
    x = _Filter(_biquad("peak", 300.0, 1.1, -2.0 * strength, sr)).run(x)
    # 4) 3.2kHz 提存在感（辅音清晰带）
    x = _Filter(_biquad("peak", 3200.0, 0.9, 4.0 * strength, sr)).run(x)

    # 4) 响度归一：有声部分拉到 -19.5 dBFS，并留出峰值余量
    target = 10 ** (-19.5 / 20.0)
    cur = math.sqrt(sum(v * v for v in x[::7]) / max(1, len(x[::7]))) or 1e-9
    gain = target / cur
    gain = max(0.6, min(gain, 6.0))
    ceil = 10 ** (-1.0 / 20.0)
    out = [0.0] * n
    for i in range(n):
        v = x[i] * gain
        av = -v if v < 0 else v
        if av > ceil * 0.85:  # 软限幅，避免削顶爆音
            t = (av - ceil * 0.85) / (ceil * 0.15)
            av = ceil * 0.85 + ceil * 0.15 * math.tanh(t)
            v = -av if v < 0 else av
        out[i] = v

    # 5) 首尾静音裁剪（保留 0.15s）
    thr = 0.006
    first, last = 0, n - 1
    for i in range(n):
        if abs(out[i]) > thr:
            first = i
            break
    for i in range(n - 1, -1, -1):
        if abs(out[i]) > thr:
            last = i
            break
    pad = int(sr * 0.15)
    first = max(0, first - pad)
    last = min(n - 1, last + pad)

    res = array.array("h")
    for v in out[first:last + 1]:
        iv = int(v * 32768)
        res.append(32767 if iv > 32767 else (-32768 if iv < -32768 else iv))

    # 6) 首尾各 10ms 淡入淡出，拼接时不会有咔哒声
    fd = min(int(sr * 0.01), len(res) // 2)
    for i in range(fd):
        g = i / float(fd)
        res[i] = int(res[i] * g)
        res[-1 - i] = int(res[-1 - i] * g)

    rms1 = math.sqrt(sum((v / 32768.0) ** 2 for v in res[::7]) / max(1, len(res[::7])))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(res.tobytes())
    return buf.getvalue(), {
        "rms_before": round(20 * math.log10(max(rms0, 1e-9)), 1),
        "rms_after": round(20 * math.log10(max(rms1, 1e-9)), 1),
        "peak_before": round(20 * math.log10(max(peak0, 1e-9)), 1),
        "gain_db": round(20 * math.log10(gain), 1),
        "trimmed": round((n - (last - first + 1)) / sr, 2),
    }


def concat_wavs(paths):
    """把多个 24k/mono/16bit wav 拼成一条，段间留 0.22s 静音"""
    frames = []
    sr = None
    for p in paths:
        with wave.open(p, "rb") as w:
            if sr is None:
                sr = w.getframerate()
            if w.getframerate() != sr or w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise ValueError("音频参数不一致，无法合并")
            frames.append(w.readframes(w.getnframes()))
    gap = b"\x00\x00" * int(sr * 0.22)
    return sr, gap.join(frames)


def safe_name(s, fallback="mimo"):
    s = re.sub(r"[（(【\[][^（(【\[）)】\]]{0,12}[）)】\]]", "", s)  # 去掉 (风格) [标签]
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", s.strip())
    return (s[:12] or fallback)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 静默，保持控制台干净

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf8")) if n else {}

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            with open(os.path.join(BASE, "index.html"), "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/api/config":
            cfg = load_config()
            key = cfg.get("api_key", "")
            masked = key[:6] + "..." + key[-4:] if len(key) > 12 else ("已设置" if key else "")
            samples = sorted(os.listdir(SAMPLES_DIR)) if os.path.isdir(SAMPLES_DIR) else []
            self._json(200, {"key_masked": masked, "has_key": bool(key),
                             "samples": samples, "voices": PRESET_VOICES})
        elif self.path == "/api/history":
            items = []
            for fn in os.listdir(OUTPUT_DIR):
                if fn.lower().endswith(".wav"):
                    p = os.path.join(OUTPUT_DIR, fn)
                    items.append({"name": fn, "size": os.path.getsize(p),
                                  "mtime": os.path.getmtime(p)})
            items.sort(key=lambda x: -x["mtime"])
            self._json(200, {"items": items[:60]})
        elif self.path.startswith("/output/"):
            fn = os.path.basename(unquote(self.path))
            p = os.path.join(OUTPUT_DIR, fn)
            if os.path.isfile(p):
                size = os.path.getsize(p)
                # 支持 Range 请求：浏览器 <audio> 要拖进度条必须能分段取数据
                rng = None
                hdr = self.headers.get("Range") or ""
                m = re.match(r"bytes=(\d*)-(\d*)", hdr.strip())
                if m and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1))
                        end = int(m.group(2)) if m.group(2) else size - 1
                    else:  # bytes=-N：取最后 N 字节
                        n = int(m.group(2))
                        start, end = max(0, size - n), size - 1
                    start, end = max(0, min(start, size - 1)), min(end, size - 1)
                    if end >= start:
                        rng = (start, end)
                if rng:
                    start, end = rng
                    with open(p, "rb") as f:
                        f.seek(start)
                        data = f.read(end - start + 1)
                    self.send_response(206)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    with open(p, "rb") as f:
                        data = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    self.wfile.write(data)
            else:
                self._json(404, {"error": "not found"})
        elif self.path == "/api/sample":
            fn = self.path  # 不会走到这，占位
            self._json(404, {})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/key":
            body = self._body()
            cfg = load_config()
            cfg["api_key"] = body.get("key", "").strip()
            save_config(cfg)
            self._json(200, {"ok": True})
        elif self.path == "/api/sample":
            body = self._body()
            name = body.get("name", "")
            b64 = body.get("data", "")
            if not re.fullmatch(r"[\w\u4e00-\u9fff\-]+(\.mp3|\.wav)", name):
                self._json(400, {"error": "样本名只能含中文、字母、数字、下划线，扩展名 mp3/wav"})
                return
            try:
                raw = base64.b64decode(b64)
            except Exception:
                self._json(400, {"error": "音频数据解析失败"})
                return
            if len(raw) > 8 * 1024 * 1024:
                self._json(400, {"error": "样本超过 8MB，请裁剪后再传（30-90 秒足够）"})
                return
            with open(os.path.join(SAMPLES_DIR, name), "wb") as f:
                f.write(raw)
            samples = sorted(os.listdir(SAMPLES_DIR))
            self._json(200, {"ok": True, "name": name, "samples": samples})
        elif self.path == "/api/delete_sample":
            body = self._body()
            name = body.get("name", "")
            if re.fullmatch(r"[\w\u4e00-\u9fff\-]+(\.mp3|\.wav)", name):
                p = os.path.join(SAMPLES_DIR, name)
                if os.path.isfile(p):
                    os.remove(p)
            self._json(200, {"ok": True, "samples": sorted(os.listdir(SAMPLES_DIR))})
        elif self.path == "/api/generate":
            t0 = time.time()
            cfg = load_config()
            key = cfg.get("api_key", "")
            if not key:
                self._json(400, {"error": "还没有配置 API Key，点右上角「密钥」填入"})
                return
            body = self._body()
            mode = body.get("mode", "preset")
            text = clean_text(body.get("text") or "")
            style = body.get("style") or ""
            if not text:
                self._json(400, {"error": "稿子是空的"})
                return
            title = safe_name(body.get("title") or text[:10])
            if mode == "clone":
                sample = body.get("sample") or ""
                if not sample:
                    self._json(400, {"error": "还没有选择音色样本"})
                    return
                p = os.path.join(SAMPLES_DIR, sample)
                if not os.path.isfile(p):
                    self._json(400, {"error": "样本文件不存在: " + sample})
                    return
                with open(p, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                mime = "audio/mpeg" if sample.lower().endswith(".mp3") else "audio/wav"
                model = "mimo-v2.5-tts-voiceclone"
                voice = "data:%s;base64,%s" % (mime, b64)
            else:
                model = "mimo-v2.5-tts"
                voice = body.get("voice") or "冰糖"
            audio, err = call_mimo(key, model, style, text, voice, prev=body.get("prev"))
            if err:
                self._json(500, {"error": err})
                return
            pcm = base64.b64decode(audio["data"])
            enh = {"applied": False}
            if body.get("enhance", True):
                try:
                    pcm, enh = enhance_pcm(pcm, strength=float(body.get("strength") or 1.0))
                    enh["applied"] = True
                except Exception as e:
                    enh = {"applied": False, "error": str(e)}
            stamp = time.strftime("%m%d_%H%M%S")
            fn = "%s_%s.wav" % (safe_name(title), stamp)
            with open(os.path.join(OUTPUT_DIR, fn), "wb") as f:
                f.write(pcm)
            self._json(200, {
                "filename": fn,
                "audio_b64": base64.b64encode(pcm).decode(),
                "transcript": audio.get("transcript") or "",
                "seconds": round(time.time() - t0, 1),
                "enhance": enh,
                "metrics": analyze_audio(pcm, len(text)),
            })
        elif self.path == "/api/delete_output":
            body = self._body()
            name = os.path.basename(body.get("name") or "")
            p = os.path.join(OUTPUT_DIR, name)
            if name and os.path.isfile(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
            self._json(200, {"ok": True})
        elif self.path == "/api/enhance_file":
            body = self._body()
            name = os.path.basename(body.get("name") or "")
            p = os.path.join(OUTPUT_DIR, name)
            if not name or not os.path.isfile(p):
                self._json(400, {"error": "文件不存在"})
                return
            with open(p, "rb") as f:
                raw = f.read()
            try:
                new, st = enhance_pcm(raw, strength=float(body.get("strength") or 1.0))
            except Exception as e:
                self._json(500, {"error": "处理失败: %s" % e})
                return
            base = re.sub(r"\.wav$", "", name, flags=re.I)
            out_name = "%s_清晰.wav" % base
            with open(os.path.join(OUTPUT_DIR, out_name), "wb") as f:
                f.write(new)
            self._json(200, {"filename": out_name, "enhance": st})
        elif self.path == "/api/concat":
            body = self._body()
            files = body.get("files") or []
            if not files:
                self._json(400, {"error": "没有可合并的文件"})
                return
            paths = []
            for fn in files:
                p = os.path.join(OUTPUT_DIR, os.path.basename(fn))
                if not os.path.isfile(p):
                    self._json(400, {"error": "文件不存在: " + fn})
                    return
                paths.append(p)
            try:
                sr, data = concat_wavs(paths)
            except Exception as e:
                self._json(500, {"error": "合并失败: %s" % e})
                return
            title = safe_name(body.get("title") or "合并")
            fn = "%s_合并_%s.wav" % (title, time.strftime("%m%d_%H%M%S"))
            with wave.open(os.path.join(OUTPUT_DIR, fn), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes(data)
            self._json(200, {"filename": fn,
                             "seconds": round(len(data) / 2 / sr, 1),
                             "count": len(paths)})
        else:
            self._json(404, {"error": "not found"})


class QuietServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    url = "http://127.0.0.1:%d" % PORT
    print("=" * 46)
    print("  MiMo 配音台已启动")
    print("  浏览器没有自动打开就手动访问:")
    print("  " + url)
    print("  关闭本窗口即退出，生成的音频在 output 文件夹")
    print("=" * 46)
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    with QuietServer(("127.0.0.1", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
