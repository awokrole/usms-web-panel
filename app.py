import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from functools import wraps

import gspread
import requests
from flask import Flask, redirect, render_template, request, session, url_for
from google.oauth2.credentials import Credentials


app = Flask(__name__)
app.secret_key = os.environ.get("WEB_SECRET_KEY") or secrets.token_hex(32)

DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "").strip()
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "").strip()
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

GOOGLE_TOKEN_JSON = os.environ.get("GOOGLE_TOKEN_JSON", "")
SHEET_ID = os.environ.get("SHEET_ID", "")
ROSTER_SHEET_NAME = os.environ.get("ROSTER_SHEET_NAME", "USMS")
TRAINING_SHEET_NAME = os.environ.get("TRAINING_SHEET_NAME", "Szkolenia")
AKTA_SHEET_NAME = os.environ.get("AKTA_SHEET_NAME", "Akta")

DB_PATH = os.environ.get("DB_PATH", "/data/sluzby.db")

DISCORD_API = "https://discord.com/api/v10"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def parse_role_ids(value: str):
    result = set()
    for part in (value or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            result.add(int(part))
    return result


WEB_ADMIN_ROLE_IDS = parse_role_ids(os.environ.get("WEB_ADMIN_ROLE_IDS", ""))


def oauth_redirect_uri():
    if PUBLIC_URL:
        return f"{PUBLIC_URL}/auth/discord/callback"
    return url_for("discord_callback", _external=True)


def logged_in_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "discord_user" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "discord_user" not in session:
            return redirect(url_for("login"))
        if not session.get("is_admin", False):
            return render_template("error.html", title="Brak dostępu",
                                   message="Nie masz uprawnień do tej sekcji."), 403
        return view(*args, **kwargs)
    return wrapped


def get_spreadsheet():
    if not GOOGLE_TOKEN_JSON or not SHEET_ID:
        raise RuntimeError("Brak GOOGLE_TOKEN_JSON lub SHEET_ID.")
    token_info = json.loads(GOOGLE_TOKEN_JSON)
    creds = Credentials.from_authorized_user_info(token_info, GOOGLE_SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID)


def checkbox_to_bool(value):
    return str(value or "").strip().casefold() in {"true", "prawda", "1", "yes", "tak"}


def normalize_badge(value):
    value = str(value or "").strip()
    if value.endswith(".0"):
        value = value[:-2]
    return value


def load_officers():
    spreadsheet = get_spreadsheet()
    roster = spreadsheet.worksheet(ROSTER_SHEET_NAME).get(
        "A:E", value_render_option="FORMATTED_VALUE"
    )
    trainings = spreadsheet.worksheet(TRAINING_SHEET_NAME).get(
        "A:L", value_render_option="FORMATTED_VALUE"
    )
    akta = spreadsheet.worksheet(AKTA_SHEET_NAME).get(
        "A:L", value_render_option="FORMATTED_VALUE"
    )

    training_names = ["FLETC", "RO", "KPP", "NL I", "NL II", "SV", "MEERY", "SEU", "ASU", "HAW"]
    training_by_badge = {}

    for row in trainings:
        row = list(row) + [""] * (12 - len(row))
        badge = normalize_badge(row[0])
        if not badge or not badge.isdigit():
            continue
        training_by_badge[badge] = [
            name for idx, name in enumerate(training_names, start=2)
            if checkbox_to_bool(row[idx])
        ]

    akta_by_badge = {}
    for row in akta:
        row = list(row) + [""] * (12 - len(row))
        badge = normalize_badge(row[0])
        if not badge or not badge.isdigit():
            continue
        akta_by_badge[badge] = {
            "plus": sum(checkbox_to_bool(v) for v in row[2:5]),
            "minus": sum(checkbox_to_bool(v) for v in row[5:8]),
            "praise": sum(checkbox_to_bool(v) for v in row[8:10]),
            "reprimand": sum(checkbox_to_bool(v) for v in row[10:12]),
        }

    records = []
    for row in roster:
        row = list(row) + [""] * (5 - len(row))
        rank = str(row[0] or "").strip()
        badge = normalize_badge(row[1])
        full_name = str(row[2] or "").strip()
        csn = str(row[3] or "").strip()
        discord_id = str(row[4] or "").strip()

        if not full_name or not discord_id.isdigit():
            continue

        stats = akta_by_badge.get(
            badge, {"plus": 0, "minus": 0, "praise": 0, "reprimand": 0}
        )
        records.append({
            "rank": rank or "Brak",
            "badge": badge or "Brak",
            "full_name": full_name,
            "csn": csn,
            "discord_id": int(discord_id),
            "trainings": training_by_badge.get(badge, []),
            **stats,
        })

    def key(item):
        try:
            return int(item["badge"])
        except Exception:
            return 999999

    return sorted(records, key=key)


def load_duty_state():
    result = {}
    if not DB_PATH or not os.path.exists(DB_PATH):
        return result

    try:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        rows = db.execute("""
            SELECT user_id, total_seconds, start_time, pause_start,
                   paused_seconds, suspension_until, vacation_start, vacation_end
            FROM users
        """).fetchall()
        db.close()
    except Exception:
        return result

    now = datetime.now(timezone.utc)
    for row in rows:
        total = int(row["total_seconds"] or 0)
        start_time = row["start_time"]
        pause_start = row["pause_start"]
        paused = int(row["paused_seconds"] or 0)

        active = bool(start_time)
        on_pause = bool(start_time and pause_start)

        if start_time:
            try:
                start = datetime.fromisoformat(start_time)
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                end = now
                if pause_start:
                    p = datetime.fromisoformat(pause_start)
                    if p.tzinfo is None:
                        p = p.replace(tzinfo=timezone.utc)
                    end = p
                total += max(0, int((end - start).total_seconds()) - paused)
            except Exception:
                pass

        result[int(row["user_id"])] = {
            "total_seconds": total,
            "active": active,
            "on_pause": on_pause,
            "suspension_until": row["suspension_until"],
            "vacation_start": row["vacation_start"],
            "vacation_end": row["vacation_end"],
        }

    return result


def format_seconds(seconds):
    seconds = max(0, int(seconds or 0))
    hours, rem = divmod(seconds, 3600)
    minutes, sec = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{sec:02d}"


def discord_member(user_id: str):
    if not DISCORD_TOKEN or not DISCORD_GUILD_ID:
        return None
    r = requests.get(
        f"{DISCORD_API}/guilds/{DISCORD_GUILD_ID}/members/{user_id}",
        headers={"Authorization": f"Bot {DISCORD_TOKEN}"},
        timeout=10,
    )
    if r.status_code != 200:
        return None
    return r.json()


def is_member_admin(member):
    if not member:
        return False
    roles = {int(x) for x in member.get("roles", []) if str(x).isdigit()}
    return bool(WEB_ADMIN_ROLE_IDS & roles) if WEB_ADMIN_ROLE_IDS else False


@app.context_processor
def inject_globals():
    return {
        "current_user": session.get("discord_user"),
        "is_admin": session.get("is_admin", False),
    }


@app.route("/")
def index():
    if "discord_user" not in session:
        return render_template("login.html")
    return redirect(url_for("dashboard"))


@app.route("/login")
def login():
    if "discord_user" in session:
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/auth/discord")
def discord_login():
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET:
        return render_template("error.html", title="Brak konfiguracji",
                               message="Brak DISCORD_CLIENT_ID lub DISCORD_CLIENT_SECRET."), 500

    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state

    params = {
        "client_id": DISCORD_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": oauth_redirect_uri(),
        "scope": "identify",
        "state": state,
        "prompt": "none",
    }
    req = requests.Request("GET", f"{DISCORD_API}/oauth2/authorize", params=params).prepare()
    return redirect(req.url)


@app.route("/auth/discord/callback")
def discord_callback():
    if request.args.get("state") != session.pop("oauth_state", None):
        return render_template("error.html", title="Błąd logowania",
                               message="Nieprawidłowy stan OAuth. Spróbuj zalogować się ponownie."), 400

    code = request.args.get("code")
    if not code:
        return redirect(url_for("login"))

    token_response = requests.post(
        f"{DISCORD_API}/oauth2/token",
        data={
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": oauth_redirect_uri(),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )

    if token_response.status_code != 200:
        return render_template("error.html", title="Błąd Discord OAuth",
                               message="Discord nie zwrócił poprawnego tokenu logowania."), 400

    access_token = token_response.json().get("access_token")
    user_response = requests.get(
        f"{DISCORD_API}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )

    if user_response.status_code != 200:
        return render_template("error.html", title="Błąd Discord",
                               message="Nie udało się pobrać danych konta Discord."), 400

    user = user_response.json()
    member = discord_member(user["id"])

    if member is None:
        return render_template(
            "error.html",
            title="Brak dostępu",
            message="To konto Discord nie jest członkiem serwera USMS albo bot nie może zweryfikować członkostwa.",
        ), 403

    avatar = user.get("avatar")
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{user['id']}/{avatar}.png?size=128"
        if avatar
        else "https://cdn.discordapp.com/embed/avatars/0.png"
    )

    session["discord_user"] = {
        "id": user["id"],
        "username": user.get("global_name") or user.get("username") or "Discord User",
        "avatar_url": avatar_url,
    }
    session["is_admin"] = is_member_admin(member)

    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@logged_in_required
def dashboard():
    try:
        officers = load_officers()
    except Exception as exc:
        officers = []
        sheet_error = str(exc)
    else:
        sheet_error = None

    duty = load_duty_state()
    active = []
    vacation_count = 0
    suspended_count = 0
    total_seconds = 0

    now = datetime.now(timezone.utc)

    for officer in officers:
        state = duty.get(officer["discord_id"], {})
        officer["duty"] = state
        officer["time_text"] = format_seconds(state.get("total_seconds", 0))
        total_seconds += state.get("total_seconds", 0)

        if state.get("active"):
            active.append(officer)

        for key, counter_name in (("suspension_until", "s"), ("vacation_end", "v")):
            raw = state.get(key)
            if not raw:
                continue
            try:
                dt = datetime.fromisoformat(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt > now:
                    if counter_name == "s":
                        suspended_count += 1
                    else:
                        vacation_count += 1
            except Exception:
                pass

    active = sorted(active, key=lambda x: x["duty"].get("total_seconds", 0), reverse=True)

    return render_template(
        "dashboard.html",
        officers=officers,
        active=active[:8],
        vacation_count=vacation_count,
        suspended_count=suspended_count,
        total_hours=round(total_seconds / 3600, 1),
        sheet_error=sheet_error,
    )


@app.route("/funkcjonariusze")
@logged_in_required
def officers():
    records = load_officers()
    duty = load_duty_state()
    q = request.args.get("q", "").strip().casefold()

    for officer in records:
        state = duty.get(officer["discord_id"], {})
        officer["active"] = state.get("active", False)
        officer["time_text"] = format_seconds(state.get("total_seconds", 0))

    if q:
        records = [
            r for r in records
            if q in r["full_name"].casefold()
            or q in str(r["badge"]).casefold()
            or q in r["rank"].casefold()
        ]

    return render_template("officers.html", officers=records, q=request.args.get("q", ""))


@app.route("/funkcjonariusze/<badge>")
@logged_in_required
def officer_detail(badge):
    officers = load_officers()
    officer = next((x for x in officers if str(x["badge"]) == str(badge)), None)
    if officer is None:
        return render_template("error.html", title="Nie znaleziono",
                               message="Nie znaleziono funkcjonariusza o podanej odznace."), 404

    state = load_duty_state().get(officer["discord_id"], {})
    officer["active"] = state.get("active", False)
    officer["on_pause"] = state.get("on_pause", False)
    officer["time_text"] = format_seconds(state.get("total_seconds", 0))
    officer["suspension_until"] = state.get("suspension_until")
    officer["vacation_end"] = state.get("vacation_end")

    return render_template("officer.html", officer=officer)


@app.route("/akta")
@logged_in_required
def akta():
    records = load_officers()
    return render_template("akta.html", officers=records)


@app.route("/szkolenia")
@logged_in_required
def trainings():
    records = load_officers()
    return render_template("trainings.html", officers=records)


@app.route("/logi")
@admin_required
def logs():
    return render_template("placeholder.html", title="Logi komend",
                           message="Sekcję logów podłączymy w kolejnym etapie do kanału Discord lub osobnej tabeli bazy.")


@app.errorhandler(500)
def internal_error(error):
    return render_template("error.html", title="Błąd serwera",
                           message="Wystąpił błąd po stronie panelu. Sprawdź logi Railway."), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=False)
