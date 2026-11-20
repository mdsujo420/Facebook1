import os, io, re, json, time, base64, random, shutil, subprocess, uuid, requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).parent
STATE = ROOT / "state.json"
OUT = ROOT / "out"
MUSIC = ROOT / "music"
UP = ROOT / "upload_tmp"
SOUNDS = ROOT / "sounds"
GRAPH = "https://graph.facebook.com/v21.0"
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-3.8-flash"
IMAGE_MODEL = os.getenv("IMAGE_MODEL") or "gemini-2.5-flash-image"
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = str(os.getenv("TELEGRAM_CHAT_ID", ""))
BW, BH = 1296, 2304          # base image (1.2x) for zoom headroom
LATIN_FONTS = ["/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
BN_FONTS = ["/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf"]

HELP = """কমান্ড (এক মেসেজে একাধিক লাইনও দিতে পারো):
(নিজের বানানো ভিডিও) টেলিগ্রামে ভিডিও পাঠাও, ক্যাপশনে পেজের নাম (যেমন page1) আর চাইলে আইডিয়া লেখো — বট টাইটেল ও ডিসক্রিপশন লিখে কিউতে রাখবে
/make page1 3 [আইডিয়া] — বটের নিজের বানানো ৩টা ভিডিও (১০/১৫/২০ সেকেন্ড, এলোমেলো)
/make all 2 — সব পেজের জন্য ২টা করে
/schedule page1 18:00 21:00 — রেডি ভিডিওগুলোর সময় (কাল 10:00 লিখলে আগামীকাল)
/schedule a1b2 18:00 — একটা নির্দিষ্ট ভিডিও (id দিয়ে)
/postnow a1b2 — এখনই পোস্ট
/redo a1b2 — ভিডিওটা নতুন করে বানানো
/skip a1b2 — বাদ দেওয়া
/queue — তালিকা
/style page1 <বর্ণনা> — স্টাইল প্রম্পট সেট (reset লিখলে আগেরটায় ফেরা)
/auto on|off — অনুমোদন ছাড়া অটো পোস্ট (ডিফল্ট বন্ধ)"""


TEST_NOTE = "ℹ️ টেস্ট মোড: ফেসবুক এখনো বন্ধ, তাই সময় হলেও পোস্ট হবে না। চালু করতে config.json-এ facebook_enabled: true দাও।"


# ---------- telegram ----------
def tg_api(method, **kw):
    return requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", timeout=120, **kw).json()


def redact(t):
    t = re.sub(r"(key|token|access_token)=[^&\s\"']+", r"\1=***", str(t))
    return re.sub(r"bot\d+:[\w-]+", "bot***", t)


def tg_send(msg):
    if TG_TOKEN and TG_CHAT and msg:
        try:
            tg_api("sendMessage", data={"chat_id": TG_CHAT, "text": redact(msg)[:4000]})
        except Exception:
            pass


def tg_send_video(path, caption):
    with open(path, "rb") as f:
        r = tg_api("sendVideo", data={"chat_id": TG_CHAT, "caption": caption[:1000],
                                      "supports_streaming": "true"}, files={"video": f})
    if not r.get("ok"):
        raise RuntimeError(f"telegram sendVideo: {r}")
    return r["result"]["video"]["file_id"]


def tg_download(file_id, dest):
    r = tg_api("getFile", data={"file_id": file_id})
    if not r.get("ok"):
        raise RuntimeError(f"telegram getFile: {r}")
    data = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{r['result']['file_path']}",
                        timeout=120)
    data.raise_for_status()
    Path(dest).write_bytes(data.content)


# ---------- state ----------
def load_state():
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    for k, v in {"next": 0, "recent": {}, "styles": {}, "queue": [], "auto": False,
                 "last_auto": "", "tg_offset": 0, "recent_meta": {}}.items():
        st.setdefault(k, v)
    return st


def save_state(st):
    st["last_run"] = datetime.now(timezone.utc).isoformat()
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2))


def err_text(e):
    body = getattr(getattr(e, "response", None), "text", "")[:200]
    msg = re.sub(r"\s*for url:\s*\S+", "", str(e))
    return f"{msg[:200]} {body}".strip()


