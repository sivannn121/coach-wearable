"""Cartes HTML + notifications macOS (matin visuel, soir 3-4 actions)."""

from __future__ import annotations

import html
import subprocess
import sys
from pathlib import Path

from coach import (
    BASE_DIR,
    DATA_DIR,
    compute_recommendation,
    datapoints,
    evening_actions,
    morning_actions,
)

MORNING_HTML = DATA_DIR / "morning.html"
EVENING_HTML = DATA_DIR / "evening.html"
LAUNCH_DIR = Path.home() / "Library" / "LaunchAgents"
MORNING_LABEL = "com.coachwearable.morning"
EVENING_LABEL = "com.coachwearable.evening"
MORNING_HOUR, MORNING_MINUTE = 7, 15
EVENING_HOUR, EVENING_MINUTE = 21, 30


def _esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def _plist(label, mode, hour, minute):
    python = sys.executable
    script = BASE_DIR / "coach.py"
    log = DATA_DIR / f"{mode}.log"
    err = DATA_DIR / f"{mode}.err"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>WorkingDirectory</key>
  <string>{BASE_DIR}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{script}</string>
    <string>{mode}</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>{hour}</integer>
    <key>Minute</key>
    <integer>{minute}</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>{log}</string>
  <key>StandardErrorPath</key>
  <string>{err}</string>
