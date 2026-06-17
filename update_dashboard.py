#!/usr/bin/env python3
"""
update_dashboard.py
--------------------
Récupère les données de l'API Strava (profil, activités récentes, zones,
plan d'entraînement) et génère un fichier data.json statique destiné à
être lu par dashboard.html.

CONFIGURATION (GitHub Actions)
--------------------------------
Les identifiants Strava sont lus depuis les variables d'environnement
STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN.

Dans GitHub : Settings > Secrets and variables > Actions > New repository
secret, pour chacune des 3 valeurs.

En local pour tester, tu peux créer un fichier `.env` (voir .env.example)
ou exporter les variables directement dans ton shell.

OBTENIR LE REFRESH TOKEN (à faire une seule fois)
--------------------------------------------------
1. Va sur https://www.strava.com/settings/api et crée une application
   (note ton Client ID et Client Secret).
2. Construis cette URL en remplaçant CLIENT_ID, puis ouvre-la dans ton
   navigateur :

   https://www.strava.com/oauth/authorize?client_id=CLIENT_ID&response_type=code&redirect_uri=http://localhost/exchange_token&approval_prompt=force&scope=activity:read_all,profile:read_all

3. Autorise l'app. Tu seras redirigé vers une URL du type
   http://localhost/exchange_token?state=&code=XXXX&scope=...
   Copie la valeur de "code".

4. Échange ce code contre un refresh_token :

   curl -X POST https://www.strava.com/oauth/token \\
        -d client_id=CLIENT_ID \\
        -d client_secret=CLIENT_SECRET \\
        -d code=XXXX \\
        -d grant_type=authorization_code

   La réponse contient "refresh_token" : mets-le dans le secret GitHub
   STRAVA_REFRESH_TOKEN.
"""

import os
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv optionnel ; on peut aussi définir les vars d'env directement

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_FILE = SCRIPT_DIR / "data.json"

STRAVA_API_BASE = "https://www.strava.com/api/v3"
TOKEN_URL = "https://www.strava.com/oauth/token"


def get_access_token():
    """Échange le refresh_token contre un access_token valide."""
    client_id = os.environ.get("STRAVA_CLIENT_ID")
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET")
    refresh_token = os.environ.get("STRAVA_REFRESH_TOKEN")

    if not all([client_id, client_secret, refresh_token]):
        sys.exit(
            "Erreur : STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET et "
            "STRAVA_REFRESH_TOKEN doivent être définis (fichier .env). "
            "Voir les instructions en haut de ce script."
        )

    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def strava_get(path, token, params=None):
    resp = requests.get(
        f"{STRAVA_API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fmt_pace(seconds_per_km):
    """Convertit un temps en secondes/km en mm:ss."""
    if not seconds_per_km or seconds_per_km <= 0:
        return None
    minutes = int(seconds_per_km // 60)
    secs = int(round(seconds_per_km % 60))
    if secs == 60:
        minutes += 1
        secs = 0
    return f"{minutes}:{secs:02d}"


def build_dataset(token):
    athlete = strava_get("/athlete", token)

    # Stats totales (running + ride etc.)
    stats = strava_get(f"/athletes/{athlete['id']}/stats", token)

    # Activités récentes (30 dernières)
    activities = strava_get(
        "/athlete/activities", token, params={"per_page": 30, "page": 1}
    )

    # Zones (FC / allure)
    try:
        zones = strava_get("/athlete/zones", token)
    except requests.HTTPError:
        zones = None

    recent_runs = []
    for act in activities:
        if act.get("type") not in ("Run", "TrailRun", "VirtualRun"):
            continue
        distance_km = act["distance"] / 1000
        moving_time = act["moving_time"]
        pace_sec_per_km = (moving_time / distance_km) if distance_km > 0 else None

        recent_runs.append({
            "id": act["id"],
            "name": act["name"],
            "date": act["start_date_local"],
            "distance_km": round(distance_km, 2),
            "moving_time_s": moving_time,
            "elapsed_time_s": act["elapsed_time"],
            "elevation_gain_m": act.get("total_elevation_gain", 0),
            "avg_pace": fmt_pace(pace_sec_per_km),
            "avg_hr": act.get("average_heartrate"),
            "max_hr": act.get("max_heartrate"),
            "avg_speed_kmh": round(act["average_speed"] * 3.6, 2),
            "kudos": act.get("kudos_count", 0),
            "pr_count": act.get("pr_count", 0),
            "type": act.get("type"),
        })
        if len(recent_runs) >= 10:
            break

    # Plan d'entraînement (séances à venir) — best effort, structure
    # renvoyée par l'API peut varier.
    upcoming_workouts = []
    try:
        plan = strava_get("/athlete/training_plan", token)
    except requests.HTTPError:
        plan = None

    if plan:
        # On essaie d'extraire une liste de séances à venir quel que soit
        # le format exact renvoyé (différentes structures observées selon
        # comptes/locales).
        candidates = []
        if isinstance(plan, dict):
            for key in ("upcoming_workouts", "workouts", "scheduled_workouts", "items"):
                if isinstance(plan.get(key), list):
                    candidates = plan[key]
                    break
        elif isinstance(plan, list):
            candidates = plan

        now = datetime.now(timezone.utc)
        for w in candidates:
            if not isinstance(w, dict):
                continue
            date_str = w.get("date") or w.get("scheduled_date") or w.get("start_date")
            upcoming_workouts.append({
                "date": date_str,
                "title": w.get("title") or w.get("name") or w.get("type") or "Séance",
                "description": w.get("description") or w.get("details") or "",
                "type": w.get("workout_type") or w.get("type"),
                "target_distance_km": w.get("distance_km") or (
                    round(w["distance"] / 1000, 2) if w.get("distance") else None
                ),
            })

    totals = stats.get("ytd_run_totals", {})
    recent_totals = stats.get("recent_run_totals", {})
    all_time_totals = stats.get("all_run_totals", {})

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "athlete": {
            "firstname": athlete.get("firstname"),
            "lastname": athlete.get("lastname"),
            "profile_picture": athlete.get("profile"),
            "city": athlete.get("city"),
            "country": athlete.get("country"),
        },
        "stats": {
            "ytd": {
                "runs": totals.get("count", 0),
                "distance_km": round(totals.get("distance", 0) / 1000, 1),
                "elevation_gain_m": round(totals.get("elevation_gain", 0)),
                "moving_time_s": totals.get("moving_time", 0),
            },
            "last_4_weeks": {
                "runs": recent_totals.get("count", 0),
                "distance_km": round(recent_totals.get("distance", 0) / 1000, 1),
                "elevation_gain_m": round(recent_totals.get("elevation_gain", 0)),
                "moving_time_s": recent_totals.get("moving_time", 0),
            },
            "all_time": {
                "runs": all_time_totals.get("count", 0),
                "distance_km": round(all_time_totals.get("distance", 0) / 1000, 1),
                "elevation_gain_m": round(all_time_totals.get("elevation_gain", 0)),
            },
            "biggest_run_km": round(stats.get("biggest_run_distance", 0) / 1000, 2),
        },
        "zones": zones,
        "recent_runs": recent_runs,
        "upcoming_workouts": upcoming_workouts,
    }
    return data


def main():
    token = get_access_token()
    data = build_dataset(token)

    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"data.json généré ({DATA_FILE}) — {len(data['recent_runs'])} sorties récentes")


if __name__ == "__main__":
    main()
