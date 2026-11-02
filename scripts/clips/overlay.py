"""Render a shareable clip: the recorded game view beside a clean panel of the AI's decisions and light telemetry.

Reads a run directory (trace.jsonl and recording/recording.json) and writes a 1920x1080 30 fps H.264 file. Every
number on screen comes from the run's own trace: decisions and their probabilities, option results, measured
positions, decision latency and per-call cost. Frame timing uses the recorder's file times, so alignment is
approximate, as recording.json says.

    uv run --no-project --with pillow python scripts/clips/overlay.py RUN_DIR OUT.mp4 --title ... [--cut-turns A B]

`--stills 0.3,0.75` writes PNGs at those fractions of the run beside OUT instead of the video, for quick layout
checks. Needs ffmpeg on PATH and the Windows fonts named in `F` (Segoe UI, Bahnschrift).

Examples (the clips made from FPSKit live-08 and DragonBlaze live-41):

    ... runs/fpskit/live-08 runs/clips/fpskit-staircase.mp4 --title "FPS Kit · The long staircase"         --subtitle "Goal: climb the stairs to the top" --accent "#4FD1C5" --metric height         --end-title "Made it to the top" --end-sub "{best} m up in {time} for {cost} of AI"
    ... runs/dragonblaze-upa/live-41 runs/clips/dragonblaze-level1.mp4 --title "DragonBlaze · Level 1"         --subtitle "Goal: get through the level" --accent "#FF8A4C" --metric progress --first-generation 1         --cut-turns 38 104 --end-title "Level 1 cleared" --end-sub "{time} of play · {decisions} decisions · {cost} of AI"

End-card text may use {time}, {decisions}, {cost} and {best}.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from bisect import bisect_right
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H, FPS = 1920, 1080, 30
GX, GY, GW, GH = 36, 36, 1344, 1008          # the game view, 4:3
PX = GX + GW + 40                            # panel left edge
PW = W - PX - 40                             # panel width
FONTS = Path("C:/Windows/Fonts")

BG = (9, 11, 15)
CARD = (19, 22, 29)
LINE = (35, 40, 51)
TEXT = (238, 241, 246)
MUTED = (139, 148, 166)
DIM = (92, 100, 117)
GOOD = (74, 222, 128)
BAD = (248, 113, 113)
WARN = (251, 191, 36)


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size)


F = {
    "caps": font("seguisb.ttf", 15),
    "title": font("seguisb.ttf", 34),
    "sub": font("segoeui.ttf", 18),
    "action": font("seguisb.ttf", 40),
    "body": font("segoeui.ttf", 20),
    "bodyb": font("seguisb.ttf", 20),
    "small": font("segoeui.ttf", 16),
    "num": font("bahnschrift.ttf", 46),
    "numsm": font("bahnschrift.ttf", 26),
    "card_big": font("seguibl.ttf", 76),
    "card_sub": font("segoeui.ttf", 30),
}


def ts(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def mix(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def hexrgb(value: str):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


# --- plain-English names -------------------------------------------------------------------------

FIXED = {
    "escalate": "Ask for help", "restart_play_mode": "Restart the level", "finish": "Call it done",
    "sweep": "Try every button", "probe_double_vertical_right": "Double jump, then right",
    "point.search_left": "Turn around to the left", "point.search_right": "Turn around to the right",
}
SIDES = {"+x": "right", "-x": "left", "forward": "forward", "back": "back", "left": "left", "right": "right"}


def human(cid: str, label: str) -> str:
    if cid in FIXED:
        return FIXED[cid]
    if cid == "wait" or cid.startswith("wait:"):
        return "Wait a moment"
    m = re.fullmatch(r"(hop2?|traverse)(?::(\w+)|([+-]x))", cid)
    if m:
        side = SIDES.get(m.group(2) or m.group(3), m.group(2) or m.group(3))
        if m.group(1) == "traverse":
            return {"forward": "Walk forward", "back": "Back up"}.get(side, f"Run {side}")
        return ("Double jump " if m.group(1) == "hop2" else "Jump ") + side
    if cid.startswith("retreat"):
        return "Back up for a run-up"
    if cid.startswith("move.strip"):
        if "(straight ahead)" in label:
            return "Walk forward"
        t = re.search(r"turn (left|right) ([0-9.]+)", label)
        if t:
            return f"Turn {t.group(1)} {round(float(t.group(2)))}° and walk"
    head = label.split(":")[0].split(" \u00b7 ")[0].strip() or cid
    return head[:1].upper() + head[1:]


def result_text(option_id: str, ended: str):
    if ended == "state_changed":
        return "Something changed!", GOOD
    if ended == "succeeded":
        if option_id.startswith("retreat"):
            return "Got its run-up", GOOD
        if option_id.startswith("hop"):
            return "Landed it", GOOD
        return "Worked", GOOD
    return {"blocked": ("Hit a wall", BAD), "fell": ("Fell", BAD), "interrupted": ("Changed its mind", WARN),
            "timed_out": ("Ran out of time", WARN), "rejected": ("Couldn't start", WARN)}.get(ended, (ended, MUTED))


# --- the run's timeline ---------------------------------------------------------------------------

class Run:
    def __init__(self, run: Path, first_generation: int):
        rows = [json.loads(line) for line in (run / "trace.jsonl").open(encoding="utf-8")]
        rec = json.loads((run / "recording" / "recording.json").read_text(encoding="utf-8"))
        self.frames = []
        for gen in rec["generations"][first_generation:]:
            folder = run / "recording" / "frames" / gen["frames_directory"]
            self.frames += [(ts(f["mtime_utc"]), folder / f["file"]) for f in gen["frames"]]
        self.frames.sort()
        self.frame_t = [t for t, _ in self.frames]

        labels: dict[int, dict[str, str]] = {}
        latency: dict[int, float] = {}
        self.costs = []
        self.decisions = []
        self.results = []
        self.positions = []
        self.turn_t: dict[int, float] = {}
        for r in rows:
            kind, body, turn = r["kind"], r["body"], r.get("turn")
            if kind == "choice_set":
                labels[turn] = {c["id"]: c["label"] for c in body["choices"]}
            elif kind == "s1_call":
                latency[turn] = body.get("latency_ms")
                cost = (body.get("usage") or {}).get("cost")
                if cost:
                    self.costs.append((ts(r["wall_utc"]), cost))
            elif kind == "decision":
                answer = body.get("answer") or {}
                cid = answer.get("choice_id")
                if not cid:
                    continue
                names = labels.get(turn, {})
                merged: dict[str, float] = {}
                for other, p in (answer.get("distribution") or {}).items():
                    name = human(other, names.get(other, other))
                    merged[name] = merged.get(name, 0.0) + p
                t = ts(r["wall_utc"])
                self.turn_t[turn] = t
                self.decisions.append({"t": t, "turn": turn, "id": cid, "name": human(cid, names.get(cid, cid)),
                                       "dist": merged, "latency": latency.get(turn),
                                       "planned": body.get("source") is not None or not merged})
            elif kind == "option":
                self.results.append((ts(r["wall_utc"]), body["option_id"], body["outcome"]["terminated_by"]))
            elif kind == "observation":
                pos = (body.get("public") or {}).get("controlled", {}).get("position") \
                    or ((body.get("public") or {}).get("probe") or {}).get("anchor", {}).get("position")
                if pos:
                    self.positions.append((ts(body["wall_utc"]), pos))
        self.decision_t = [d["t"] for d in self.decisions]
        self.result_t = [x[0] for x in self.results]
        self.position_t = [p[0] for p in self.positions]
        self.cost_t = [c[0] for c in self.costs]
        self.cost_cum = []
        total = 0.0
        for _, c in self.costs:
            total += c
            self.cost_cum.append(total)

    def frame_at(self, t):
        i = max(0, bisect_right(self.frame_t, t) - 1)
        return self.frames[i][1]

    def decision_at(self, t):
        i = bisect_right(self.decision_t, t) - 1
        return (self.decisions[i], i + 1) if i >= 0 else (None, 0)

    def result_at(self, t):
        i = bisect_right(self.result_t, t) - 1
        return self.results[i] if i >= 0 else None

    def cost_at(self, t):
        i = bisect_right(self.cost_t, t) - 1
        return self.cost_cum[i] if i >= 0 else 0.0

    def position_at(self, t):
        i = bisect_right(self.position_t, t)
        if i == 0:
            return self.positions[0][1]
        if i >= len(self.positions):
            return self.positions[-1][1]
        (t0, a), (t1, b) = self.positions[i - 1], self.positions[i]
        k = (t - t0) / (t1 - t0) if t1 > t0 else 0
        return [x + (y - x) * k for x, y in zip(a, b)]


# --- drawing ---------------------------------------------------------------------------------------

def fit(draw, text, base, width, smallest=24):
    size = base.size
    f = base
    while draw.textlength(text, font=f) > width and size > smallest:
        size -= 2
        f = font(Path(base.path).name, size)
    return f


class Painter:
    def __init__(self, run: Run, args):
        self.run, self.args = run, args
        self.accent = hexrgb(args.accent)
        self.mask = Image.new("L", (GW, GH), 0)
        ImageDraw.Draw(self.mask).rounded_rectangle((0, 0, GW - 1, GH - 1), radius=22, fill=255)
        self.cache: dict[Path, Image.Image] = {}
        self.bars: dict[str, float] = {}
        self.base = self.static_base()
        self.metric_hist: list[tuple[float, float]] = []
        p0 = run.positions[0][1]
        self.start_pos = p0
        xs = [p[1][0] for p in run.positions]
        self.x_lo, self.x_hi = p0[0], max(xs)

    def game(self, path: Path) -> Image.Image:
        img = self.cache.get(path)
        if img is None:
            img = Image.open(path).convert("RGB").resize((GW, GH), Image.LANCZOS)
            if len(self.cache) > 64:
                self.cache.clear()
            self.cache[path] = img
        return img

    def static_base(self):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.text((PX, 44), "AI PLAYTESTER", font=F["caps"], fill=self.accent)
        d.text((PX, 66), self.args.title, font=fit(d, self.args.title, F["title"], PW), fill=TEXT)
        d.text((PX, 112), self.args.subtitle, font=F["sub"], fill=MUTED)
        footer = "Every move is picked live by a small, fast AI model."
        d.text((PX, H - 74), footer, font=F["small"], fill=DIM)
        d.text((PX, H - 52), "No script and no map of the level.", font=F["small"], fill=DIM)
        return img

    def metric(self, t):
        pos = self.run.position_at(t)
        if self.args.metric == "height":
            value = max(0.0, pos[1] - self.start_pos[1])
            return value, f"{value:.1f} m", "HEIGHT"
        span = (self.x_hi - self.x_lo) or 1.0
        value = max(0.0, min(1.0, (pos[0] - self.x_lo) / span))
        return value, f"{value * 100:.0f}%", "DISTANCE ACROSS THE LEVEL"

    def panel(self, img, t, run_start):
        d = ImageDraw.Draw(img)
        decision, count = self.run.decision_at(t)

        # NOW card
        y = 170
        d.rounded_rectangle((PX, y, PX + PW, y + 210), radius=18, fill=CARD, outline=LINE)
        d.text((PX + 24, y + 20), "THE AI DECIDED", font=F["caps"], fill=MUTED)
        if decision:
            a = min(1.0, (t - decision["t"]) / 0.22)
            off = int((1 - a) * 10)
            name_font = fit(d, decision["name"], F["action"], PW - 48)
            d.text((PX + 24, y + 46 + off), decision["name"], font=name_font, fill=mix(CARD, TEXT, a))
            share = decision["dist"].get(decision["name"]) if not decision["planned"] else None
            key = "__chosen"
            target = share or 0.0
            self.bars[key] = self.bars.get(key, 0.0) + (target - self.bars.get(key, 0.0)) * 0.3
            bx, by, bw = PX + 24, y + 124, PW - 48
            if share is None:
                self.bars[key] = 0.0
            else:
                d.rounded_rectangle((bx, by, bx + bw, by + 8), radius=4, fill=LINE)
            if share is not None and self.bars[key] > 0.005:
                d.rounded_rectangle((bx, by, bx + max(8, int(bw * self.bars[key])), by + 8), radius=4,
                                    fill=self.accent)
            info = (f"{share * 100:.0f}% of its vote" if share else "planned during its last move")
            if decision["latency"]:
                info += f"  ·  decided in {decision['latency'] / 1000:.2f} s"
            d.text((PX + 24, y + 146), info, font=F["body"], fill=MUTED)
        else:
            d.text((PX + 24, y + 54), "Getting ready…", font=F["action"], fill=DIM)

        # also considered
        y = 404
        others = []
        if decision and not decision["planned"]:
            others = sorted(((n, p) for n, p in decision["dist"].items() if n != decision["name"]),
                            key=lambda kv: -kv[1])[:3]
        if others:
            d.text((PX, y), "ALSO CONSIDERED", font=F["caps"], fill=MUTED)
        for i, (name, p) in enumerate(others):
            ry = y + 30 + i * 46
            label_font = fit(d, name, F["body"], PW - 80, 16)
            d.text((PX, ry), name, font=label_font, fill=TEXT)
            d.text((PX + PW, ry), f"{p * 100:.0f}%", font=F["bodyb"], fill=MUTED, anchor="ra")
            key = f"o{i}"
            self.bars[key] = self.bars.get(key, 0.0) + (p - self.bars.get(key, 0.0)) * 0.3
            d.rounded_rectangle((PX, ry + 30, PX + PW, ry + 34), radius=2, fill=LINE)
            d.rounded_rectangle((PX, ry + 30, PX + max(4, int(PW * self.bars[key])), ry + 34), radius=2,
                                fill=mix(LINE, MUTED, 0.8))

        # last result
        y = 580
        res = self.run.result_at(t)
        if res and t - res[0] < 4.0:
            text, color = result_text(res[1], res[2])
            fade = min(1.0, (t - res[0]) / 0.2) * (1.0 if t - res[0] < 3.4 else (4.0 - (t - res[0])) / 0.6)
            what = human(res[1], res[1])
            chip = f"{what}: {text}"
            cw = int(d.textlength(chip, font=F["bodyb"])) + 46
            d.rounded_rectangle((PX, y, PX + min(PW, cw), y + 42), radius=21, fill=mix(BG, mix(CARD, color, 0.18), fade),
                                outline=mix(BG, color, fade * 0.7))
            d.ellipse((PX + 16, y + 16, PX + 26, y + 26), fill=mix(BG, color, fade))
            d.text((PX + 34, y + 9), chip, font=fit(d, chip, F["bodyb"], PW - 50, 14), fill=mix(BG, TEXT, fade))

        # metric + sparkline (moved up when the run has no option results to show)
        y = 650 if self.run.results else 600
        value, shown, title = self.metric(t)
        if not self.metric_hist or t > self.metric_hist[-1][0]:
            self.metric_hist.append((t, value))
        d.rounded_rectangle((PX, y, PX + PW, y + 200), radius=18, fill=CARD, outline=LINE)
        d.text((PX + 24, y + 20), title, font=F["caps"], fill=MUTED)
        d.text((PX + 24, y + 42), shown, font=F["num"], fill=TEXT)
        peak = max(v for _, v in self.metric_hist)
        if self.args.metric == "height":
            d.text((PX + PW - 24, y + 62), f"best {peak:.1f} m", font=F["body"], fill=MUTED, anchor="ra")
        sx, sy, sw, sh = PX + 24, y + 110, PW - 48, 66
        window = [(tt, v) for tt, v in self.metric_hist]
        top = max(peak, 1e-6) if self.args.metric == "height" else 1.0
        t0 = run_start
        span = max(1.0, t - t0)
        pts = [(sx + sw * (tt - t0) / span, sy + sh - sh * min(1.0, v / top)) for tt, v in window[::2]] + \
              [(sx + sw, sy + sh - sh * min(1.0, value / top))]
        if len(pts) > 1:
            d.polygon(pts + [(pts[-1][0], sy + sh), (pts[0][0], sy + sh)], fill=mix(CARD, self.accent, 0.16))
            d.line(pts, fill=self.accent, width=3, joint="curve")
            d.ellipse((pts[-1][0] - 5, pts[-1][1] - 5, pts[-1][0] + 5, pts[-1][1] + 5), fill=self.accent)

        # stats row
        y = 876 if self.run.results else 826
        elapsed = max(0, int(t - run_start))
        stats = [("TIME", f"{elapsed // 60}:{elapsed % 60:02d}"), ("DECISIONS", str(count)),
                 ("AI COST", f"${self.run.cost_at(t):.3f}")]
        cw = PW / 3
        for i, (k, v) in enumerate(stats):
            x = PX + i * cw
            d.text((x, y), k, font=F["caps"], fill=MUTED)
            d.text((x, y + 22), v, font=F["numsm"], fill=TEXT)

    def frame(self, t, run_start, game_t=None, card=None, card_a=0.0):
        img = self.base.copy()
        g = self.game(self.run.frame_at(game_t if game_t is not None else t))
        if card is not None:
            g = Image.blend(g, g.filter(ImageFilter.GaussianBlur(18)).point(lambda v: v * 0.45), card_a)
        img.paste(g, (GX, GY), self.mask)
        self.panel(img, t, run_start)
        if card is not None and card_a > 0:
            d = ImageDraw.Draw(img)
            title, sub, color = card
            cx, cy = GX + GW // 2, GY + GH // 2
            d.text((cx, cy - 36), title, font=F["card_big"], fill=mix(BG, color, card_a), anchor="mm")
            d.text((cx, cy + 40), sub, font=F["card_sub"], fill=mix(BG, TEXT, card_a), anchor="mm")
        return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--title", required=True)
    ap.add_argument("--subtitle", required=True)
    ap.add_argument("--accent", default="#4FD1C5")
    ap.add_argument("--metric", choices=("height", "progress"), default="height")
    ap.add_argument("--first-generation", type=int, default=0)
    ap.add_argument("--cut-turns", type=int, nargs=2, metavar=("AFTER", "RESUME"))
    ap.add_argument("--end-title", required=True)
    ap.add_argument("--end-sub", required=True)
    ap.add_argument("--stills", help="comma-separated fractions of the run: write PNG stills instead of the video")
    args = ap.parse_args()

    run = Run(args.run, args.first_generation)
    paint = Painter(run, args)
    start, end = run.frame_t[0], run.frame_t[-1] + 0.3
    segments = [(start, end)]
    cut_card = None
    if args.cut_turns:
        a = run.turn_t[args.cut_turns[0]] + 1.5
        b = run.turn_t[args.cut_turns[1]] - 0.4
        segments = [(start, a), (b, end)]
        jumps = sum(1 for t, oid, _ in run.results if a < t < b and oid.startswith("hop"))
        gap = int(round(b - a))
        cut_card = (f"{gap // 60} min {gap % 60} s later",
                    f"{jumps} more jumps at the same wall", TEXT)

    if args.stills:
        for frac in (float(x) for x in args.stills.split(",")):
            t = start + (end - start) * frac
            sample = start
            while sample < t:  # the sparkline's history up to this moment
                paint.metric_hist.append((sample, paint.metric(sample)[0]))
                sample += 0.25
            for k in range(10):  # let the fades and bars settle
                img = paint.frame(t + k / FPS, start)
            out = args.out.with_name(f"{args.out.stem}-{frac:.2f}.png")
            img.save(out)
            print(out, "written")
        return

    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "slow",
                           "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(args.out)],
                          stdin=subprocess.PIPE)

    def emit(img):
        ff.stdin.write(img.tobytes())

    step = 1.0 / FPS
    for index, (s0, s1) in enumerate(segments):
        t = s0
        while t < s1:
            emit(paint.frame(t, start))
            t += step
        if index == 0 and cut_card is not None:
            hold = segments[0][1]
            n = int(1.8 * FPS)
            for k in range(n):
                a = min(1.0, k / 8) if k < n - 8 else (n - k) / 8
                emit(paint.frame(hold, start, card=cut_card, card_a=a))
    last = segments[-1][1]
    _, decisions = run.decision_at(last)
    played = int(last - start)
    best = max(v for _, v in paint.metric_hist) if paint.metric_hist else 0.0
    fields = {"time": f"{played // 60}:{played % 60:02d}", "decisions": decisions,
              "cost": f"{run.cost_at(last) * 100:.1f}¢", "best": f"{best:.1f}"}
    args.end_title, args.end_sub = args.end_title.format(**fields), args.end_sub.format(**fields)
    n = int(4.0 * FPS)
    for k in range(n):
        emit(paint.frame(last, start, card=(args.end_title, args.end_sub, paint.accent), card_a=min(1.0, k / 15)))
    ff.stdin.close()
    ff.wait()
    print(args.out, "written")


if __name__ == "__main__":
    main()
