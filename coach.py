"""
coach.py — Bilan hebdomadaire pour Fitbit Air / Google Health API

Ce script :
1. Se connecte à la Google Health API avec ton refresh token.
2. Récupère ton sommeil, ton HRV (variabilité de la fréquence cardiaque)
   et ta fréquence cardiaque au repos des ~21 derniers jours.
3. Calcule ta "baseline" (ta moyenne personnelle sur les jours précédents)
   pour comparer la semaine en cours aux semaines passées.
4. Te pose un quizz hebdomadaire en 3 piliers (Hygiène de vie, Santé
   physique, Santé mentale), puis combine tes réponses déclarées avec ces
   données objectives pour générer un récap complet.
5. Génère les messages et actions de chaque sous-score (via l'API Claude
   si disponible, sinon des messages basés sur des règles simples).
6. Ouvre le récap dans ton navigateur et enregistre le quizz dans
   data/weekly_checkins.csv, pour affiner la baseline au fil des semaines.

Pour l'exécuter : voir les instructions données dans le chat.
"""

import csv
import datetime as dt
import json
import logging
import math
import os
import shutil
import statistics
import subprocess
import sys
import warnings
import webbrowser
from html import escape as html_escape
from pathlib import Path

import requests
from dotenv import load_dotenv

# Coupe le bruit console qui n'a rien à voir avec le programme lui-même :
# - l'avertissement urllib3 sur la version d'OpenSSL du Python système macOS
#   (purement informatif, aucun impact fonctionnel)
# - les avertissements de python-dotenv sur des lignes de .env mal formées
#   (on préfère les corriger nous-mêmes plutôt que les laisser s'imprimer ;
#   voir log_debug plus bas pour un diagnostic silencieux si besoin)
try:
    from urllib3.exceptions import NotOpenSSLWarning
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except ImportError:
    pass
logging.getLogger("dotenv.main").setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()  # charge les variables depuis le fichier .env

CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN")

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://health.googleapis.com/v4/users/me/dataTypes"

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
CHECKIN_FILE = DATA_DIR / "weekly_checkins.csv"
DEBUG_LOG_PATH = DATA_DIR / "debug.log"

LOOKBACK_DAYS = 21  # combien de jours d'historique on va chercher
BASELINE_MIN_NIGHTS = 4  # en dessous de ça, la baseline n'est pas fiable


def fail(message):
    print(f"\n❌ {message}\n")
    sys.exit(1)


DEBUG = "--debug" in sys.argv


def log_debug(message):
    """Écrit une ligne dans data/debug.log (jamais dans la console, sauf
    si --debug est passé). Sert à diagnostiquer sans polluer la sortie."""
    line = f"{dt.datetime.now().isoformat(timespec='seconds')}  {message}"
    if DEBUG:
        print(f"🔎 {line}")
    try:
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 1. Authentification
# ---------------------------------------------------------------------------

def get_access_token():
    if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
        fail(
            "Il manque GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET ou "
            "GOOGLE_REFRESH_TOKEN dans ton fichier .env. "
            "Vérifie que le fichier .env existe bien (pas seulement .env.example) "
            "et qu'il est rempli."
        )
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        fail(
            "Impossible d'obtenir un access token depuis Google. "
            f"Code {resp.status_code} : {resp.text}\n"
            "Cause fréquente : le refresh token a expiré (7 jours en mode "
            "Testing), ou ne couvre pas les scopes activité/mesures de santé. "
            "Il faut alors refaire l'autorisation dans l'OAuth Playground "
            "(avec les 3 scopes) et coller le nouveau refresh_token dans .env."
        )
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# 2. Récupération des données
# ---------------------------------------------------------------------------

def api_get(access_token, data_type, params=None):
    url = f"{API_BASE}/{data_type}/dataPoints"
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(url, headers=headers, params=params or {}, timeout=30)
    if resp.status_code != 200:
        print(
            f"⚠️  Requête {data_type} a échoué (code {resp.status_code}): "
            f"{resp.text[:300]}"
        )
        return {"dataPoints": []}
    return resp.json()


def fetch_sleep_sessions(access_token, days=LOOKBACK_DAYS):
    """Récupère les sessions de sommeil des `days` derniers jours."""
    since = (dt.datetime.utcnow() - dt.timedelta(days=days)).strftime(
        "%Y-%m-%dT00:00:00"
    )
    params = {"filter": f'sleep.interval.civil_end_time >= "{since}"'}
    data = api_get(access_token, "sleep", params)
    return data.get("dataPoints", [])