# ---------- AI ----------
def gemini_parts(parts):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    r = requests.post(url, params={"key": os.environ["GEMINI_API_KEY"]}, timeout=120, json={
        "contents": [{"parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 1.0}})
    r.raise_for_status()
    return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])


def gemini(prompt):
    return gemini_parts([{"text": prompt}])


_last_poll = [0.0]


def img_pollinations(prompt, seed):
    """Pollinations (নতুন gen এন্ডপয়েন্ট)। enter.pollinations.ai-এর ফ্রি sk_ চাবি দিলে স্থিতিশীল"""
    from urllib.parse import quote
    key = os.getenv("POLLINATIONS_KEY")
    params = {"width": 768, "height": 1344, "model": "flux", "nologo": "true", "seed": seed}
    headers = {}
    if key:
        params["key"] = key
        headers["Authorization"] = f"Bearer {key}"
    last_status = None
    for _ in range(4):
        wait = 16 - (time.time() - _last_poll[0])
        if not key and wait > 0:
            time.sleep(wait)
        _last_poll[0] = time.time()
        r = requests.get(f"https://gen.pollinations.ai/image/{quote(prompt[:900])}",
                         params=params, headers=headers, timeout=180)
        if r.status_code == 429 or r.status_code >= 500:
            last_status = r.status_code
            time.sleep(20)  # ব্যস্ত/সাময়িক বন্ধ: একটু থেমে আবার
            continue
        if r.status_code in (401, 403):
            raise RuntimeError(f"pollinations: চাবি গ্রহণ হয়নি (HTTP {r.status_code}), POLLINATIONS_KEY দেখো")
        r.raise_for_status()
        if "image" not in r.headers.get("content-type", ""):
            raise RuntimeError("pollinations: ছবি আসেনি")
        return r.content
    raise RuntimeError(f"pollinations: সার্ভার ব্যস্ত বা বন্ধ (HTTP {last_status})")


def img_gemini(prompt, seed=None):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{IMAGE_MODEL}:generateContent"
    configs = [{"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "9:16"}},
               {"responseModalities": ["IMAGE"]},
               {"responseModalities": ["TEXT", "IMAGE"]}]
    last = None
    for cfg in configs:
        r = requests.post(url, params={"key": os.environ["GEMINI_API_KEY"]}, timeout=180, json={
            "contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg})
        if r.status_code == 400:
            last = r.text[:300]
            continue
        r.raise_for_status()
        for cand in r.json().get("candidates", []):
            for p in cand.get("content", {}).get("parts", []):
                d = p.get("inlineData") or p.get("inline_data")
                if d:
                    return base64.b64decode(d["data"])
        raise RuntimeError("gemini: কোনো ছবি ফেরত আসেনি")
    raise RuntimeError(f"gemini: রিকোয়েস্ট রিজেক্ট {last}")


def gen_image(prompt, seed=None):
    """IMAGE_PROVIDERS=pollinations,gemini (ক্রমে চেষ্টা করে; ডিফল্টে ফ্রি আগে)"""
    errors = []
    for name in os.getenv("IMAGE_PROVIDERS", "pollinations,gemini").split(","):
        try:
            return {"pollinations": img_pollinations, "gemini": img_gemini}[name.strip()](prompt, seed)
        except Exception as e:
            errors.append(f"{name}: {err_text(e)[:120]}")
    raise RuntimeError("ছবি তৈরি ফেল — " + " | ".join(errors))


def style_of(page, st):
    s = st["styles"].get(page["name"]) or {}
    text = s.get("style_prompt") or f"Niche: {page['niche']}\nStyle: {page['style']}"
    return text, s.get("image_style") or page.get("image_style", "")


MOODS = {"funny": 3, "emotional": 3, "cute": 2, "adventure": 1, "peaceful": 1}
MOOD_GUIDE = {
    "funny": "comedy: relatable humor, a silly mishap, and a clear punchline in the LAST scene",
    "emotional": "heartwarming and touching: care, family, friendship, a gentle reunion or a quiet brave moment; end on a soft emotional beat (nothing sad in a graphic way)",
    "cute": "pure cuteness: playful, sweet, fluffy moments",
    "adventure": "a small brave journey or discovery in nature",
    "peaceful": "calm, beautiful nature with gentle animal behavior and a soothing mood",
}
TEXT_KEYS = ("title", "on_image_text", "description", "spoken_line")


def pick_mood(name, st):
    last = [m.get("mood") for m in st["recent_meta"].get(name, [])[-2:]]
    pool = {k: v for k, v in MOODS.items() if k not in last} or MOODS
    return random.choices(list(pool), weights=list(pool.values()))[0]


def proofread(c, page):
    try:
        fixed = gemini(f"""Proofread and fix this Facebook Reel text. Language: {page.get('language', 'English')}.
Fix spelling, grammar and awkward wording; keep meaning and tone.
Rules: on_image_text max 12 words (empty is allowed); spoken_line max 8 words (empty is allowed);
description must end with 4-6 relevant hashtags; remove or correct any animal/nature fact that is not clearly true.
Return ONLY JSON with the same keys: title, on_image_text, description, spoken_line.
INPUT: {json.dumps({k: c.get(k, '') for k in TEXT_KEYS}, ensure_ascii=False)}""")
        for k in TEXT_KEYS:
            if isinstance(fixed.get(k), str):
                c[k] = fixed[k]
    except Exception:
        pass  # প্রুফরিড না হলেও আসল লেখা চলবে
    return c


def gen_content(page, st, hint=""):
    text, img_style = style_of(page, st)
    name = page["name"]
    recent = st["recent"].get(name, [])[-30:]
    meta = st["recent_meta"].get(name, [])[-8:]
    mood = pick_mood(name, st)
    speak = random.random() < page.get("speech_chance", 0.1)
    music = random.random() < page.get("music_chance", 0.3)
    secs = random.choice(page.get("durations") or [10, 15, 20])
    n = {10: 2, 15: 3, 20: 4}.get(secs, max(2, round(secs / 5)))
    variety = page.get("variety") or ("Mainly the niche subject, but roughly 1 in 3 videos should feature "
                                      "other animals, birds or nature scenes instead.")
    c = gemini(f"""You create viral short Reels for a Facebook page about animals and nature.
PAGE STYLE (follow strictly):
{text}
Variety rule: {variety}
Recent videos (subject and mood) - make this one clearly DIFFERENT in subject and story: {meta}
Do NOT repeat these earlier topics: {recent}
MOOD for this video: {mood} -> {MOOD_GUIDE[mood]}
Owner's idea hint (may be empty): {hint}
Language for title, on-image caption and description: {page.get('language', 'English')}

SAFETY AND ACCURACY: suitable for all ages; no violence, no animal harm or suffering, no human faces,
no brands, logos or copyrighted characters. Animal behavior must be realistic; any fact in the description must be true.
Speech this time: {'one very short line is allowed' if speak else 'NOT allowed, spoken_line must be an empty string'}.

The video is about {secs} seconds, made of exactly {n} scenes (one image per scene) telling a tiny story with a beginning, a middle and a satisfying last scene.

Return ONLY JSON with these keys:
"title": short catchy title,
"subject": the main animal or nature subject in 1-3 words,
"character": ONE line describing the main subject's look (colors, face, size). It is repeated in every scene so it must stay identical,
"scenes": list of exactly {n} short English prompts, one per scene: what is happening, setting, lighting, camera angle. Same look and location across scenes. Image style: {img_style}. No text or letters inside images,
"on_image_text": a short caption (max 12 words), or "" for no caption (use "" in about 1 of 3 videos),
"description": Facebook post description: 1-2 engaging lines, a question inviting comments, then 4-6 relevant hashtags,
"ambience": ONE of forest, night, rain, wind, snow, city, kitchen, none (natural sound that fits),
"spoken_line": "" or ONE very short English line (max 8 words) the animal could say near the end""")
    c = proofread(c, page)
    c["scenes"] = [x for x in c["scenes"] if str(x).strip()][:n]
    if not c["scenes"]:
        raise RuntimeError("Gemini কোনো সিন দেয়নি")
    if not speak:
        c["spoken_line"] = ""
    c["music"], c["mood"], c["seconds"] = music, mood, secs
    st["recent"][name] = (recent + [c["title"]])[-30:]
    st["recent_meta"].setdefault(name, []).append({"mood": mood, "subject": c.get("subject", "")})
    st["recent_meta"][name] = st["recent_meta"][name][-20:]
    return c


# ---------- video ----------
FPS, XFADE = 24, 0.5
SYNTH = {  # সিন্থেটিক ব্যাকআপ (আসল সাউন্ড ফাইল না থাকলে)
    "forest": "anoisesrc=color=pink:amplitude=0.5:duration=DUR,lowpass=f=700,tremolo=f=0.25:d=0.6",
    "night": "anoisesrc=color=brown:amplitude=0.4:duration=DUR,lowpass=f=400",
    "wind": "anoisesrc=color=pink:amplitude=0.5:duration=DUR,lowpass=f=500,tremolo=f=0.15:d=0.8",
    "snow": "anoisesrc=color=brown:amplitude=0.3:duration=DUR,lowpass=f=350",
    "rain": "anoisesrc=color=white:amplitude=0.25:duration=DUR,highpass=f=900,lowpass=f=7000",
    "city": "anoisesrc=color=brown:amplitude=0.4:duration=DUR,lowpass=f=300",
    "kitchen": "anoisesrc=color=brown:amplitude=0.2:duration=DUR,lowpass=f=250",
}


def load_font(text, size):
    bn = any("\u0980" <= ch <= "\u09ff" for ch in text)
    for p in (BN_FONTS if bn else []) + LATIN_FONTS:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size, layout_engine=ImageFont.Layout.RAQM)
            except Exception:
                return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def wrap(d, text, font, max_w):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=font) <= max_w:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def find_sound(folder, tag):
    if not tag or not folder.exists():
        return None
    files = [p for p in folder.iterdir()
             if p.suffix.lower() in (".mp3", ".wav", ".ogg", ".m4a") and tag.lower() in p.stem.lower()]
    return random.choice(files) if files else None


def tts(text, voice, path):
    import asyncio
    import edge_tts
    asyncio.run(edge_tts.Communicate(text, voice).save(str(path)))


def mix_audio(video, audio, voice, total):
    """ambience + (কখনো কখনো) মিউজিক + (কখনো কখনো) ছোট ইংরেজি লাইন"""
    dur = int(total)
    ins, fl = [], []

    def add(args, filt):
        n = len(fl) + 1
        ins.extend(args)
        fl.append(f"[{n}:a]{filt}[a{n}]")

    amb = (audio.get("ambience") or "").lower()
    if amb and amb != "none":
        f = find_sound(SOUNDS, amb)
        if f:
            add(["-stream_loop", "-1", "-i", str(f)],
                f"atrim=0:{dur},asetpts=PTS-STARTPTS,volume=0.7,afade=t=in:d=0.6")
        elif amb in SYNTH:
            add(["-f", "lavfi", "-i", SYNTH[amb].replace("DUR", str(dur))], "volume=0.5")
    tracks = sorted(MUSIC.glob("*.mp3")) if MUSIC.exists() else []
    if audio.get("music") and tracks:
        add(["-stream_loop", "-1", "-i", str(random.choice(tracks))],
            f"atrim=0:{dur},asetpts=PTS-STARTPTS,volume=0.22")
    line = (audio.get("spoken_line") or "").strip()
    if line:
        try:
            vp = OUT / "voice.mp3"
            tts(line, voice, vp)
            ms = int(max(1.2, dur - 5.0) * 1000)
            add(["-i", str(vp)], f"adelay={ms}|{ms},volume=1.5")
        except Exception:
            pass  # ভয়েস না হলেও ভিডিও চলবে
    if not fl:
        return video
    labels = "".join(f"[a{i}]" for i in range(1, len(fl) + 1))
    graph = ";".join(fl) + (f";{labels}amix=inputs={len(fl)}:duration=longest:normalize=0,"
                            f"afade=t=out:st={dur - 1}:d=1,atrim=0:{dur}[aout]")
    out = OUT / "final_audio.mp4"
    run(["ffmpeg", "-y", "-i", str(video), *ins, "-filter_complex", graph, "-map", "0:v",
         "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-t", str(dur),
         "-movflags", "+faststart", str(out)])
    return out


def prepare_base(img_bytes, caption, path):
    img = ImageOps.fit(Image.open(io.BytesIO(img_bytes)).convert("RGB"), (BW, BH))
    if caption.strip():
        d = ImageDraw.Draw(img)
        font = load_font(caption, 88)
        lines = wrap(d, caption, font, BW - 280)
        lh, pad = 112, 44
        top = int(BH * 0.10)
        ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(ov).rounded_rectangle((100, top, BW - 100, top + lh * len(lines) + pad * 2),
                                             radius=48, fill=(0, 0, 0, 150))
        img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
        d = ImageDraw.Draw(img)
        y = top + pad
        for ln in lines:
            d.text(((BW - d.textlength(ln, font=font)) / 2, y), ln, font=font, fill="white")
            y += lh
    img.save(path)


def motion(frames):
    step = 0.2 / frames
    kind = random.choice(["in", "out", "left", "right"])
    cy = "ih/2-(ih/zoom/2)"
    if kind == "in":
        zp = f"z='min(zoom+{step:.6f},1.2)':x='iw/2-(iw/zoom/2)':y='{cy}'"
    elif kind == "out":
        zp = f"z='if(eq(on,1),1.2,max(zoom-{step:.6f},1.0))':x='iw/2-(iw/zoom/2)':y='{cy}'"
    elif kind == "left":
        zp = f"z=1.15:x='(iw-iw/zoom)*on/{frames}':y='{cy}'"
    else:
        zp = f"z=1.15:x='(iw-iw/zoom)*(1-on/{frames})':y='{cy}'"
    return f"zoompan={zp}:d={frames}:s=1080x1920:fps={FPS},format=yuv420p"


def render(images, caption, audio=None, voice="en-US-AnaNeural", seconds=20):
    """একাধিক সিনের ছবি → প্রতিটায় আলাদা ক্যামেরা মুভমেন্ট → ক্রসফেডে জোড়া → সাউন্ড"""
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(exist_ok=True)
    n = len(images)
    frames = int(round((seconds + (n - 1) * XFADE) / n * FPS))
    clip_len = frames / FPS
    clips = []
    for i, data in enumerate(images):
        base, clip = OUT / f"base{i}.png", OUT / f"clip{i}.mp4"
        prepare_base(data, caption if i == 0 else "", base)
        run(["ffmpeg", "-y", "-i", str(base), "-vf", motion(frames), "-t", f"{clip_len:.3f}",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(clip)])
        clips.append(clip)
    silent = OUT / "video.mp4"
    if n == 1:
        shutil.copy(clips[0], silent)
    else:
        cmd, chain, prev = ["ffmpeg", "-y"], [], "[0:v]"
        for c in clips:
            cmd += ["-i", str(c)]
        for k in range(1, n):
            lab = f"[v{k}]"
            chain.append(f"{prev}[{k}:v]xfade=transition=fade:duration={XFADE}:"
                         f"offset={k * (clip_len - XFADE):.3f}{lab}")
            prev = lab
        run(cmd + ["-filter_complex", ";".join(chain), "-map", prev, "-c:v", "libx264",
                   "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                   "-movflags", "+faststart", str(silent)])
    return mix_audio(silent, audio or {}, voice, n * clip_len - (n - 1) * XFADE)


def review_images(images, scenes):
    """Gemini ভিশন দিয়ে ছবিগুলো পরীক্ষা: বিকৃত শরীর, লেখা/ওয়াটারমার্ক, মিল না থাকা, অসংগতি"""
    parts = [{"text": """You are a strict QA reviewer for AI-generated images used in animal/nature Reels.
Flag a scene if: deformed or impossible anatomy (extra/missing limbs, melted or distorted face);
any visible text, letters, logo or watermark; it does not match the intended scene; the main animal looks
like a different animal or character than in the other scenes; blurry or low quality; violent or unsafe content.
Return ONLY JSON: {"bad_scenes": [{"scene": <index>, "problem": "<short>"}]} (empty list if all are fine)."""}]
    for i, (data, sc) in enumerate(zip(images, scenes)):
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((512, 512))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        parts.append({"text": f"Scene {i} intended: {sc}"})
        parts.append({"inline_data": {"mime_type": "image/jpeg",
                                      "data": base64.b64encode(buf.getvalue()).decode()}})
    res = gemini_parts(parts)
    out = []
    for x in res.get("bad_scenes", []):
        try:
            i = int(x.get("scene"))
        except Exception:
            continue
        if 0 <= i < len(images):
            out.append((i, str(x.get("problem", ""))[:80]))
    return out


def verify_video(path, seconds):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,width,height:format=duration,size", "-of", "json", str(path)],
                       capture_output=True, text=True, check=True)
    j = json.loads(r.stdout)
    v = [x for x in j["streams"] if x["codec_type"] == "video"]
    if not v or (v[0]["width"], v[0]["height"]) != (1080, 1920):
        raise RuntimeError("ভিডিও সাইজ ঠিক নেই")
    if abs(float(j["format"]["duration"]) - seconds) > 1.5:
        raise RuntimeError("ভিডিওর দৈর্ঘ্য ঠিক নেই")
    if int(j["format"]["size"]) < 100_000:
        raise RuntimeError("ভিডিও ফাইল অস্বাভাবিক ছোট")


def build_item(page, st, hint=""):
    c = gen_content(page, st, hint)
    img_style = style_of(page, st)[1]
    seed, char, scenes = random.randint(1, 10**6), c.get("character", ""), c["scenes"]

    def make(i, sd):
        return gen_image(f"{char}. {scenes[i]}. {img_style}. Vertical 9:16, no text, no watermark.", sd)

    images = [make(i, seed) for i in range(len(scenes))]
    qa = "✅ QA পাস"
    try:
        for rnd in range(3):
            bad = review_images(images, scenes)
            if not bad:
                break
            if rnd == 2:
                qa = "⚠️ QA: " + "; ".join(f"সিন {i + 1}: {p}" for i, p in bad) + " — নিজে দেখে নিও"
                break
            for i, _ in bad:
                images[i] = make(i, seed + rnd + 1)
    except Exception:
        qa = "⚠️ QA চালানো যায়নি — নিজে ভালো করে দেখে নিও"
    audio = {"ambience": c.get("ambience", "forest"), "music": bool(c.get("music")),
             "spoken_line": c.get("spoken_line", "") if page.get("allow_speech", True) else ""}
    secs = c["seconds"]
    video = render(images, c.get("on_image_text", ""), audio, page.get("voice", "en-US-AnaNeural"), secs)
    verify_video(video, secs)
    item = {"id": uuid.uuid4().hex[:4], "page": page["name"], "title": c["title"],
            "mood": c.get("mood", ""), "subject": c.get("subject", ""), "qa": qa, "seconds": secs,
            "on_image_text": c.get("on_image_text", ""), "description": c["description"],
            "audio": audio, "time": None, "tries": 0}
    return item, video


def preview_caption(item):
    a = item.get("audio", {})
    snd = f"🔊 {a.get('ambience', '-')}" + (" · 🎵 মিউজিক" if a.get("music") else "")
    if a.get("spoken_line"):
        snd += f" · 🗣 \"{a['spoken_line']}\""
    return (f"#{item['id']} · {item['page']} · {item.get('mood', '')} · {item.get('subject', '')} · ⏱{item.get('seconds', '?')}s\n"
            f"🎬 {item['title']}\n💬 {item['on_image_text'] or '(ক্যাপশন নেই)'}\n{snd}\n"
            f"{item.get('qa', '')}\n\n{item['description']}")


def post_text(item):
    return f"{item['title']}\n\n{item['description']}"


# ---------- facebook ----------
def upload_reel(page_id, token, video, text):
    r = requests.post(f"{GRAPH}/{page_id}/video_reels",
                      data={"upload_phase": "start", "access_token": token}, timeout=60)
    r.raise_for_status()
    info = r.json()
    with open(video, "rb") as f:
        u = requests.post(info["upload_url"], data=f, timeout=600, headers={
            "Authorization": f"OAuth {token}", "offset": "0",
            "file_size": str(Path(video).stat().st_size)})
    u.raise_for_status()
    fin = requests.post(f"{GRAPH}/{page_id}/video_reels", timeout=60, data={
        "upload_phase": "finish", "video_id": info["video_id"], "video_state": "PUBLISHED",
        "description": text, "access_token": token})
    fin.raise_for_status()


def publish_item(page, item):
    OUT.mkdir(exist_ok=True)
    dest = OUT / "publish.mp4"
    tg_download(item["file_id"], dest)
    upload_reel(page["page_id"], os.environ[page["token_env"]], dest, post_text(item))


# ---------- commands ----------
def parse_times(tokens, tz):
    off, out = 0, []
    for t in tokens:
        tl = t.lower()
        if tl in ("tomorrow", "কাল"):
            off = 1
            continue
        if tl in ("today", "আজ"):
            off = 0
            continue
        h, m = map(int, t.split(":"))
        now = datetime.now(tz)
        dt = (now + timedelta(days=off)).replace(hour=h, minute=m, second=0, microsecond=0)
        if dt <= now + timedelta(minutes=2):
            dt += timedelta(days=1)
        out.append(dt)
    return out


def find(st, key):
    return next((q for q in st["queue"] if q["id"] == key.lstrip("#")), None)


def handle(line, c, st):
    line = line.strip()
    if not line:
        return ""
    if not line.startswith("/"):
        return "কমান্ড দিয়ে শুরু করো। /help দেখো।"
    parts = line.split()
    cmd = parts[0].lower().split("@")[0]
    pages = {p["name"]: p for p in c["pages"]}
    tz = ZoneInfo(c.get("timezone", "Asia/Dhaka"))

    if cmd in ("/start", "/help"):
        return HELP + "\n\nপেজ: " + ", ".join(pages)

    if cmd == "/style":
        if len(parts) < 3 or parts[1] not in pages:
            return "ব্যবহার: /style page1 স্টাইলের বর্ণনা"
        name, desc = parts[1], line.split(maxsplit=2)[2]
        if desc.strip().lower() == "reset":
            st["styles"].pop(name, None)
            return f"✅ {name} ডিফল্ট স্টাইলে ফিরেছে।"
        s = gemini(f"""From the owner's description, write a detailed style brief for a short-video page
(tone, humor type, caption style, subjects, mood) and a short image style line for AI image generation.
If the owner pasted example captions, capture their style but do not copy them.
Description: {desc}
Return ONLY JSON: {{"style_prompt": "...", "image_style": "..."}}""")
        st["styles"][name] = s
        return f"✅ {name} এর স্টাইল সেট (পরের ভিডিও থেকে):\n{s['style_prompt']}\n\nইমেজ: {s['image_style']}"

    if cmd == "/make":
        target = parts[1] if len(parts) > 1 else ""
        targets = list(pages) if target == "all" else ([target] if target in pages else [])
        if not targets:
            return "ব্যবহার: /make page1 3 অথবা /make all 2"
        rest, n = parts[2:], 1
        if rest and rest[0].isdigit():
            n, rest = min(int(rest[0]), 5), rest[1:]
        if len(targets) * n > 6:
            return "একবারে সর্বোচ্চ ৬টা ভিডিও বানানো যাবে (প্রতিটায় কয়েক মিনিট লাগে)।"
        hint, made = " ".join(rest), 0
        for name in targets:
            for _ in range(n):
                try:
                    item, video = build_item(pages[name], st, hint)
                    item["file_id"] = tg_send_video(video, preview_caption(item))
                    st["queue"].append(item)
                    made += 1
                except Exception as e:
                    tg_send(f"❌ {name} ভিডিও ফেল: {err_text(e)}")
        if made == 0:
            return "❌ কোনো ভিডিও বানানো যায়নি। ওপরের এরর মেসেজ দেখো।"
        return f"✅ {made}টা ভিডিও রেডি। সময় দিতে: /schedule page1 18:00 21:00"

    if cmd == "/schedule":
        if len(parts) < 3:
            return "ব্যবহার: /schedule page1 18:00 21:00 অথবা /schedule a1b2 কাল 18:00"
        key = parts[1].lstrip("#")
        try:
            times = parse_times(parts[2:], tz)
        except Exception:
            return "সময় ভুল। HH:MM লেখো, যেমন 18:00"
        if key in pages:
            items = [q for q in st["queue"] if q["page"] == key and not q["time"]]
        else:
            q = find(st, key)
            items = [q] if q else []
        if not items or not times:
            return "সময় দেওয়ার মতো রেডি ভিডিও পাওয়া যায়নি।"
        res = []
        for q, dt in zip(items, times):
            q["time"] = dt.isoformat()
            res.append(f"#{q['id']} ({q['page']}) → {dt.strftime('%d %b %H:%M')}")
        if len(items) > len(times):
            res.append(f"⚠️ আরও {len(items) - len(times)}টার সময় বাকি")
        return "🗓 শিডিউল:\n" + "\n".join(res) + ("" if c.get("facebook_enabled") else "\n\n" + TEST_NOTE)

    if cmd == "/postnow":
        q = find(st, parts[1]) if len(parts) > 1 else None
        if not q:
            return "এই id পাওয়া যায়নি।"
        q["time"] = datetime.now(timezone.utc).isoformat()
        return f"⏩ #{q['id']} এই রানেই পোস্ট হবে।" + ("" if c.get("facebook_enabled") else "\n\n" + TEST_NOTE)

    if cmd == "/redo":
        q = find(st, parts[1]) if len(parts) > 1 else None
        if not q:
            return "এই id পাওয়া যায়নি।"
        item, video = build_item(pages[q["page"]], st)
        item["file_id"] = tg_send_video(video, preview_caption(item))
        item["time"] = q["time"]
        st["queue"][st["queue"].index(q)] = item
        return f"🔁 #{q['id']} বদলে নতুন #{item['id']} হয়েছে।"

    if cmd == "/skip":
        q = find(st, parts[1]) if len(parts) > 1 else None
        if not q:
            return "এই id পাওয়া যায়নি।"
        st["queue"].remove(q)
        return "🗑 বাদ দেওয়া হয়েছে।"

    if cmd == "/queue":
        if not st["queue"]:
            return "জমা কিছু নেই।"
        return "\n".join(f"#{q['id']} {q['page']} | {q['title']} | "
                         f"{q['time'][:16].replace('T', ' ') if q['time'] else 'সময় বাকি'}"
                         for q in st["queue"])

    if cmd == "/auto":
        if len(parts) > 1 and parts[1] in ("on", "off"):
            st["auto"] = parts[1] == "on"
            return f"অটো পোস্ট: {'চালু' if st['auto'] else 'বন্ধ'}"
        return "ব্যবহার: /auto on অথবা /auto off"

    return "বুঝিনি। /help দেখো।"


def extract_frames(path, k=4):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, check=True).stdout.strip()
    dur, frames = float(out or 0), []
    for i in range(k):
        jpg = UP / f"f{i}.jpg"
        run(["ffmpeg", "-y", "-ss", f"{dur * (i + 0.5) / k:.2f}", "-i", str(path), "-frames:v", "1",
             "-vf", "scale=512:-1", str(jpg)])
        frames.append(jpg.read_bytes())
    return frames, dur


def handle_upload(msg, c, st):
    """নিজের বানানো ভিডিও: ফ্রেম দেখে টাইটেল-ডিসক্রিপশন লিখে কিউতে রাখে"""
    v = msg.get("video") or msg.get("document") or {}
    if v.get("file_size", 0) > 20 * 1024 * 1024:
        return "❌ ভিডিও ২০MB-র বেশি (টেলিগ্রাম বটের সীমা)। ছোট সাইজে বা কম কোয়ালিটিতে পাঠাও।"
    pages = {p["name"]: p for p in c["pages"]}
    words = (msg.get("caption") or "").split()
    if words and words[0] in pages:
        page, hint = pages[words[0]], " ".join(words[1:])
    else:
        page, hint = c["pages"][st["next"] % len(c["pages"])], " ".join(words)
        st["next"] = (st["next"] + 1) % len(c["pages"])
    shutil.rmtree(UP, ignore_errors=True)
    UP.mkdir(exist_ok=True)
    path = UP / "in.mp4"
    tg_download(v["file_id"], path)
    frames, dur = extract_frames(path)
    text, _ = style_of(page, st)
    parts = [{"text": f"""You write the Facebook Reel post for this short animal/nature video (frames shown in order).
PAGE STYLE: {text}
Language: {page.get('language', 'English')}. Owner's hint (may be empty): {hint}
Describe only what is actually visible; any fact you state must be true. Suitable for all ages.
Return ONLY JSON: {{"title": "short catchy title", "subject": "main subject, 1-3 words",
"mood": "funny|emotional|cute|adventure|peaceful",
"description": "1-2 engaging lines, a question inviting comments, then 4-6 relevant hashtags",
"problems": ["visible defects: distorted anatomy, misspelled or garbled on-screen text, watermark/logo, etc. Empty list if none"]}}"""}]
    parts += [{"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(f).decode()}}
              for f in frames]
    res = gemini_parts(parts)
    res = proofread({"title": res["title"], "description": res["description"], "on_image_text": "",
                     "spoken_line": "", "subject": res.get("subject", ""), "mood": res.get("mood", ""),
                     "problems": res.get("problems", [])}, page)
    probs = [str(x)[:60] for x in res.get("problems", []) if str(x).strip()][:3]
    item = {"id": uuid.uuid4().hex[:4], "page": page["name"], "title": res["title"],
            "mood": res.get("mood", ""), "subject": res.get("subject", ""), "seconds": round(dur),
            "qa": ("⚠️ QA: " + "; ".join(probs) + " — নিজে দেখে নিও") if probs else "✅ QA পাস",
            "on_image_text": "", "description": res["description"], "audio": {},
            "file_id": v["file_id"], "source": "upload", "time": None, "tries": 0}
    st["queue"].append(item)
    st["recent"].setdefault(page["name"], []).append(item["title"])
    shutil.rmtree(UP, ignore_errors=True)
    return (f"✅ আপলোড করা ভিডিও কিউতে ({page['name']}):\n#{item['id']} · {item['mood']} · "
            f"{item['subject']} · ⏱{item['seconds']}s\n🎬 {item['title']}\n{item['qa']}\n\n"
            f"{item['description']}\n\nসময় দিতে: /schedule {item['id']} 18:00")


def poll_telegram(c, st):
    if not TG_TOKEN:
        return
    r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                     params={"offset": st["tg_offset"], "timeout": 0}, timeout=30).json()
    for u in r.get("result", []):
        st["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != TG_CHAT:
            continue  # শুধু তোমার চ্যাট থেকে কমান্ড নেবে
        if msg.get("video") or (msg.get("document") or {}).get("mime_type", "").startswith("video/"):
            try:
                tg_send(handle_upload(msg, c, st))
            except Exception as e:
                tg_send(f"❌ ভিডিও প্রসেস ফেল: {err_text(e)}")
            save_state(st)
            continue
        replies = []
        for ln in (msg.get("text") or "").splitlines():
            try:
                replies.append(handle(ln, c, st))
            except Exception as e:
                replies.append(f"❌ কমান্ড ফেল: {err_text(e)}")
        tg_send("\n\n".join(r for r in replies if r))
        save_state(st)


# ---------- schedulers ----------
def publish_due(c, st):
    if not c.get("facebook_enabled"):
        return  # টেস্ট মোড: ফেসবুক বন্ধ
    pages = {p["name"]: p for p in c["pages"]}
    now = datetime.now(timezone.utc)
    for q in list(st["queue"]):
        if not q["time"] or datetime.fromisoformat(q["time"]) > now:
            continue
        try:
            publish_item(pages[q["page"]], q)
            st["queue"].remove(q)
            tg_send(f"✅ {q['page']}: পোস্ট হয়েছে — {q['title']}")
        except Exception as e:
            q["tries"] += 1
            if q["tries"] >= 2:
                st["queue"].remove(q)
                tg_send(f"❌ {q['page']} #{q['id']} বাদ (২ বার ফেল): {err_text(e)}")
            else:
                tg_send(f"⚠️ {q['page']} #{q['id']} ফেল, পরের রানে আবার চেষ্টা: {err_text(e)}")
        save_state(st)


def auto_post(c, st):
    if not st["auto"] or not c.get("facebook_enabled"):
        return
    every = c.get("auto_post_every_hours", 4) * 3600 - 300
    now = datetime.now(timezone.utc)
    if st["last_auto"] and (now - datetime.fromisoformat(st["last_auto"])).total_seconds() < every:
        return
    st["last_auto"] = now.isoformat()
    page = c["pages"][st["next"] % len(c["pages"])]
    st["next"] = (st["next"] + 1) % len(c["pages"])
    try:
        item, video = build_item(page, st)
        upload_reel(page["page_id"], os.environ[page["token_env"]], video, post_text(item))
        tg_send(f"✅ {page['name']}: অটো পোস্ট — {item['title']}")
    except Exception as e:
        tg_send(f"❌ {page['name']} অটো ফেল: {err_text(e)}")


def main():
    c = json.loads((ROOT / "config.json").read_text())
    for p in c["pages"]:
        p.setdefault("scenes", c.get("scenes", 4))
        p.setdefault("seconds", c.get("seconds", 20))
        p.setdefault("speech_chance", c.get("speech_chance", 0.1))
        p.setdefault("music_chance", c.get("music_chance", 0.3))
    st = load_state()
    for step in (poll_telegram, publish_due, auto_post):
        try:
            step(c, st)
        except Exception as e:
            tg_send(f"❌ {step.__name__}: {err_text(e)}")
    save_state(st)


if __name__ == "__main__":
    main()