</dict>
</plist>
"""


def macos_notify(title, subtitle, body):
    """Bannière macOS. Le détail vit dans la carte HTML (la notif est courte)."""
    def q(text):
        return text.replace("\\", "\\\\").replace('"', '\\"')

    script = (
        f'display notification "{q(body)}" with title "{q(title)}" '
        f'subtitle "{q(subtitle)}"'
    )
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"⚠️  Notification macOS impossible ({exc})")


def open_html(path):
    subprocess.run(["open", str(path)], check=False)


def _css():
    return """
    :root {
      --bg: #0c0f14;
      --card: #161b24;
      --line: #2a3140;
      --text: #f4f1ea;
      --muted: #9aa3b5;
      --go: #3dd68c;
      --light: #f5c84c;
      --rest: #ff7a6e;
      --deep: #5b7cff;
      --rem: #c084fc;
      --lite: #7d8ba0;
      --awake: #fb923c;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "SF Pro Text", "Helvetica Neue", sans-serif;
      background:
        radial-gradient(1200px 500px at 10% -10%, #1b2744 0%, transparent 55%),
        var(--bg);
      color: var(--text);
      min-height: 100vh;
    }
    .wrap { max-width: 760px; margin: 0 auto; padding: 28px 20px 48px; }
    .kicker { color: var(--muted); letter-spacing: .14em; font-size: 11px; text-transform: uppercase; }
    h1 { font-size: 28px; margin: 6px 0 18px; font-weight: 650; }
    .badge {
      display: inline-flex; align-items: center; gap: 8px;
      padding: 8px 14px; border-radius: 999px; font-weight: 700;
      letter-spacing: .04em;
    }
    .badge.go { background: #163526; color: var(--go); }
    .badge.light { background: #3a3014; color: var(--light); }
    .badge.rest { background: #3a1816; color: var(--rest); }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 20px 0; }
    @media (max-width: 640px) { .grid { grid-template-columns: 1fr; } }
    .card {
      background: var(--card); border: 1px solid var(--line);
      border-radius: 18px; padding: 16px 16px 14px;
    }
    .card .label { color: var(--muted); font-size: 12px; margin-bottom: 4px; }
    .card .value { font-size: 26px; font-weight: 650; }
    .hint { color: var(--muted); font-size: 12px; margin-top: 8px; }
    .hint.good { color: var(--go); }
    .hint.bad { color: var(--rest); }
    .track { height: 8px; background: #0b0e13; border-radius: 99px; overflow: hidden; margin-top: 12px; }
    .fill { height: 100%; border-radius: 99px; background: linear-gradient(90deg, #5b7cff, #3dd68c); }
    .fill.bad { background: linear-gradient(90deg, #fb923c, #ff7a6e); }
    .stages { display: flex; height: 22px; border-radius: 99px; overflow: hidden; margin: 8px 0 12px; }
    .stages span { display: block; height: 100%; }
    .legend { display: flex; flex-wrap: wrap; gap: 12px; color: var(--muted); font-size: 12px; }
    .dot { width: 8px; height: 8px; border-radius: 99px; display: inline-block; margin-right: 6px; }
    ol.tips { list-style: none; padding: 0; margin: 8px 0 0; }
    ol.tips li {
      display: flex; gap: 12px; align-items: flex-start;
      padding: 12px 0; border-top: 1px solid var(--line);
    }
    .n {
      flex: 0 0 28px; height: 28px; border-radius: 50%;
      display: grid; place-items: center; font-weight: 700; font-size: 13px;
      background: #222838; color: var(--text);
    }
    .evening li { padding: 16px 0; font-size: 18px; line-height: 1.35; }
    .foot { color: var(--muted); font-size: 12px; margin-top: 22px; }
    """


def render_evening_html(rec):
    actions = evening_actions(rec)
    tonight = rec.get("tonight_bedtime") or "—"
    items = "".join(
        f'<li><span class="n">{i}</span><div>{_esc(action)}</div></li>'
        for i, action in enumerate(actions, start=1)
    )
    return f"""<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ce soir — Coach</title>
<style>{_css()}</style>
</head><body><div class="wrap">
  <div class="kicker">Briefing du soir</div>
  <h1>4 gestes avant de dormir</h1>
  <div class="card">
    <div class="label">Heure de coucher visée</div>
    <div class="value">{_esc(tonight)}</div>
    <p class="hint">Pas un diagnostic : des habitudes, pour protéger profond et REM.</p>
    <ol class="tips evening">{items}</ol>
  </div>
  <p class="foot">Notif automatique vers 21h30 · `python3 coach.py evening`</p>
</div></body></html>
"""


def render_morning_html(rec, message):
    today_points = datapoints(rec)
    level = rec["level"]
    badge_class = {"GO": "go", "LIGHT": "light", "REST": "rest"}.get(level, "light")
    level_label = {"GO": "GO — séance intense", "LIGHT": "LIGHT — modéré", "REST": "REST — récupération"}.get(
        level, level
    )
    actions = morning_actions(rec)
    tips = "".join(
        f'<li><span class="n">{i}</span><div>{_esc(action)}</div></li>'
        for i, action in enumerate(actions, start=1)
    )

    def metric(label, value, vs):
        tone = vs["tone"]
        fill_cls = "fill bad" if tone == "bad" else "fill"
        return f"""
        <div class="card">
          <div class="label">{_esc(label)}</div>
          <div class="value">{_esc(value)}</div>
          <div class="track"><div class="{fill_cls}" style="width:{vs['pct']}%"></div></div>
          <div class="hint {tone}">{_esc(vs['text'])}</div>
        </div>
        """

    deep_w = max(4, round(100 * today_points["deep_pct"] / today_points["stage_total"]))
    rem_w = max(4, round(100 * today_points["rem_pct"] / today_points["stage_total"]))
    light_w = max(4, round(100 * today_points["light_pct"] / today_points["stage_total"]))
    awake_w = max(2, 100 - deep_w - rem_w - light_w)

    return f"""<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Matin — Coach</title>
<style>{_css()}</style>
</head><body><div class="wrap">
  <div class="kicker">Briefing du matin</div>
  <h1>Voici ta nuit, en clair</h1>
  <span class="badge {badge_class}">{_esc(level_label)}</span>
  <p style="color:var(--muted);max-width:42rem">{_esc(message)}</p>
  <div class="grid">
    {metric("Durée de sommeil", today_points["hours"], today_points["hours_vs"])}
    {metric("Efficacité", today_points["efficiency"], today_points["efficiency_vs"])}
    {metric("Sommeil profond", today_points["deep"], today_points["deep_vs"])}
    {metric("REM", today_points["rem"], today_points["rem_vs"])}
    {metric("Temps éveillé", today_points["awake"], today_points["awake_vs"])}
    {metric("Heure de coucher", today_points["bed"], today_points["bed_vs"])}
  </div>
  <div class="card">
    <div class="label">Composition de la nuit</div>
    <div class="stages">
      <span style="width:{deep_w}%;background:var(--deep)"></span>
      <span style="width:{rem_w}%;background:var(--rem)"></span>
      <span style="width:{light_w}%;background:var(--lite)"></span>
      <span style="width:{awake_w}%;background:var(--awake)"></span>
    </div>
    <div class="legend">
      <span><i class="dot" style="background:var(--deep)"></i>Profond {_esc(today_points['deep_pct'])}%</span>
      <span><i class="dot" style="background:var(--rem)"></i>REM {_esc(today_points['rem_pct'])}%</span>
      <span><i class="dot" style="background:var(--lite)"></i>Léger {_esc(today_points['light_pct'])}%</span>
      <span><i class="dot" style="background:var(--awake)"></i>Éveillé {_esc(today_points['awake_pct'])}%</span>
    </div>
  </div>
  <div class="card" style="margin-top:12px">
    <div class="label">Reco du jour</div>
    <ol class="tips">{tips}</ol>
  </div>
  <p class="foot">{_esc(rec.get("reason"))} · barre à 50% = ta moyenne perso</p>
</div></body></html>
"""


def run_evening(rec):
    DATA_DIR.mkdir(exist_ok=True)
    EVENING_HTML.write_text(render_evening_html(rec), encoding="utf-8")
    actions = evening_actions(rec)
    body = " · ".join(actions[:2])
    macos_notify(
        "Coach · ce soir",
        f"{len(actions)} actions · coucher {rec.get('tonight_bedtime') or ''}",
        body[:180],
    )
    open_html(EVENING_HTML)
    print(f"🌙 Carte du soir : {EVENING_HTML}")


def run_morning(rec, message):
    DATA_DIR.mkdir(exist_ok=True)
    MORNING_HTML.write_text(render_morning_html(rec, message), encoding="utf-8")
    macos_notify(
        "Coach · ce matin",
        f"{rec['level']} · ouvre la carte pour tes chiffres",
        message.replace("\n", " ")[:180],
    )
    open_html(MORNING_HTML)
    print(f"☀️  Carte du matin : {MORNING_HTML}")


def _demo_nights():
    def night(end, asleep, deep, rem, light, awake, bed, eff):
        return {
            "start_time": None,
            "end_time": end,
            "bedtime_mins": bed,
            "minutes_asleep": asleep,
            "efficiency": eff,
            "deep": deep,
            "rem": rem,
            "light": light,
            "awake": awake,
            "deep_pct": round(100 * deep / asleep, 1),
            "rem_pct": round(100 * rem / asleep, 1),
        }

    history = [
        night(f"2026-09-{d:02d}T06:10:00Z", 450, 85, 95, 240, 18, 22 * 60 + 40, 93)
        for d in range(18, 28)
    ]
    today = night("2026-09-29T04:08:00Z", 401, 38, 62, 280, 21, 24 * 60 + 25, 98.3)
    return [today] + history


def write_preview():
    rec = compute_recommendation(_demo_nights())
    message = (
        "Cette nuit : 6.7h, profond un peu juste. Séance modérée aujourd'hui. "
        "Ce soir, avance le coucher."
    )
    MORNING_HTML.write_text(render_morning_html(rec, message), encoding="utf-8")
    EVENING_HTML.write_text(render_evening_html(rec), encoding="utf-8")
    print(f"Preview matin : {MORNING_HTML}")
    print(f"Preview soir  : {EVENING_HTML}")


def install_schedule():
    LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    jobs = [
        (MORNING_LABEL, "morning", MORNING_HOUR, MORNING_MINUTE),
        (EVENING_LABEL, "evening", EVENING_HOUR, EVENING_MINUTE),
    ]
    for label, mode, hour, minute in jobs:
        path = LAUNCH_DIR / f"{label}.plist"
        path.write_text(_plist(label, mode, hour, minute), encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(path)], check=False)
        loaded = subprocess.run(["launchctl", "load", str(path)], check=False)
        when = f"{hour:02d}h{minute:02d}"
        status = "ok" if loaded.returncode == 0 else "à charger à la main"
        print(f"📅 {mode:8} {when}  {path}  ({status})")
    print(
        "\nmacOS peut demander l'autorisation Notifications pour Script Editor / osascript."
        "\nTest immédiat : python3 coach.py morning   et   python3 coach.py evening"
        "\nStopper : launchctl unload ~/Library/LaunchAgents/com.coachwearable.morning.plist"
    )