def fetch_daily_metric(access_token, data_type, days=LOOKBACK_DAYS):
    """Récupère un type de donnée journalière (HRV, FC repos...)."""
    since = (dt.datetime.utcnow() - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    filter_field = data_type.replace("-", "_")
    params = {"filter": f'{filter_field}.date >= "{since}"'}
    data = api_get(access_token, data_type, params)
    return data.get("dataPoints", [])


def parse_daily_value(point, data_type):
    """Extrait (date_str, valeur) d'un point de donnée journalier (HRV/FC repos).

    La doc Google Health API v4 donne deux formes possibles pour ces types
    "Daily" selon la version/le champ : soit un objet date {year, month, day}
    avec une valeur nommée (ex: averageHeartRateVariabilityMilliseconds,
    beatsPerMinute), soit un champ sampleTime + une valeur nommée (bpm).
    On essaie toutes les variantes connues plutôt que de parier sur une seule.
    """
    camel = "".join(
        w.capitalize() if i else w for i, w in enumerate(data_type.split("-"))
    )
    # ex: "daily-heart-rate-variability" -> "dailyHeartRateVariability"
    body = point.get(camel) or point.get(data_type) or {}

    # --- date ---
    date_val = None
    raw_date = body.get("date")
    if isinstance(raw_date, dict) and "year" in raw_date:
        date_val = f"{raw_date['year']:04d}-{raw_date['month']:02d}-{raw_date['day']:02d}"
    elif isinstance(raw_date, str):
        date_val = raw_date[:10]

    if date_val is None:
        sample_time = body.get("sampleTime") or {}
        if isinstance(sample_time, dict):
            for key in ("civilTime", "dateTime", "time"):
                if key in sample_time:
                    date_val = str(sample_time[key])[:10]
                    break
        elif isinstance(sample_time, str):
            date_val = sample_time[:10]

    if date_val is None:
        for key in ("dateTime", "day"):
            if key in body:
                date_val = str(body[key])[:10]
                break

    # --- valeur ---
    value = None
    for key in (
        "averageHeartRateVariabilityMilliseconds",
        "rmssdMilli",
        "rmssd",
        "bpm",
        "beatsPerMinute",
        "restingHeartRate",
        "value",
    ):
        if key in body and body[key] not in (None, ""):
            value = body[key]
            break
    if value is not None:
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = None

    return date_val, value


def build_daily_series(points, data_type):
    """Transforme une liste de dataPoints bruts en {date_str: valeur}."""
    series = {}
    for pt in points:
        date_val, value = parse_daily_value(pt, data_type)
        if date_val and value is not None:
            series[date_val] = value
    return series


# ---------------------------------------------------------------------------
# 3. Calcul de la baseline et du score du jour
# ---------------------------------------------------------------------------

def summarize_sleep(sessions):
    """Transforme les sessions de sommeil brutes en liste de dicts simples,
    triés du plus récent au plus ancien. Ignore les siestes (< 3h)."""
    out = []
    for pt in sessions:
        sleep = pt.get("sleep", {})
        summary = sleep.get("summary", {})
        interval = sleep.get("interval", {})
        minutes_asleep = int(summary.get("minutesAsleep", 0) or 0)
        minutes_in_period = int(summary.get("minutesInSleepPeriod", 0) or 0)
        if minutes_asleep < 180:  # on ignore les siestes courtes
            continue
        efficiency = (
            round(100 * minutes_asleep / minutes_in_period, 1)
            if minutes_in_period
            else None
        )
        stages = {
            s["type"]: int(s["minutes"])
            for s in summary.get("stagesSummary", [])
        }
        out.append(
            {
                "end_time": interval.get("endTime"),
                "minutes_asleep": minutes_asleep,
                "efficiency": efficiency,
                "deep": stages.get("DEEP", 0),
                "rem": stages.get("REM", 0),
                "light": stages.get("LIGHT", 0),
                "awake": stages.get("AWAKE", 0),
            }
        )
    out.sort(key=lambda x: x["end_time"] or "", reverse=True)
    return out


def zscore(value, values):
    """z-score de `value` par rapport à la moyenne/écart-type de `values`."""
    if value is None or len(values) < 2:
        return 0.0
    mean = statistics.mean(values)
    try:
        stdev = statistics.stdev(values)
    except statistics.StatisticsError:
        stdev = 0.0
    if stdev == 0:
        return 0.0
    return (value - mean) / stdev


def find_claude_binary():
    """Cherche l'exécutable `claude` en se fiant d'abord au PATH hérité par
    ce process Python. Si rien n'est trouvé (fréquent quand le script est
    lancé depuis le bouton Run d'un IDE, qui n'hérite pas toujours du même
    PATH que le terminal), on retente avec quelques emplacements usuels
    d'installation avant d'abandonner."""
    found = shutil.which("claude")
    if found:
        return found
    candidates = [
        Path.home() / ".claude" / "local" / "claude",
        Path.home() / ".npm-global" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/opt/homebrew/bin/claude"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


# ---------------------------------------------------------------------------
# 5. Bilan hebdomadaire
# ---------------------------------------------------------------------------

def ask_scale(question, min_v, max_v, low_label=None, high_label=None):
    """Demande une note entière entre min_v et max_v, redemande si la saisie
    est invalide, retourne None si l'utilisateur interrompt (Ctrl+D/Ctrl+C).
    Affiche des ancres (ex: "1 = calme ... 10 = épuisante") pour que la
    question soit sans ambiguïté."""
    anchors = ""
    if low_label and high_label:
        anchors = f"  [{min_v} = {low_label}  ·  {max_v} = {high_label}]"
    while True:
        try:
            print(f"{question} ({min_v}-{max_v}){anchors} ", end="", flush=True)
            raw = input().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        try:
            val = int(raw)
        except ValueError:
            print(f"   → réponds avec un nombre entier entre {min_v} et {max_v}.")
            continue
        if min_v <= val <= max_v:
            return val
        print(f"   → réponds avec un nombre entier entre {min_v} et {max_v}.")


def ask_yn(question):
    """Demande une question oui/non, retourne True/False, ou None si
    l'utilisateur interrompt (Ctrl+D/Ctrl+C)."""
    while True:
        try:
            print(f"{question} (o/n) ", end="", flush=True)
            raw = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if raw in ("o", "oui", "y", "yes"):
            return True
        if raw in ("n", "non", "no"):
            return False
        print("   → réponds par 'o' ou 'n'.")


def ask_choice(question, options):
    """Affiche une question à choix multiple numéroté (options = liste de
    (clé, label)), retourne la clé choisie, ou None si l'utilisateur
    interrompt."""
    while True:
        print(question)
        for i, (_, label) in enumerate(options, start=1):
            print(f"   {i}. {label}")
        try:
            print("Ton choix (numéro) ", end="", flush=True)
            raw = input().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        try:
            idx = int(raw)
        except ValueError:
            print("   → réponds avec le numéro correspondant.")
            continue
        if 1 <= idx <= len(options):
            return options[idx - 1][0]
        print("   → réponds avec le numéro correspondant.")


def ask_optional_float(question):
    """Demande un nombre décimal, Entrée pour passer. Retourne un float,
    None si laissé vide, ou la chaîne "INTERRUPTED" si l'utilisateur
    interrompt (Ctrl+D/Ctrl+C) — à distinguer de "pas de réponse"."""
    while True:
        try:
            print(f"{question} (Entrée pour passer) ", end="", flush=True)
            raw = input().strip().replace(",", ".")
        except (EOFError, KeyboardInterrupt):
            print()
            return "INTERRUPTED"
        if raw == "":
            return None
        try:
            return float(raw)
        except ValueError:
            print("   → réponds avec un nombre (ex: 72.5), ou laisse vide pour passer.")


def run_weekly_quiz():
    """Questionnaire hebdo, organisé en 3 piliers (Hygiène de vie, Santé
    physique, Santé mentale). Retourne un dict avec toutes les réponses, ou
    None si l'utilisateur interrompt en cours de route (rien n'est
    enregistré dans ce cas)."""
    print("\n— Hygiène de vie —")
    sport = ask_scale(
        "Sur 10, à quel point tu es satisfait de ton niveau d'activité "
        "physique cette semaine ?",
        1, 10,
        low_label="quasi aucune activité", high_label="objectif sportif atteint",
    )
    if sport is None:
        return None
    training_time = ask_choice(
        "Cette semaine, tu t'es plutôt entraîné :",
        [
            ("matin", "Le matin"),
            ("soir", "Le soir / l'après-midi"),
            ("mixte", "Un peu des deux"),
            ("aucun", "Pas vraiment entraîné"),
        ],
    )
    if training_time is None:
        return None
    regularite = ask_scale(
        "Sur 10, à quel point tes horaires de sommeil (coucher/lever) ont "
        "été réguliers cette semaine ?",
        1, 10,
        low_label="très irréguliers", high_label="toujours pareil",
    )
    if regularite is None:
        return None
    sommeil = ask_scale(
        "Sur 10, comment as-tu perçu la qualité de ton sommeil cette "
        "semaine (indépendamment des chiffres de la montre) ?",
        1, 10,
        low_label="sommeil très mauvais", high_label="sommeil excellent",
    )
    if sommeil is None:
        return None
    soiree_degradee = ask_yn(
        "As-tu eu une ou plusieurs soirées qui ont dégradé ton sommeil "
        "cette semaine (sortie, écrans tard, alcool...) ?"
    )
    if soiree_degradee is None:
        return None
    nutrition = ask_scale(
        "Sur 10, à quel point ton alimentation a été équilibrée et "
        "régulière cette semaine ?",
        1, 10,
        low_label="très déséquilibrée", high_label="parfaitement équilibrée",
    )
    if nutrition is None:
        return None
    tabac = ask_yn("As-tu fumé (cigarette, vapote...) cette semaine ?")
    if tabac is None:
        return None
    alcool = ask_yn("As-tu bu de l'alcool à plusieurs occasions cette semaine ?")
    if alcool is None:
        return None

    print("\n— Santé physique —")
    poids = ask_optional_float("Quel est ton poids actuel (en kg) ?")
    if poids == "INTERRUPTED":
        return None
    checkup_recent = ask_yn(
        "As-tu fait une visite médicale générale (check-up) dans les 12 "
        "derniers mois ?"
    )
    if checkup_recent is None:
        return None

    print("\n— Santé mentale —")
    print("(4 petites questions sur ton stress perçu cette semaine, notées de 0 à 4)")
    pss_control = ask_scale(
        "À quel point as-tu eu le sentiment de ne pas contrôler les choses "
        "importantes de ta vie ?",
        0, 4, low_label="jamais", high_label="très souvent",
    )
    if pss_control is None:
        return None
    pss_confidence = ask_scale(
        "À quel point t'es-tu senti confiant dans ta capacité à gérer tes "
        "problèmes personnels ?",
        0, 4, low_label="jamais", high_label="très souvent",
    )
    if pss_confidence is None:
        return None
    pss_going_well = ask_scale(
        "À quel point as-tu eu l'impression que les choses allaient dans "
        "le sens que tu voulais ?",
        0, 4, low_label="jamais", high_label="très souvent",
    )
    if pss_going_well is None:
        return None
    pss_overwhelmed = ask_scale(
        "À quel point les difficultés se sont-elles accumulées au point "
        "que tu avais du mal à les surmonter ?",
        0, 4, low_label="jamais", high_label="très souvent",
    )
    if pss_overwhelmed is None:
        return None
    emotions = ask_scale(
        "Sur 10, à quel point as-tu l'impression d'avoir bien géré tes "
        "émotions cette semaine ?",
        1, 10, low_label="très mal géré", high_label="très bien géré",
    )
    if emotions is None:
        return None

    return {
        "sport": sport,
        "training_time": training_time,
        "regularite": regularite,
        "sommeil": sommeil,
        "soiree_degradee": soiree_degradee,
        "nutrition": nutrition,
        "tabac": tabac,
        "alcool": alcool,
        "poids": poids,
        "checkup_recent": checkup_recent,
        "pss_control": pss_control,
        "pss_confidence": pss_confidence,
        "pss_going_well": pss_going_well,
        "pss_overwhelmed": pss_overwhelmed,
        "emotions": emotions,
    }


CHECKIN_FIELDS = [
    "date", "sport", "training_time", "regularite", "sommeil",
    "soiree_degradee", "nutrition", "tabac", "alcool", "poids",
    "checkup_recent", "pss_control", "pss_confidence", "pss_going_well",
    "pss_overwhelmed", "emotions",
]


def _migrate_checkin_file_if_needed():
    """Si data/weekly_checkins.csv existe mais a un en-tête différent du
    format actuel (ex : ancien quizz à 4 questions avant la restructuration
    en 3 piliers), on le met de côté sous un autre nom plutôt que de
    planter ou d'écrire des colonnes désalignées dessous. Rien n'est perdu :
    l'ancien fichier reste lisible sous son nom de sauvegarde."""
    if not CHECKIN_FILE.exists():
        return
    with open(CHECKIN_FILE, newline="", encoding="utf-8") as f:
        header = next(csv.reader(f), None)
    if header == CHECKIN_FIELDS:
        return
    backup = CHECKIN_FILE.with_name(CHECKIN_FILE.stem + "_ancien_format.csv")
    i = 2
    while backup.exists():
        backup = CHECKIN_FILE.with_name(f"{CHECKIN_FILE.stem}_ancien_format_{i}.csv")
        i += 1
    CHECKIN_FILE.rename(backup)
    log_debug(
        f"_migrate_checkin_file_if_needed: ancien format détecté dans "
        f"{CHECKIN_FILE.name} — renommé en {backup.name}, nouveau fichier "
        "créé avec le format actuel."
    )


def log_checkin(answers):
    _migrate_checkin_file_if_needed()
    is_new_file = not CHECKIN_FILE.exists()
    with open(CHECKIN_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new_file:
            writer.writerow(CHECKIN_FIELDS)
        writer.writerow(
            [
                dt.date.today().isoformat(),
                answers["sport"],
                answers["training_time"],
                answers["regularite"],
                answers["sommeil"],
                "oui" if answers["soiree_degradee"] else "non",
                answers["nutrition"],
                "oui" if answers["tabac"] else "non",
                "oui" if answers["alcool"] else "non",
                answers["poids"] if answers["poids"] is not None else "",
                "oui" if answers["checkup_recent"] else "non",
                answers["pss_control"],
                answers["pss_confidence"],
                answers["pss_going_well"],
                answers["pss_overwhelmed"],
                answers["emotions"],
            ]
        )


def _row_to_checkin(row):
    """Convertit une ligne brute du CSV en dict typé. Retourne None si la
    ligne ne correspond pas au format attendu (ex : ancien format de
    checkin avant la restructuration en 3 piliers)."""
    try:
        return {
            "date": row["date"],
            "sport": int(row["sport"]),
            "training_time": row.get("training_time") or "aucun",
            "regularite": int(row["regularite"]),
            "sommeil": int(row["sommeil"]),
            "soiree_degradee": row.get("soiree_degradee") == "oui",
            "nutrition": int(row["nutrition"]),
            "tabac": row.get("tabac") == "oui",
            "alcool": row.get("alcool") == "oui",
            "poids": float(row["poids"]) if row.get("poids") else None,
            "checkup_recent": row.get("checkup_recent") == "oui",
            "pss_control": int(row["pss_control"]),
            "pss_confidence": int(row["pss_confidence"]),
            "pss_going_well": int(row["pss_going_well"]),
            "pss_overwhelmed": int(row["pss_overwhelmed"]),
            "emotions": int(row["emotions"]),
        }
    except (KeyError, ValueError):
        return None


def load_last_checkin():
    """Retourne le dernier bilan hebdo enregistré (dict), ou None s'il n'y
    en a pas encore (ou si le fichier est dans l'ancien format — dans ce
    cas, supprime data/weekly_checkins.csv et relance le quizz)."""
    if not CHECKIN_FILE.exists():
        return None
    with open(CHECKIN_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None
    return _row_to_checkin(rows[-1])


def load_all_checkins():
    """Retourne tous les bilans hebdo valides enregistrés, du plus ancien
    au plus récent. Sert aux analyses sur plusieurs semaines (suivi de
    poids, détection d'habitudes)."""
    if not CHECKIN_FILE.exists():
        return []
    with open(CHECKIN_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [c for c in (_row_to_checkin(r) for r in rows) if c is not None]


def pss4_composite(checkin):
    """Score composite inspiré du PSS-4 (0-16, plus haut = plus de stress
    perçu). 2 items sont inversés (confiance, les choses vont dans le bon
    sens) car un score élevé sur ces items reflète MOINS de stress."""
    return (
        checkin["pss_control"]
        + (4 - checkin["pss_confidence"])
        + (4 - checkin["pss_going_well"])
        + checkin["pss_overwhelmed"]
    )


def assess_cardiac_anomaly(hrv_by_date, rhr_by_date):
    """Signal DISTINCT de la tendance de récupération utilisée pour le
    score d'activité / le GO-LIGHT-REST quotidien : ici on cherche une FC
    repos durablement élevée sur PLUSIEURS jours cette semaine (pas juste
    une moyenne faussée par une mauvaise nuit isolée), ce qui peut
    justifier d'aller voir un médecin. On regarde chaque jour des 7
    derniers individuellement pour distinguer "ponctuel" de "durable"."""
    dates_sorted = sorted(rhr_by_date.keys())
    if not dates_sorted:
        return {"status": "inconnu", "detail": "Pas de données de FC repos disponibles cette semaine.", "days_elevated": 0}

    last7_dates = dates_sorted[-7:]
    prior_vals = [rhr_by_date[d] for d in dates_sorted if d not in last7_dates]

    if len(prior_vals) < BASELINE_MIN_NIGHTS:
        return {"status": "inconnu", "detail": "Pas encore assez d'historique de FC repos pour comparer.", "days_elevated": 0}

    daily_z = [zscore(rhr_by_date[d], prior_vals) for d in last7_dates]
    days_elevated = sum(1 for z in daily_z if z >= 0.75)
    avg_z = statistics.mean(daily_z)

    if days_elevated >= 3 and avg_z >= 1.0:
        status = "consulter"
        detail = (
            f"FC repos au-dessus de ta moyenne habituelle sur {days_elevated} "
            "jour(s) cette semaine (pas juste une mauvaise nuit isolée), sans "
            "cause évidente dans tes données."
        )
    elif abs(avg_z) < 0.3:
        status = "stable"
        detail = "FC repos stable par rapport à tes semaines précédentes."
    else:
        status = "a_surveiller"
        detail = "Légère variation de FC repos cette semaine, rien d'alarmant pour l'instant."

    return {"status": status, "detail": detail, "days_elevated": days_elevated, "avg_z": avg_z}


def analyze_weight_trend(prior_checkins, current_weight):
    """Tendance de poids purement descriptive (aucun jugement, aucune
    notion de 'bon'/'mauvais') : compare le poids déclaré cette semaine à
    la dernière valeur enregistrée."""
    if current_weight is None:
        return {"status": "non_renseigne", "detail": "Poids non renseigné cette semaine.", "delta": None}
    history = [c["poids"] for c in prior_checkins if c.get("poids") is not None]
    if not history:
        return {"status": "premiere_mesure", "detail": f"Premier poids enregistré : {current_weight:g} kg.", "delta": None}
    previous = history[-1]
    delta = round(current_weight - previous, 1)
    if abs(delta) < 0.3:
        status = "stable"
    elif delta > 0:
        status = "hausse"
    else:
        status = "baisse"
    sign = "+" if delta >= 0 else ""
    return {"status": status, "detail": f"{current_weight:g} kg ({sign}{delta:g} kg vs dernière mesure).", "delta": delta}


def detect_training_time_pattern(prior_checkins):
    """Cherche si un moment d'entraînement (matin/soir) est associé à un
    meilleur sommeil ressenti sur l'historique disponible. Seuil
    volontairement prudent (3 semaines de chaque côté minimum) pour ne pas
    inventer un pattern à partir de bruit — sur un pilote de 2 semaines ça
    ne se déclenchera probablement pas encore, et c'est voulu."""
    morning = [c["sommeil"] for c in prior_checkins if c.get("training_time") == "matin"]
    evening = [c["sommeil"] for c in prior_checkins if c.get("training_time") == "soir"]
    if len(morning) < 3 or len(evening) < 3:
        return None
    avg_morning = statistics.mean(morning)
    avg_evening = statistics.mean(evening)
    if avg_morning - avg_evening >= 1.5:
        return "Sur tes dernières semaines, ton sommeil est mieux noté les semaines où tu t'entraînes plutôt le matin."
    if avg_evening - avg_morning >= 1.5:
        return "Sur tes dernières semaines, ton sommeil est mieux noté les semaines où tu t'entraînes plutôt le soir."
    return None


def analyze_week(nights, hrv_by_date, rhr_by_date, checkin, prior_checkins):
    """Partie 100% déterministe : calcule tous les signaux objectifs et
    déclaratifs de la semaine (les 3 piliers). Ne décide d'aucun texte —
    seulement des chiffres/statuts, utilisés ensuite pour écrire les
    messages (par règles ou par Claude)."""
    last7 = nights[:7]
    prior = nights[7:]

    last7_dates = {n["end_time"][:10] for n in last7 if n["end_time"]}
    prior_dates_set = {n["end_time"][:10] for n in prior if n["end_time"]}

    avg_sleep_week = (
        statistics.mean([n["minutes_asleep"] / 60 for n in last7]) if last7 else None
    )
    prior_sleep_vals = [n["minutes_asleep"] for n in prior]
    sleep_z = None
    if last7 and len(prior_sleep_vals) >= 4:
        sleep_z = zscore(statistics.mean([n["minutes_asleep"] for n in last7]), prior_sleep_vals)

    hrv_week_vals = [v for d, v in hrv_by_date.items() if d in last7_dates]
    hrv_prior_vals = [v for d, v in hrv_by_date.items() if d in prior_dates_set]
    hrv_z = (
        zscore(statistics.mean(hrv_week_vals), hrv_prior_vals)
        if hrv_week_vals and len(hrv_prior_vals) >= 4
        else None
    )

    rhr_week_vals = [v for d, v in rhr_by_date.items() if d in last7_dates]
    rhr_prior_vals = [v for d, v in rhr_by_date.items() if d in prior_dates_set]
    rhr_z = (
        zscore(statistics.mean(rhr_week_vals), rhr_prior_vals)
        if rhr_week_vals and len(rhr_prior_vals) >= 4
        else None
    )

    recovery_parts = [z for z in (hrv_z, -rhr_z if rhr_z is not None else None) if z is not None]
    recovery_trend = statistics.mean(recovery_parts) if recovery_parts else None

    cardiac = assess_cardiac_anomaly(hrv_by_date, rhr_by_date)
    weight_trend = analyze_weight_trend(prior_checkins, checkin.get("poids"))
    habit_insight = detect_training_time_pattern(prior_checkins)

    pss_raw = pss4_composite(checkin)
    stress_declared = pss_raw / 16 * 10  # ramené sur l'échelle 0-10 habituelle

    return {
        "avg_sleep_week": avg_sleep_week,
        "sleep_z": sleep_z,
        "hrv_z": hrv_z,
        "rhr_z": rhr_z,
        "recovery_trend": recovery_trend,
        "cardiac": cardiac,
        "weight_trend": weight_trend,
        "habit_insight": habit_insight,
        "sport_rating": checkin["sport"],
        "training_time": checkin["training_time"],
        "regularite_rating": checkin["regularite"],
        "sommeil_rating": checkin["sommeil"],
        "soiree_degradee": checkin["soiree_degradee"],
        "nutrition_rating": checkin["nutrition"],
        "tabac": checkin["tabac"],
        "alcool": checkin["alcool"],
        "checkup_recent": checkin["checkup_recent"],
        "pss_raw": pss_raw,
        "stress_declared": stress_declared,
        "emotions_rating": checkin["emotions"],
    }


def blend_rating(declared, z, invert=False, weight_declared=0.65):
    """Combine la note déclarée (0-10) avec un signal objectif (z-score vs
    baseline des semaines précédentes) en une note pondérée 0-10 affichée
    sur le récap. Le z-score est d'abord converti sur une échelle 0-10 où
    plus haut = mieux (z=0 -> 5, pente de 2.5 pts par unité, plafonnée à
    [0,10]). `invert=True` sert pour le stress : un meilleur signal
    physiologique (HRV en hausse) doit faire BAISSER la note de charge
    affichée, pas l'augmenter. Sans signal dispo (historique insuffisant),
    on garde simplement la note déclarée."""
    if z is None:
        return round(declared)
    objective_score = max(0.0, min(10.0, 5 + z * 2.5))
    if invert:
        objective_score = 10 - objective_score
    blended = weight_declared * declared + (1 - weight_declared) * objective_score
    return round(max(0.0, min(10.0, blended)))


def should_suggest_psy(analysis):
    """Décision 100% déterministe (jamais laissée à l'IA) : suggérer d'en
    parler à un professionnel si le stress perçu (PSS-4) est TRÈS élevé
    cette semaine. Seuil volontairement haut pour ne pas se déclencher sur
    une semaine ordinaire."""
    return analysis["pss_raw"] >= 13  # sur 16


def compute_all_ratings(analysis):
    """Notes affichées (0-10, haut = bon partout) pour chaque sous-score à
    jauge. Pondération 65% ressenti déclaré / 35% donnée objective quand
    elle est disponible. Régularité/nutrition/émotions n'ont pas de
    donnée objective (rien d'équivalent dans l'API) donc elles restent la
    note déclarée telle quelle.

    Pour le stress (PSS-4), comme pour l'ancienne catégorie "Vie perso", on
    calcule la charge pondérée normalement puis on AFFICHE son inverse
    (sérénité = 10 - charge) pour que chiffre/jauge/mascotte restent
    cohérents avec la convention "haut = bon" utilisée partout ailleurs."""
    weighted_charge = blend_rating(analysis["stress_declared"], analysis["hrv_z"], invert=True)
    serenity_display = round(max(0.0, min(10.0, 10 - weighted_charge)))

    cardiac_status_score = {
        "stable": 9, "a_surveiller": 6, "consulter": 2, "inconnu": 5,
    }[analysis["cardiac"]["status"]]

    return {
        "activite": blend_rating(analysis["sport_rating"], analysis["recovery_trend"]),
        "sommeil": blend_rating(analysis["sommeil_rating"], analysis["sleep_z"]),
        "regularite": round(analysis["regularite_rating"]),
        "nutrition": round(analysis["nutrition_rating"]),
        "cardio": cardiac_status_score,
        "stress": serenity_display,
        "emotions": round(analysis["emotions_rating"]),
    }


WEEKLY_CONTENT_KEYS = [
    "activite", "sommeil", "regularite", "nutrition",
    "cardio", "poids", "checkup", "stress", "emotions",
]


def hygiene_insight_chips(analysis):
    """Petites puces factuelles (pas de jauge/mascotte — un smiley pour
    "tu as fumé" enverrait un message bizarre) affichées sous la section
    Hygiène de vie."""
    chips = []
    if analysis["tabac"]:
        chips.append("Tabac cette semaine")
    if analysis["alcool"]:
        chips.append("Alcool à plusieurs reprises")
    if analysis["soiree_degradee"]:
        chips.append("Sommeil pénalisé par une soirée")
    if analysis["habit_insight"]:
        chips.append(analysis["habit_insight"])
    return chips


def rule_based_weekly_content(analysis):
    """Contenu de secours déterministe (message + 1-2 actions par
    sous-score), utilisé si Claude n'est pas disponible."""
    content = {}

    # --- Activité ---
    recovery_trend = analysis["recovery_trend"]
    sport_rating = analysis["sport_rating"]
    if sport_rating <= 3:
        msg = "Pas assez d'activité physique cette semaine par rapport à ce que tu vises."
        actions = [
            "Bloque 2 créneaux fixes dans ton agenda cette semaine",
            "Commence petit : 20-30 min suffisent pour relancer une routine",
        ]
    elif sport_rating >= 9 and recovery_trend is not None and recovery_trend <= -0.3:
        msg = "Grosse semaine sportive, peut-être même trop vu ta récupération (HRV/FC repos) en baisse."
        actions = [
            "Remplace une séance intense par une séance légère cette semaine",
            "Surveille ton HRV/FC repos avant de repartir fort",
        ]
    elif recovery_trend is not None and sport_rating <= 5 and recovery_trend >= 0.2:
        msg = "Activité plutôt légère cette semaine alors que ta récupération est bonne : tu as de la marge pour reprendre plus fort."
        actions = ["Profite de ta bonne récupération pour une séance plus intense cette semaine"]
    else:
        msg = "Bon niveau d'activité cette semaine, dans une quantité qui te convient."
        actions = ["Garde ce rythme la semaine prochaine"]
    content["activite"] = {"message": msg, "actions": actions}

    # --- Sommeil ---
    avg_sleep_week = analysis["avg_sleep_week"]
    hours_txt = (
        f"{avg_sleep_week:.1f}h de moyenne mesurée cette semaine"
        if avg_sleep_week is not None else "pas assez de données de sommeil cette semaine"
    )
    if analysis["soiree_degradee"] and analysis["sommeil_rating"] <= 5:
        msg = f"Sommeil pénalisé cette semaine, probablement lié à la soirée que tu as signalée ({hours_txt})."
        actions = ["La prochaine fois, vise un coucher proche de ton horaire habituel même après une soirée"]
    elif analysis["sommeil_rating"] <= 4:
        msg = f"Sommeil difficile cette semaine ({hours_txt})."
        actions = ["Couche-toi 30 min plus tôt, à heure fixe, dès ce soir", "Coupe les écrans 30 min avant le coucher"]
    elif analysis["sommeil_rating"] >= 7:
        msg = f"Bon sommeil cette semaine ({hours_txt})."
        actions = ["Garde ton horaire de coucher actuel, y compris le week-end"]
    else:
        msg = f"Sommeil correct cette semaine ({hours_txt})."
        actions = ["Vise une heure de coucher régulière cette semaine"]
    content["sommeil"] = {"message": msg, "actions": actions}

    # --- Régularité ---
    if analysis["regularite_rating"] <= 4:
        msg = "Horaires de sommeil irréguliers cette semaine — ça joue probablement sur ta forme générale."
        actions = ["Fixe une heure de coucher cible et tiens-la 5 jours sur 7"]
    else:
        msg = "Horaires plutôt réguliers cette semaine, c'est une bonne base à garder."
        actions = ["Garde ce rythme, y compris le week-end"]
    content["regularite"] = {"message": msg, "actions": actions}

    # --- Nutrition ---
    if analysis["nutrition_rating"] <= 4:
        msg = "Alimentation en dessous de ce que tu voudrais cette semaine."
        actions = ["Prépare 2-3 repas à l'avance ce week-end", "Ajoute une portion de légumes à un repas par jour"]
    elif analysis["nutrition_rating"] >= 8:
        msg = "Bonne semaine côté alimentation d'après toi. Continue sur cette régularité."
        actions = ["Continue sur cette régularité la semaine prochaine"]
    else:
        msg = "Alimentation correcte cette semaine, sans plus."
        actions = ["Repère un repas de la semaine à améliorer en priorité"]
    content["nutrition"] = {"message": msg, "actions": actions}

    # --- Cardio (Santé physique) ---
    cardiac = analysis["cardiac"]
    if cardiac["status"] == "consulter":
        msg = cardiac["detail"] + " Ce n'est pas un diagnostic, mais ça vaut le coup d'en parler à un médecin."
        actions = ["Prends rendez-vous avec ton médecin pour en parler", "Note si un symptôme particulier accompagne ça (fatigue, fièvre...)"]
    elif cardiac["status"] == "stable":
        msg = cardiac["detail"]
        actions = ["Rien à faire de particulier, continue comme ça"]
    elif cardiac["status"] == "a_surveiller":
        msg = cardiac["detail"]
        actions = ["Garde un œil dessus la semaine prochaine"]
    else:
        msg = "Pas encore assez de données de FC repos pour analyser ça finement."
        actions = ["Continue à porter ta montre régulièrement pour affiner ce suivi"]
    content["cardio"] = {"message": msg, "actions": actions}

    # --- Poids ---
    weight_trend = analysis["weight_trend"]
    if weight_trend["status"] == "non_renseigne":
        msg = "Poids non renseigné cette semaine."
        actions = ["Renseigne ton poids la semaine prochaine pour démarrer un suivi"]
    else:
        msg = f"Suivi de poids : {weight_trend['detail']}"
        actions = ["Pèse-toi dans les mêmes conditions chaque semaine pour un suivi fiable"]
    content["poids"] = {"message": msg, "actions": actions}

    # --- Check-up médical ---
    if analysis["checkup_recent"]:
        msg = "Check-up médical à jour (moins de 12 mois). Rien à faire de ce côté."
        actions = ["Continue sur ce rythme de suivi médical"]
    else:
        msg = "Pas de check-up médical général depuis plus de 12 mois."
        actions = ["Prends rendez-vous pour un bilan de santé général dans les prochaines semaines"]
    content["checkup"] = {"message": msg, "actions": actions}

    # --- Stress perçu (PSS-4) ---
    if should_suggest_psy(analysis):
        msg = (
            "Stress perçu très élevé cette semaine d'après tes réponses. Ce "
            "n'est pas un diagnostic, mais si cette sensation persiste dans "
            "la durée, en parler à un professionnel (psychologue, médecin, "
            "service de santé universitaire) peut vraiment aider."
        )
        actions = [
            "Parle de ce que tu ressens à quelqu'un de confiance cette semaine",
            "Si ça persiste, prends rendez-vous avec un psychologue ou le service de santé universitaire",
        ]
    elif analysis["pss_raw"] <= 4:
        msg = "Stress perçu bas cette semaine — plutôt sereine sur ce plan."
        actions = ["Profite de cette période calme pour ancrer une nouvelle habitude"]
    else:
        msg = "Stress perçu dans la moyenne cette semaine, rien à signaler de particulier."
        actions = ["Note ce qui a le plus pesé cette semaine pour l'anticiper la prochaine fois"]
    content["stress"] = {"message": msg, "actions": actions}

    # --- Émotions ---
    if analysis["emotions_rating"] <= 4:
        msg = "Gestion des émotions difficile cette semaine d'après toi."
        actions = ["Identifie un moment précis où ça a été dur, et ce qui aurait aidé"]
    else:
        msg = "Bonne gestion de tes émotions cette semaine."
        actions = ["Note ce qui t'a aidé pour le refaire la prochaine fois"]
    content["emotions"] = {"message": msg, "actions": actions}

    return content


def build_weekly_message_prompt(analysis):
    def fmt_z(z):
        return f"{z:+.2f}" if z is not None else "pas assez d'historique"

    avg_sleep_txt = (
        f"{analysis['avg_sleep_week']:.1f}h/nuit en moyenne"
        if analysis["avg_sleep_week"] is not None
        else "pas assez de données de sommeil"
    )
    cardiac = analysis["cardiac"]
    weight_trend = analysis["weight_trend"]
    suggest_psy = should_suggest_psy(analysis)

    return (
        "Tu es un coach de santé et de récupération, direct, bref et "
        "bienveillant. Voici le bilan hebdo d'un utilisateur, organisé en "
        "3 piliers (Hygiène de vie, Santé physique, Santé mentale), avec "
        "pour chaque sous-score une note déclarée et, quand disponibles, "
        "des tendances objectives mesurées par sa montre connectée "
        "(z-score vs ses semaines précédentes ; positif = mieux qu'avant, "
        "négatif = moins bien qu'avant).\n\n"
        "RÈGLES IMPORTANTES :\n"
        "- Jamais de diagnostic médical ou psychologique, jamais de jargon "
        "médical. Tu n'es ni médecin ni psychologue.\n"
        "- Pour \"cardio\" et \"stress\", reste factuel et rassurant si la "
        "situation est stable/normale — ne dramatise jamais.\n"
        "- Pour \"poids\", décris la tendance sans aucun jugement (ni sur "
        "une perte ni sur une prise de poids).\n"
        "- Pour chaque catégorie, donne : \"message\" (2-3 phrases maximum, "
        "en français, tutoiement, pas d'emoji) et \"actions\" (1 à 2 actions "
        "concrètes et réalisables la semaine prochaine, phrases courtes à "
        "l'impératif, pas de généralités du type \"fais attention\").\n\n"
        "=== HYGIÈNE DE VIE ===\n"
        f"Activité — satisfaction déclarée : {analysis['sport_rating']}/10. "
        f"Moment d'entraînement cette semaine : {analysis['training_time']}. "
        f"Tendance récupération (HRV/FC repos) : {fmt_z(analysis['recovery_trend'])}\n"
        f"Sommeil — qualité ressentie : {analysis['sommeil_rating']}/10. "
        f"Données mesurées : {avg_sleep_txt}, tendance vs semaines précédentes : "
        f"{fmt_z(analysis['sleep_z'])}. Soirée ayant dégradé le sommeil signalée : "
        f"{'oui' if analysis['soiree_degradee'] else 'non'}\n"
        f"Régularité des horaires — déclarée : {analysis['regularite_rating']}/10 "
        "(pas de donnée objective disponible)\n"
        f"Nutrition — équilibre déclaré : {analysis['nutrition_rating']}/10 "
        "(pas de donnée objective disponible)\n"
        f"Tabac cette semaine : {'oui' if analysis['tabac'] else 'non'}. "
        f"Alcool à plusieurs reprises : {'oui' if analysis['alcool'] else 'non'}\n\n"
        "=== SANTÉ PHYSIQUE ===\n"
        f"Cardio (FC repos sur la semaine) — statut déterminé : {cardiac['status']} "
        f"({cardiac['detail']})\n"
        f"Poids — {weight_trend['detail']}\n"
        f"Check-up médical dans les 12 derniers mois : "
        f"{'oui' if analysis['checkup_recent'] else 'non'}\n\n"
        "=== SANTÉ MENTALE ===\n"
        f"Stress perçu (inspiré du PSS-4) — score {analysis['pss_raw']}/16 cette "
        f"semaine (plus haut = plus de stress). Tendance HRV : {fmt_z(analysis['hrv_z'])}. "
        "Suggestion d'en parler à un professionnel si ça persiste : "
        + ("oui, mentionne-le avec douceur, sans alarmer, en précisant que ce n'est pas un diagnostic" if suggest_psy else "non, ne mentionne pas ça cette semaine")
        + "\n"
        f"Gestion des émotions — déclarée : {analysis['emotions_rating']}/10 "
        "(pas de donnée objective disponible)\n\n"
        "Réponds UNIQUEMENT avec un objet JSON valide, sans texte autour, "
        "sans balises markdown, exactement sous cette forme (actions = "
        "liste de 1 ou 2 chaînes) :\n"
        '{"activite": {"message": "...", "actions": ["...", "..."]}, '
        '"sommeil": {"message": "...", "actions": ["...", "..."]}, '
        '"regularite": {"message": "...", "actions": ["...", "..."]}, '
        '"nutrition": {"message": "...", "actions": ["...", "..."]}, '
        '"cardio": {"message": "...", "actions": ["...", "..."]}, '
        '"poids": {"message": "...", "actions": ["...", "..."]}, '
        '"checkup": {"message": "...", "actions": ["...", "..."]}, '
        '"stress": {"message": "...", "actions": ["...", "..."]}, '
        '"emotions": {"message": "...", "actions": ["...", "..."]}}'
    )


def claude_weekly_messages(analysis):
    """Génère les 9 messages (+ actions) du récap hebdo via `claude -p`, en
    se basant sur l'analyse déterministe (analysis). Retombe sur le contenu
    par règles si Claude n'est pas disponible, échoue, ou renvoie un JSON
    invalide/incomplet — le repli se fait champ par champ. Retourne
    (content_dict, source)."""
    fallback = rule_based_weekly_content(analysis)
    binary = find_claude_binary()
    if binary is None:
        log_debug("claude_weekly_messages: 'claude' introuvable — contenu par règles utilisé.")
        return fallback, "regles"

    prompt = build_weekly_message_prompt(analysis)
    try:
        result = subprocess.run(
            [binary, "-p", prompt],
            capture_output=True,
            text=True,
            timeout=60,
        )
        raw = result.stdout.strip()
        if result.returncode != 0 or not raw:
            log_debug(
                f"claude_weekly_messages: échec (code {result.returncode}) "
                f"stderr={result.stderr.strip()[:300]!r} — contenu par règles utilisé."
            )
            return fallback, "regles"

        # Au cas où le modèle entoure le JSON de texte ou de ```...```
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            log_debug("claude_weekly_messages: pas de JSON trouvé dans la réponse — fallback.")
            return fallback, "regles"
        parsed = json.loads(raw[start : end + 1])

        content = {}
        any_ai = False
        for key in WEEKLY_CONTENT_KEYS:
            entry = parsed.get(key)
            entry = entry if isinstance(entry, dict) else {}

            msg = entry.get("message")
            if isinstance(msg, str) and msg.strip():
                message = msg.strip()
                any_ai = True
            else:
                message = fallback[key]["message"]

            raw_actions = entry.get("actions")
            actions = [
                a.strip()
                for a in raw_actions
                if isinstance(a, str) and a.strip()
            ] if isinstance(raw_actions, list) else []
            if actions:
                actions = actions[:2]
                any_ai = True
            else:
                actions = fallback[key]["actions"]

            content[key] = {"message": message, "actions": actions}

        if not any_ai:
            return fallback, "regles"
        log_debug(f"claude_weekly_messages: OK via {binary}")
        return content, "ia"
    except Exception as e:  # noqa: BLE001
        log_debug(f"claude_weekly_messages: exception {e!r} — contenu par règles utilisé.")
        return fallback, "regles"


def compute_weekly_recap(nights, hrv_by_date, rhr_by_date, checkin, prior_checkins):
    """Combine le bilan déclaratif de la semaine avec les données objectives
    des 7 derniers jours (vs les jours précédents comme baseline) : calcule
    les signaux et les notes pondérées par règles (déterministe), puis fait
    rédiger le message + les actions de chaque sous-score par Claude à
    partir de ces signaux (avec repli sur du contenu par règles si Claude
    est indisponible). Retourne la structure en 3 piliers : Hygiène de vie,
    Santé physique, Santé mentale."""
    analysis = analyze_week(nights, hrv_by_date, rhr_by_date, checkin, prior_checkins)
    ratings = compute_all_ratings(analysis)
    content, source = claude_weekly_messages(analysis)

    def sub(key, title):
        return {
            "key": key,
            "title": title,
            "rating": ratings[key],
            "message": content[key]["message"],
            "actions": content[key]["actions"],
        }

    def status_card(key, title, status_label):
        return {
            "key": key,
            "title": title,
            "status_label": status_label,
            "message": content[key]["message"],
            "actions": content[key]["actions"],
        }

    weight_status_label = {
        "non_renseigne": "Non renseigné",
        "premiere_mesure": "1er suivi",
        "stable": "Stable",
        "hausse": "En hausse",
        "baisse": "En baisse",
    }[analysis["weight_trend"]["status"]]
    checkup_status_label = "À jour" if analysis["checkup_recent"] else "À planifier"

    return {
        "hygiene": {
            "title": "Hygiène de vie",
            "subscores": [
                sub("activite", "Activité"),
                sub("sommeil", "Sommeil"),
                sub("regularite", "Régularité"),
                sub("nutrition", "Nutrition"),
            ],
            "insights": hygiene_insight_chips(analysis),
        },
        "physique": {
            "title": "Santé physique",
            "subscores": [sub("cardio", "Cardio")],
            "status_cards": [
                status_card("poids", "Poids", weight_status_label),
                status_card("checkup", "Check-up médical", checkup_status_label),
            ],
        },
        "mentale": {
            "title": "Santé mentale",
            "subscores": [
                sub("stress", "Stress perçu"),
                sub("emotions", "Émotions"),
            ],
            "suggest_psy": should_suggest_psy(analysis),
        },
        "source_messages": source,
    }


def mood_from_rating(rating):
    """Traduit une note /10 en humeur ('happy' | 'neutral' | 'sad') pour la
    mascotte. Toutes les notes affichées sur le récap suivent la même
    convention (haut = bon), donc un seul barème suffit ici."""
    if rating >= 7:
        return "happy"
    if rating <= 4:
        return "sad"
    return "neutral"


# Palette de marque : 3 bleus, utilisés partout (jauge + mascotte) pour une
# identité visuelle cohérente plutôt qu'une couleur différente par catégorie.
BLUE_DEEP = "#0040DD"
BLUE_BASE = "#0A84FF"
BLUE_LIGHT = "#7FC1FF"
BLUE_TRACK = "#EAF2FF"
BLUE_PATCH = "#CFE8FF"


def weekly_gauge_svg(category_key, rating, mood):
    """Jauge en arc de cercle (dégradé des 3 bleus de la marque) avec une
    mascotte ourson debout à sa base, dont l'expression (souriante, neutre,
    triste) suit `mood`. `category_key` sert juste à donner un id de
    dégradé unique par catégorie (plusieurs jauges sur la même page)."""
    r = 88
    length = math.pi * r
    pct = max(0, min(100, rating * 10))
    offset = max(0.0, min(length, length * (1 - pct / 100)))
    grad_id = f"grad-{category_key}"

    if mood == "happy":
        mouth = (
            f'<path d="M32 58 Q40 66 48 58" stroke="{BLUE_DEEP}" stroke-width="3.5" '
            'stroke-linecap="round" fill="none"/>'
        )
        brows = ""
    elif mood == "sad":
        mouth = (
            f'<path d="M32 62 Q40 55 48 62" stroke="{BLUE_DEEP}" stroke-width="3.5" '
            'stroke-linecap="round" fill="none"/>'
        )
        brows = (
            f'<path d="M20 32 L32 28" stroke="{BLUE_DEEP}" stroke-width="3.2" stroke-linecap="round"/>'
            f'<path d="M60 32 L48 28" stroke="{BLUE_DEEP}" stroke-width="3.2" stroke-linecap="round"/>'
        )
    else:
        mouth = (
            f'<path d="M33 60 L47 60" stroke="{BLUE_DEEP}" stroke-width="3.5" '
            'stroke-linecap="round" fill="none"/>'
        )
        brows = ""

    return (
        '<svg viewBox="0 0 220 160" width="100%">'
        "<defs>"
        f'<linearGradient id="{grad_id}" x1="0%" y1="0%" x2="100%" y2="0%">'
        f'<stop offset="0%" stop-color="{BLUE_DEEP}"/>'
        f'<stop offset="50%" stop-color="{BLUE_BASE}"/>'
        f'<stop offset="100%" stop-color="{BLUE_LIGHT}"/>'
        "</linearGradient>"
        "</defs>"
        f'<path d="M22 120 A{r} {r} 0 0 1 198 120" fill="none" stroke="{BLUE_TRACK}" '
        'stroke-width="16" stroke-linecap="round"/>'
        f'<path d="M22 120 A{r} {r} 0 0 1 198 120" fill="none" stroke="url(#{grad_id})" '
        f'stroke-width="16" stroke-linecap="round" stroke-dasharray="{length:.1f}" '
        f'stroke-dashoffset="{offset:.1f}"/>'
        '<g transform="translate(70,34)">'
        f'<ellipse cx="6" cy="50" rx="8" ry="14" fill="{BLUE_BASE}"/>'
        f'<ellipse cx="74" cy="50" rx="8" ry="14" fill="{BLUE_BASE}"/>'
        f'<ellipse cx="28" cy="83" rx="9" ry="7" fill="{BLUE_DEEP}"/>'
        f'<ellipse cx="52" cy="83" rx="9" ry="7" fill="{BLUE_DEEP}"/>'
        f'<circle cx="18" cy="14" r="11" fill="{BLUE_BASE}"/>'
        f'<circle cx="62" cy="14" r="11" fill="{BLUE_BASE}"/>'
        f'<path d="M40 8 C60 8 72 26 72 46 C72 68 58 83 40 83 C22 83 8 68 8 46 '
        f'C8 26 20 8 40 8 Z" fill="{BLUE_BASE}"/>'
        f'<ellipse cx="40" cy="53" rx="17" ry="14" fill="{BLUE_PATCH}"/>'
        f'{brows}'
        f'<circle cx="30" cy="38" r="5.5" fill="{BLUE_DEEP}"/>'
        f'<circle cx="50" cy="38" r="5.5" fill="{BLUE_DEEP}"/>'
        '<circle cx="28" cy="36" r="1.8" fill="#FFFFFF"/>'
        '<circle cx="48" cy="36" r="1.8" fill="#FFFFFF"/>'
        f'<ellipse cx="40" cy="49" rx="3.5" ry="2.5" fill="{BLUE_DEEP}"/>'
        f'{mouth}'
        "</g>"
        "</svg>"
    )


WEEKLY_HTML_TEMPLATE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Récap de la semaine</title>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  body { margin: 0; font-family: 'Manrope', sans-serif; background: #F2F2F7; color: #1C1C1E; }
  .wrap { max-width: 460px; margin: 0 auto; padding: 32px 20px 48px; display: flex; flex-direction: column; gap: 16px; }
  .kicker { font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: #6E6E73; margin-top: 8px; }
  h1 { margin: 0; font-weight: 800; font-size: 28px; line-height: 1.15; }
  .section-title { font-weight: 800; font-size: 18px; margin: 10px 0 -4px; display: flex; align-items: center; gap: 8px; }
  .section-dot { width: 10px; height: 10px; border-radius: 50%; background: linear-gradient(135deg, #0040DD, #7FC1FF); flex-shrink: 0; }
  .cat-card { background: #FFFFFF; border: 1px solid #E5E5EA; border-radius: 24px; overflow: hidden; box-shadow: 0 8px 20px rgba(28,28,30,0.05); }
  .gauge-panel { background: linear-gradient(160deg, #EAF2FF 0%, #ECE9FB 55%, #FCEEF3 100%); padding: 18px 16px 2px; display: flex; flex-direction: column; align-items: center; }
  .cat-label-chip { align-self: flex-start; display: flex; align-items: center; gap: 6px; font-weight: 800; font-size: 13px; background: rgba(255,255,255,0.75); padding: 4px 10px; border-radius: 999px; margin-bottom: 2px; }
  .cat-dot { width: 8px; height: 8px; border-radius: 50%; background: #0A84FF; flex-shrink: 0; }
  .cat-rating-big { font-weight: 800; font-size: 30px; margin-top: -20px; }
  .cat-rating-big span { font-weight: 600; font-size: 14px; color: #9A9A9E; margin-left: 2px; }
  .cat-body { padding: 14px 20px 20px; display: flex; flex-direction: column; gap: 8px; }
  .cat-msg { font-size: 14px; line-height: 1.5; color: #3A3A3C; }
  .cat-actions { list-style: none; margin: 4px 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
  .cat-actions li { font-size: 13px; line-height: 1.4; color: #1C1C1E; padding-left: 20px; position: relative; }
  .cat-actions li::before { content: "→"; position: absolute; left: 0; color: #0A84FF; font-weight: 700; }
  .insight-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: -4px; }
  .insight-chip { background: #EAF2FF; color: #0040DD; font-size: 12px; font-weight: 700; padding: 6px 12px; border-radius: 999px; }
  .status-card { background: #FFFFFF; border: 1px solid #E5E5EA; border-radius: 24px; padding: 18px 20px; display: flex; flex-direction: column; gap: 8px; box-shadow: 0 8px 20px rgba(28,28,30,0.05); }
  .status-head { display: flex; align-items: center; justify-content: space-between; }
  .status-title { font-weight: 800; font-size: 15px; }
  .status-badge { font-size: 12px; font-weight: 700; padding: 4px 10px; border-radius: 999px; background: #EAF2FF; color: #0040DD; white-space: nowrap; }
  .psy-note { background: #FCEEF3; border-radius: 18px; padding: 14px 16px; font-size: 13px; line-height: 1.5; color: #7A1E4A; }
</style>
</head>
<body>
<div class="wrap">

  <div>
    <div class="kicker">Semaine du __WEEK_DATE__</div>
    <h1>Ton récap hebdo</h1>
  </div>

__SECTIONS__

</div>
</body>
</html>
"""

PSY_NOTE_TEXT = (
    "Ton stress perçu est ressorti très élevé cette semaine. Ce n'est pas un "
    "diagnostic — mais si cette sensation dure, en parler à un professionnel "
    "(psychologue, médecin, service de santé universitaire) peut vraiment aider."
)


def gauge_card_html(sub):
    gauge = weekly_gauge_svg(sub["key"], sub["rating"], mood_from_rating(sub["rating"]))
    actions = "".join(f"<li>{html_escape(a)}</li>" for a in sub["actions"][:2])
    return (
        '<div class="cat-card">'
        '<div class="gauge-panel">'
        f'<div class="cat-label-chip"><span class="cat-dot"></span>{html_escape(sub["title"])}</div>'
        f"{gauge}"
        f'<div class="cat-rating-big">{sub["rating"]}<span>/10</span></div>'
        "</div>"
        '<div class="cat-body">'
        f'<div class="cat-msg">{html_escape(sub["message"])}</div>'
        f'<ul class="cat-actions">{actions}</ul>'
        "</div>"
        "</div>"
    )


def status_card_html(card):
    actions = "".join(f"<li>{html_escape(a)}</li>" for a in card["actions"][:2])
    return (
        '<div class="status-card">'
        '<div class="status-head">'
        f'<div class="status-title">{html_escape(card["title"])}</div>'
        f'<div class="status-badge">{html_escape(card["status_label"])}</div>'
        "</div>"
        f'<div class="cat-msg">{html_escape(card["message"])}</div>'
        f'<ul class="cat-actions">{actions}</ul>'
        "</div>"
    )


def section_html(title, cards_html, insights=None, psy_note=None):
    parts = [f'<div class="section-title"><span class="section-dot"></span>{html_escape(title)}</div>']
    if insights:
        chips = "".join(f'<span class="insight-chip">{html_escape(i)}</span>' for i in insights)
        parts.append(f'<div class="insight-row">{chips}</div>')
    parts.extend(cards_html)
    if psy_note:
        parts.append(f'<div class="psy-note">{html_escape(psy_note)}</div>')
    return "\n".join(parts)


def render_weekly_html(recap, checkin):
    hygiene = recap["hygiene"]
    physique = recap["physique"]
    mentale = recap["mentale"]

    hygiene_section = section_html(
        hygiene["title"],
        [gauge_card_html(s) for s in hygiene["subscores"]],
        insights=hygiene["insights"],
    )
    physique_section = section_html(
        physique["title"],
        [gauge_card_html(s) for s in physique["subscores"]]
        + [status_card_html(c) for c in physique["status_cards"]],
    )
    mentale_section = section_html(
        mentale["title"],
        [gauge_card_html(s) for s in mentale["subscores"]],
        psy_note=PSY_NOTE_TEXT if mentale["suggest_psy"] else None,
    )

    page = WEEKLY_HTML_TEMPLATE.replace("__WEEK_DATE__", html_escape(checkin.get("date", "")))
    page = page.replace(
        "__SECTIONS__", "\n\n".join([hygiene_section, physique_section, mentale_section])
    )

    out_path = DATA_DIR / "weekly_recap.html"
    out_path.write_text(page, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Programme principal
# ---------------------------------------------------------------------------

def main():
    debug = DEBUG

    access_token = get_access_token()

    sessions = fetch_sleep_sessions(access_token)
    nights = summarize_sleep(sessions)

    hrv_points = fetch_daily_metric(access_token, "daily-heart-rate-variability")
    rhr_points = fetch_daily_metric(access_token, "daily-resting-heart-rate")
    hrv_by_date = build_daily_series(hrv_points, "daily-heart-rate-variability")
    rhr_by_date = build_daily_series(rhr_points, "daily-resting-heart-rate")

    if debug:
        print(f"\n--- DEBUG : {len(nights)} nuit(s) trouvée(s) ---")
        for n in nights:
            print(n)
        print(f"\n--- DEBUG : HRV brut ({len(hrv_points)} point(s)) ---")
        for p in hrv_points:
            print(p)
        print(f"--- DEBUG : HRV parsé -> {hrv_by_date} ---")
        print(f"\n--- DEBUG : FC repos brut ({len(rhr_points)} point(s)) ---")
        for p in rhr_points:
            print(p)
        print(f"--- DEBUG : FC repos parsé -> {rhr_by_date} ---\n")

    if not nights:
        fail(
            "Aucune donnée de sommeil trouvée. Vérifie que ton Fitbit Air "
            "a bien synchronisé récemment dans l'app Google Health."
        )

    # Le script ne fait plus qu'une seule chose : le quizz hebdo + le récap.
    # On charge l'historique AVANT d'enregistrer la semaine en cours, pour
    # que les calculs de tendance (poids, habitudes) comparent bien "cette
    # semaine" aux semaines précédentes, jamais à elle-même.
    prior_checkins = load_all_checkins()
    answers = run_weekly_quiz()
    if not answers:
        fail("Quizz non complété — impossible de générer le récap.")
    log_checkin(answers)
    checkin = load_last_checkin()

    recap = compute_weekly_recap(nights, hrv_by_date, rhr_by_date, checkin, prior_checkins)
    weekly_path = render_weekly_html(recap, checkin)
    try:
        webbrowser.open(f"file://{weekly_path.resolve()}")
    except Exception:  # noqa: BLE001
        pass
    if debug:
        print(f"🗓️  Récap généré : {weekly_path}")
        print(f"🔎 Messages générés par : {recap['source_messages']}")


if __name__ == "__main__":
    main()
