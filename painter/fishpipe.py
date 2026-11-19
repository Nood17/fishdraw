#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fishpipe — 画图唯一入口（2026-11-19 主人定）

初级提示词 → 加工（写词会话）→ 拼默认值 → 画两遍 → 输出，全走这一条路。
网页端（ui.py）与 agent（fishdraw / fishimage 工具）都只调这里，不许各写一套。

用法（脚本用，吐 JSON 状态）：
  python3 fishpipe.py run --text "中文需求" [--out 输出.png] [--no-writer] [--suffix "…"] [--tags "…"] [--denoise 0.45] [--size 832x1216] [--seed 123] [--kind 种类]
可选：--json-out 把最终结果另存一份 json 到指定路径。

留档：每次都在 ~/ai/painter/logs/<时间>-<编号>.json 落一份完整记录
      （time/text/kind/suffix/words/stages/out/bytes/ok/err），旁边存一份 .md 人看版。
"输入的时候不准阅读"：本脚本不吃任何外部文件里的旧提示词，也不读历史留档，只认这一次给的 text。

**硬规矩（2026-11-19 主人定）**
- 不准阅读绘画提示词：脚本只报各段长度，不打印 moss/tags/neg 原文；写词会话也不许去翻 library.jsonl / logs 留档。
- 不准查看画出来的画面：出图之后不许用任何看图手段打开成品，也不描述画面。
- 只能交给发图脚本返图：`fishpipe.py send --img 成品.png [--to QQ号] [--group 群号]`，只看回执看不看画面。
"""
import argparse
import json
import re
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
PAINTER_DIR = os.environ.get("PAINTER_DIR", os.path.join(HOME, "ai", "painter"))
PAINT_PY = os.environ.get("PAINT_PY", os.path.join(HOME, "ai", "paint.py"))
PYBIN = os.environ.get("PYBIN", os.path.join(HOME, "ai", "ComfyUI", ".venv", "bin", "python"))
COMFY = os.path.join(HOME, "ai", "ComfyUI")
FISHWORDS = os.environ.get("FISHWORDS_BIN", os.path.join(HOME, ".dsh", "dsh-onebot-qq", "bin", "fishwords"))
# ── 写词会话的调用路径（2026-11-19 主人定：把路径写清楚，不许动会话模型本身）────────
#   WRITER_CMD = [fishwords, <原话>]  →  fishwords 内部执行
#       $DSH_BIN --profile headless "<指令>"
#   即本机 DSH 自己的 headless 画师会话（默认模型/预设，一次性、不带 --session-id）；
#   会话没吐出 JSON 才回退到 api.deepseek.com/chat/completions（deepseek-chat）。
#   本脚本只管把原话塞给 WRITER_CMD，不碰任何会话/预设/模型配置。
WRITER_TIMEOUT = int(os.environ.get("FISHWORDS_TIMEOUT", "300"))
SUFFIX_FILE = os.path.join(PAINTER_DIR, "suffix.json")
LOGDIR = os.path.join(PAINTER_DIR, "logs")
COMFY_URL = "http://127.0.0.1:8188"
NAPCAT = os.environ.get("NAPCAT_URL", "http://127.0.0.1:3000")
NAPCAT_TOKEN = os.environ.get("NAPCAT_TOKEN", "")  # 必填：NapCat/OneBot 的 access token
WHITE = os.environ.get("FISH_QQ_DATA_DIR", os.path.join(HOME, ".dsh", "dsh-onebot-qq"))
CWHITE = os.environ.get("FISH_QQ_CONTAINER_DIR", "/app/plugin-workspace")
OWNER = int(os.environ.get("FISH_OWNER", "0"))  # 必填：站长本人 QQ；不设=所有人都不拼默认值
DEF_NEG = "lowres, bad anatomy, bad hands, text, watermark, jpeg artifacts, blurry, extra limbs"
QUALITY = "masterpiece, best quality, very aesthetic, absurdres, "


def log(msg):
    print(msg, flush=True)


# ---------- 默认值（网页端与 agent 共用同一份） ----------
# 默认值分三段：moss_prompt（第一遍自然语言）、tags（第二遍标签）、natives（负向词）
# 老数据只有一整串字符串，读的时候自动拆到对应段里。
SUFFIX_KEYS = ("moss_prompt", "tags", "natives")
NEG_HEAD_RE = re.compile(r"^\s*(ne?g\w*|负向|负面|反向)\s*[:：]\s*", re.I)


def split_legacy(text):
    """老格式（一大串）→ 三段。含「negative：」的行归 natives，其余归 moss_prompt 与 tags。"""
    out = {"moss_prompt": "", "tags": "", "natives": ""}
    for raw in (text or "").splitlines():
        line = raw.strip().strip(",，").strip()
        if not line:
            continue
        m = NEG_HEAD_RE.match(line)
        if m:
            out["natives"] = _join(out["natives"], line[m.end():])
            continue
        # 中文多半是自然语言句子 → moss；纯英文逗号串 → tags（两边都放，避免丢东西）
        if re.search(r"[\u4e00-\u9fff]", line):
            out["moss_prompt"] = _join(out["moss_prompt"], line)
        else:
            out["tags"] = _join(out["tags"], line)
    return {k: v.strip() for k, v in out.items()}


def _join(a, b):
    a, b = (a or "").strip().strip(",，").strip(), (b or "").strip().strip(",，").strip()
    if not a:
        return b
    if not b:
        return a
    return a + ", " + b


def load_suffix_struct():
    try:
        with open(SUFFIX_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {"moss_prompt": "", "tags": "", "natives": ""}
    if isinstance(d.get("parts"), dict):
        p = d["parts"]
        return {k: str(p.get(k) or "").strip() for k in SUFFIX_KEYS}
    return split_legacy(str(d.get("suffix") or ""))


def save_suffix_struct(parts):
    parts = {k: str((parts or {}).get(k) or "").strip() for k in SUFFIX_KEYS}
    tmp = SUFFIX_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"parts": parts, "t": time.time()}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SUFFIX_FILE)
    return parts


SEP = "；"


def parse_flat(given):
    """把 suffix_flat 产生的串拆回三段；不是这种格式就返回 None（交给 split_legacy）。
    只有一段（没有分隔符）也要认，否则单独设了 moss_prompt 会被当成英文标签塞进 tags。"""
    given = (given or "").strip()
    if not given:
        return {k: "" for k in SUFFIX_KEYS}
    out = {k: "" for k in SUFFIX_KEYS}
    hit = False
    for chunk in given.split(SEP):
        chunk = chunk.strip()
        for k in SUFFIX_KEYS:
            if chunk.startswith(k + "="):
                v = chunk[len(k) + 1:].strip()
                out[k] = _join(out[k], v) if v else out[k]
                hit = True
                break
    return out if hit else None


def suffix_flat(parts):
    parts = parts or {}
    return "；".join(f"{k}={v}" for k, v in ((k, parts.get(k)) for k in SUFFIX_KEYS) if v)


def load_suffix():
    """兼容老调用：返回三段拼成一行的可读串。"""
    return suffix_flat(load_suffix_struct())


def save_suffix(s):
    save_suffix_struct(split_legacy(s or ""))


# ---------- 默认值只认主人（2026-11-19 主人定） ----------
# 保存的那三段默认值（suffix.json）只有主人（FISH_OWNER）能用；
# 别的账号来画图，一律不拼默认值：不读文件、不拼、不报长度。
# cf. 非主人如果自己带了 --suffix，那是他自己这一单的词，照用（那是他的请求，不是我们的默认值）。
def suffix_allowed(user_qq):
    try:
        return int(user_qq) == OWNER
    except (TypeError, ValueError):
        return False


# ---------- 第二步：加工（写词会话） ----------
def writer(text):
    log("写词中（约 40 秒）")
    p = subprocess.run([FISHWORDS, text], capture_output=True, text=True, timeout=WRITER_TIMEOUT)
    if p.returncode != 0:
        raise RuntimeError("写词失败：" + (p.stderr or "")[-300:])
    lines = [x for x in p.stdout.strip().splitlines() if x.strip()]
    if not lines:
        raise RuntimeError("写词失败：没有输出")
    return json.loads(lines[-1])


# ---------- 出图 ----------
def comfy_alive():
    try:
        with urllib.request.urlopen(COMFY_URL + "/system_stats", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_comfy():
    if comfy_alive():
        return
    log("出图服务不在，拉起来")
    subprocess.Popen([os.path.join(COMFY, ".venv", "bin", "python"), "main.py",
                      "--listen", "127.0.0.1", "--port", "8188", "--reserve-vram", "0.6"],
                     cwd=COMFY, stdout=open("/tmp/comfy_pipe.log", "a"), stderr=subprocess.STDOUT,
                     start_new_session=True, stdin=subprocess.DEVNULL)
    for _ in range(90):
        if comfy_alive():
            log("出图服务起来了")
            return
        time.sleep(2)
    raise RuntimeError("出图服务起不来，看 /tmp/comfy_pipe.log")


def post(prompt):
    data = json.dumps({"prompt": prompt}).encode()
    req = urllib.request.Request(COMFY_URL + "/prompt", data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["prompt_id"]


def wait(pid, timeout=1800):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"{COMFY_URL}/history/{pid}", timeout=10) as r:
                h = json.loads(r.read())
        except Exception:
            h = {}
        if pid in h:
            outs = h[pid].get("outputs", {})
            for _, o in outs.items():
                for im in o.get("images", []):
                    return os.path.join(COMFY, "output", im.get("subfolder", ""), im["filename"])
            raise RuntimeError("这轮没有图出来")
        time.sleep(2)
    raise RuntimeError("等图超时")


def stage1_payload(moss, size, seed):
    w, h = (int(x) for x in size.lower().split("x"))
    return {"1": {"class_type": "UNETLoader", "inputs": {"unet_name": "moss_image_sft_int8_convrot.safetensors", "weight_dtype": "default"}},
            "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3.5_9b_w4a8_gptq.safetensors", "type": "moss_image", "device": "default"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": "flux2-vae.safetensors"}},
            "4": {"class_type": "CLIPTextEncode", "inputs": {"text": moss, "clip": ["2", 0]}},
            "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
            "6": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
            "7": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": 25, "cfg": 2.5, "sampler_name": "er_sde",
                                                       "scheduler": "simple", "denoise": 1.0, "model": ["1", 0],
                                                       "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0]}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "pipe1", "images": ["8", 0]}}}


def stage2_payload(tags, neg, denoise, seed, inp):
    return {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "Illustrious-XL-v1.0.safetensors"}},
            "2": {"class_type": "LoadImage", "inputs": {"image": inp}},
            "3": {"class_type": "VAEEncode", "inputs": {"pixels": ["2", 0], "vae": ["1", 2]}},
            "4": {"class_type": "CLIPTextEncode", "inputs": {"text": QUALITY + tags, "clip": ["1", 1]}},
            "5": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["1", 1]}},
            "6": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": 32, "cfg": 5.0, "sampler_name": "euler_ancestral",
                                                       "scheduler": "karras", "denoise": denoise, "model": ["1", 0],
                                                       "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["3", 0]}},
            "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
            "8": {"class_type": "SaveImage", "inputs": {"filename_prefix": "pipe2", "images": ["7", 0]}}}


# ---------- 留档 ----------
def archive(rec):
    os.makedirs(LOGDIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(LOGDIR, f"{stamp}-{rec['id']}")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    md = [f"# {rec['time']}  {rec['id']}", "",
          f"- 初级提示词：{rec.get('text') or '-'}",
          f"- 种类：{rec.get('kind') or '-'}",
          f"- 默认值：{rec.get('suffix') or '（无）'}",
          f"- 默认值分段：{json.dumps(rec.get('suffix_parts') or {}, ensure_ascii=False)}",
          f"- 是否过写词：{'是' if rec.get('used_writer') else '否'}",
          f"- 请求者：{rec.get('requester') or OWNER}（默认值{'已拼' if (rec.get('suffix_parts') or {}).get('moss_prompt') or (rec.get('suffix_parts') or {}).get('tags') or (rec.get('suffix_parts') or {}).get('natives') else '未拼'}）", ""]
    w = rec.get("words") or {}
    if w:
        md += ["## 加工结果", f"- moss：{w.get('moss_prompt','')}", f"- tags：{w.get('tags','')}",
               f"- negative：{w.get('negative','')}", f"- denoise：{w.get('denoise')}", f"- size：{w.get('size')}", ""]
    if rec.get("moss") or rec.get("tags") or rec.get("neg"):
        md += ["## 提示词全文（只给主人看；模型不许读）",
               "```", "[moss_prompt]", rec.get("moss") or "", "", "[tags]", rec.get("tags") or "",
               "", "[negative]", rec.get("neg") or "", "```", ""]
    md += ["## 出图", f"- 第一遍：{rec.get('stage1') or '-'}", f"- 第二遍：{rec.get('stage2') or '-'}",
           f"- 成品：{rec.get('out') or '-'}", f"- 结果：{'成了' if rec.get('ok') else '没成'}",
           f"- 备注：{rec.get('err') or '无'}"]
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    return base


# ---------- 主流程 ----------
def run(a):
    rid = time.strftime("%H%M%S") + "-" + secrets.token_hex(2)
    rec = {"id": rid, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "text": a.text.strip(),
           "kind": a.kind or "", "ok": False, "err": None, "words": None,
           "stage1": None, "stage2": None, "out": None, "used_writer": not a.no_writer}
    if not rec["text"]:
        raise RuntimeError("先写一句你要的画")
    # 默认值只给主人（2026-11-19 主人令）：非主人这单不拼任何默认值
    requester = str(getattr(a, "user", "") or OWNER)
    allow_default = suffix_allowed(requester)
    rec["requester"] = requester
    if allow_default:
        sfx_parts = load_suffix_struct()
    else:
        sfx_parts = {k: "" for k in SUFFIX_KEYS}

    if a.suffix is not None:
        # 调用方明确给了这一单的默认值：网页端（--user 不给，当主人用）、主人自己、
        # 或任何账号自己指定的词，都照用——这不是「默认值」，是这单的入参。
        given = a.suffix.strip()
        parsed = parse_flat(given)
        if parsed is not None:
            sfx_parts = parsed            # 「k=v」格式：按段覆盖（单段也认）
        elif given:
            sfx_parts = split_legacy(given)   # 老的一大串：按中英/负向猜着拆
        else:
            sfx_parts = {k: "" for k in SUFFIX_KEYS}
    elif not allow_default:
        sfx_parts = {k: "" for k in SUFFIX_KEYS}
    sfx = suffix_flat(sfx_parts)
    rec["suffix"] = sfx
    rec["suffix_parts"] = sfx_parts
    ensure_comfy()

    # 1) 加工：写词会话只吃原话
    if not a.no_writer:
        # 写词那关偶尔会岔开（给替代方案/要确认）——这单算失败，原话再递一遍，最多三轮
        w, last_err = None, None
        for attempt in range(1, 4):
            try:
                w = writer(rec["text"])
                break
            except Exception as e:
                last_err = e
                log(f"写词第 {attempt} 轮没成，原话再递一遍")
        if w is None:
            raise RuntimeError(str(last_err))
        rec["words"] = w
        moss = w.get("moss_prompt") or rec["text"]
        tags = w.get("tags") or ""
        neg = a.neg or w.get("negative") or DEF_NEG
        den = float(a.denoise or w.get("denoise") or 0.45)
        size = a.size or w.get("size") or "832x1216"
    else:
        moss, tags = rec["text"], (a.tags or rec["text"])
        neg, den, size = (a.neg or DEF_NEG), float(a.denoise or 0.45), (a.size or "832x1216")
    if a.tags:
        tags = (tags + ", " + a.tags).strip(", ")

    # 2) 拼默认值：三段各回各家（moss 进第一遍，tags 进第二遍正向，natives 进第二遍负向）
    if sfx_parts.get("moss_prompt"):
        moss = _join(moss, sfx_parts["moss_prompt"])
    if sfx_parts.get("tags"):
        tags = _join(tags, sfx_parts["tags"])
    if sfx_parts.get("natives"):
        neg = _join(neg, sfx_parts["natives"])
    if sfx_parts.get("moss_prompt") or sfx_parts.get("tags") or sfx_parts.get("natives"):
        # 只报各段字数，不吐提示词原文（2026-11-19 主人定：不准阅读绘画提示词）
        log("默认值已拼到加工结果后面（长度：" + " / ".join(
            f"{k}={len(sfx_parts.get(k) or '')}" for k in SUFFIX_KEYS) + "）")
    rec["moss"], rec["tags"], rec["neg"] = moss, tags, neg
    rec["denoise"], rec["size"] = den, size

    # 提示词去向（2026-11-19 主人令 + 黑匣令）：
    # - 全文只进留档（logs/<时间>-<编号>.json/.md），那两个文件里写了「模型不许读」；
    # - 终端不出任何词、不出任何路径：打印区只报各段字数，不给可读的东西；
    # - 原 last-prompts.txt 单文件留档已废：一个固定文件名太容易被顺手打开，
    #   改成只在留档目录里有一份（每次独立文件名，含编号，不好猜也不好撞）。
    rec["prompt_lens"] = {k: len(sfx_parts.get(k) or "") for k in SUFFIX_KEYS}
    log("提示词已封存（" + " / ".join(f"{k}={rec['prompt_lens'][k]}" for k in SUFFIX_KEYS) + " 段字数）")
    if os.environ.get("FISHPIPE_SHOW_PROMPTS") == "1":
        # 网页端独占：词只打给网页看，跟着进程的输出走，agent 不设这个变量就永远看不到
        log("PROMPT [moss_prompt] " + moss)
        log("PROMPT [tags] " + tags)
        log("PROMPT [negative] " + neg)

    seed = int(a.seed or secrets.randbelow(1 << 24))
    rec["seed"] = seed

    # 3) 画两遍
    log(f"第一遍（构图）开始 seed={seed}")
    base = wait(post(stage1_payload(moss, size, seed)))
    rec["stage1"] = base
    log("STAGE1 " + base)     # 第一遍一出来就报，网页端拿它先显示 A 图
    if getattr(a, "no_stage2", False):
        log("跳过第二遍，直接用第一遍的图")
        rec["stage2"] = None
        final = base
    else:
        indir = os.path.join(COMFY, "input")
        os.makedirs(indir, exist_ok=True)
        shutil.copy(base, os.path.join(indir, "pipe_input.png"))
        log(f"第二遍（换画风）denoise={den}")
        final = wait(post(stage2_payload(tags, neg, den, seed, "pipe_input.png")))
    rec["stage2"] = final

    # 4) 输出
    out = a.out or os.path.join(PAINTER_DIR, "out", f"fish-{rid}.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    shutil.copy(final, out)
    rec["out"] = out
    rec["bytes"] = os.path.getsize(out)
    rec["ok"] = True

    # 黑匣（2026-11-19 主人令）：留档路径只写进 rec / 留档，不打印。
    # 打印区只留「成没成、图在哪、多大」，够 agent 判断和交差，不够读词。
    rec.pop("text", None)
    rec["archive"] = True
    log(f"DONE {out} {rec['bytes']}")
    archive(rec)
    return rec


# ---------- 返图（只看回执，不看画面） ----------
def send_image(path, to=OWNER, group=""):
    """把成品拷进白名单目录再交给 NapCat 发出去；不打开、不预览、不描述画面。"""
    if not os.path.exists(path):
        raise RuntimeError("没有这张图：" + path)
    name = "fishsend-%d-%s" % (int(time.time()), os.path.basename(path))
    dst = os.path.join(WHITE, name)
    os.makedirs(WHITE, exist_ok=True)
    shutil.copy(path, dst)
    seg = {"type": "image", "data": {"file": CWHITE + "/" + name}}
    if group:
        url = NAPCAT + "/send_group_msg"
        body = {"group_id": int(group), "message": [seg]}
    else:
        url = NAPCAT + "/send_private_msg"
        body = {"user_id": int(to), "message": [seg]}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json",
                                          "Authorization": "Bearer " + NAPCAT_TOKEN})
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.load(r)
    ok = res.get("status") == "ok" or res.get("retcode") == 0
    print("SENT " + json.dumps({"ok": ok, "file": dst}, ensure_ascii=False))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="画图唯一入口：初级→加工→+默认值→画两遍→输出")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--text", required=True, help="初级提示词")
    r.add_argument("--out", default="")
    r.add_argument("--no-writer", action="store_true", help="不过写词会话，直接拿原话当词")
    r.add_argument("--suffix", default=None, help="本次使用/指定默认值")
    r.add_argument("--tags", default="", help="额外标签（追加）")
    r.add_argument("--neg", default="")
    r.add_argument("--denoise", default="")
    r.add_argument("--size", default="")
    r.add_argument("--seed", default="")
    r.add_argument("--kind", default="", help="种类标记，仅留档用")
    r.add_argument("--user", default="", help="这单是谁要的（QQ 号）；不给当主人。默认值只对主人拼")
    r.add_argument("--no-stage2", dest="no_stage2", action="store_true",
                   help="不画第二遍：直接拿第一遍的构图当成品")
    s = sub.add_parser("suffix")
    s.add_argument("--get", action="store_true")
    s.add_argument("--set", default=None, help="整体设（老格式，会拆成三段）")
    s.add_argument("--moss", default=None, help="设 moss_prompt 段")
    s.add_argument("--tags", default=None, help="设 tags 段")
    s.add_argument("--natives", default=None, help="设 natives 段")
    s.add_argument("--clear", action="store_true")
    d = sub.add_parser("send", help="只负责把成品返图出去（不看画面）")
    d.add_argument("--img", required=True, help="成品图片路径")
    d.add_argument("--to", default=str(OWNER), help="私聊 QQ 号")
    d.add_argument("--group", default="", help="要发群就填群号（填了走群发）")
    a = ap.parse_args()

    if a.cmd == "send":
        return send_image(a.img, a.to, a.group)

    if a.cmd == "suffix":
        if a.clear:
            save_suffix_struct({k: "" for k in SUFFIX_KEYS})
        elif any(x is not None for x in (a.moss, a.tags, a.natives)):
            parts = load_suffix_struct()
            for k, v in (("moss_prompt", a.moss), ("tags", a.tags), ("natives", a.natives)):
                if v is not None:
                    parts[k] = v
            save_suffix_struct(parts)
        elif a.set is not None:
            save_suffix(a.set)
        parts = load_suffix_struct()
        print(json.dumps({"parts": parts, "suffix": suffix_flat(parts)}, ensure_ascii=False))
        return 0

    try:
        rec = run(a)
        # 黑匣：RESULT 只报结果元数据，words / archive 一律不吐（2026-11-19 主人令）
        print("RESULT " + json.dumps({k: rec.get(k) for k in ("id", "out", "bytes", "ok", "stage1", "stage2")}, ensure_ascii=False))
        return 0
    except Exception as e:
        rec = {"id": time.strftime("%H%M%S"), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
               "text": getattr(a, "text", ""), "kind": getattr(a, "kind", ""), "ok": False,
               "err": f"{type(e).__name__}: {e}", "words": None}
        try:
            archive(rec)
        except Exception:
            pass
        # 失败也不带任何留档路径
        print("FAIL " + rec["err"], file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
