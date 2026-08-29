import json
import os
import secrets
import sqlite3
import io
from datetime import datetime, timezone

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    RealDictCursor = None
from functools import wraps

import gspread
import requests
from flask import Flask, abort, redirect, render_template, request, send_file, session, url_for
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

DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
DB_PATH = (os.environ.get("DB_PATH") or "").strip()


if DATABASE_URL:
    print("🐘 WEB: DATABASE_URL wykryte — panel będzie czytał PostgreSQL.", flush=True)
elif DB_PATH:
    print(f"🗄️ WEB: PostgreSQL nieustawiony — fallback SQLite: {DB_PATH}", flush=True)
else:
    print("⚠️ WEB: Brak DATABASE_URL i DB_PATH — stan służby będzie pusty.", flush=True)


DISCORD_API = "https://discord.com/api/v10"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

PROFILE_PHOTO_MAX_BYTES = 5 * 1024 * 1024
DOCUMENT_MAX_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp"}



def parse_role_ids(value: str):
    result = set()
    for part in (value or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            result.add(int(part))
    return result


WEB_ADMIN_ROLE_IDS = parse_role_ids(os.environ.get("WEB_ADMIN_ROLE_IDS", ""))



def pg_connect(application_name="usms-web-panel"):
    if not DATABASE_URL:
        raise RuntimeError("Ta funkcja wymaga DATABASE_URL/PostgreSQL.")
    if psycopg2 is None:
        raise RuntimeError("Brakuje psycopg2-binary.")
    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=10,
        application_name=application_name,
    )


def ensure_web_profile_tables():
    """Tworzy trwałe tabele zdjęć profili i dokumentów funkcjonariuszy."""
    if not DATABASE_URL:
        return

    conn = pg_connect("usms-web-panel-init")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS officer_profiles (
                    badge_number TEXT PRIMARY KEY,
                    photo_mime TEXT DEFAULT NULL,
                    photo_data BYTEA DEFAULT NULL,
                    photo_uploaded_by BIGINT DEFAULT NULL,
                    photo_uploaded_at TEXT DEFAULT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS officer_documents (
                    id BIGSERIAL PRIMARY KEY,
                    badge_number TEXT NOT NULL,
                    title TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    file_data BYTEA NOT NULL,
                    uploaded_by BIGINT NOT NULL,
                    uploaded_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_officer_documents_badge
                ON officer_documents (badge_number, uploaded_at DESC)
                """
            )
        conn.commit()
        print("✅ WEB: tabele profili i dokumentów gotowe.", flush=True)
    finally:
        conn.close()



def get_officer_by_badge(badge: str):
    for officer in load_officers():
        if str(officer.get("badge")) == str(badge):
            return officer
    return None


def current_user_owns_officer(officer) -> bool:
    if not officer:
        return False

    user = session.get("discord_user") or {}
    current_id = str(user.get("id", "")).strip()
    officer_id = str(officer.get("discord_id", "")).strip()

    return bool(current_id and officer_id and current_id == officer_id)


def get_profile_meta(badge: str):
    if not DATABASE_URL:
        return {"has_photo": False}

    conn = pg_connect("usms-profile-meta")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT badge_number, photo_mime, photo_uploaded_by, photo_uploaded_at,
                       (photo_data IS NOT NULL) AS has_photo
                FROM officer_profiles
                WHERE badge_number = %s
                """,
                (str(badge),),
            )
            row = cur.fetchone()
            return dict(row) if row else {"has_photo": False}
    finally:
        conn.close()


def get_officer_documents(badge: str):
    if not DATABASE_URL:
        return []

    conn = pg_connect("usms-profile-documents")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, badge_number, title, mime_type, uploaded_by, uploaded_at
                FROM officer_documents
                WHERE badge_number = %s
                ORDER BY id DESC
                """,
                (str(badge),),
            )
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _read_upload(file_storage, max_bytes: int):
    if file_storage is None or not file_storage.filename:
        raise ValueError("Nie wybrano pliku.")

    mime = (file_storage.mimetype or "").lower().strip()
    if mime not in ALLOWED_IMAGE_MIMES:
        raise ValueError("Dozwolone są tylko obrazy PNG, JPG/JPEG i WEBP.")

    data = file_storage.read(max_bytes + 1)
    if not data:
        raise ValueError("Plik jest pusty.")
    if len(data) > max_bytes:
        raise ValueError(
            f"Plik jest za duży. Maksymalny rozmiar to {max_bytes // (1024 * 1024)} MB."
        )

    return mime, data


try:
    ensure_web_profile_tables()
except Exception as exc:
    print(f"⚠️ WEB: nie udało się przygotować tabel profili/dokumentów: {exc!r}", flush=True)


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
    creds = Credentials.from_authorized_user_info(token_info)
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


def _load_duty_rows():
    """
    Czyta stan służby z tej samej bazy co bot.

    Priorytet:
    1. PostgreSQL przez DATABASE_URL (Railway / produkcja)
    2. SQLite przez DB_PATH (opcjonalny fallback lokalny)
    """
    query = """
        SELECT user_id, total_seconds, lifetime_seconds, start_time, pause_start,
               paused_seconds, suspension_until, vacation_start, vacation_end
        FROM users
    """

    if DATABASE_URL:
        if psycopg2 is None:
            raise RuntimeError(
                "DATABASE_URL jest ustawione, ale brakuje psycopg2-binary."
            )

        conn = psycopg2.connect(
            DATABASE_URL,
            connect_timeout=10,
            application_name="usms-web-panel",
        )
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query)
                return cur.fetchall()
        finally:
            conn.close()

    if DB_PATH and os.path.exists(DB_PATH):
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        try:
            return db.execute(query).fetchall()
        finally:
            db.close()

    return []


def load_duty_state():
    result = {}

    try:
        rows = _load_duty_rows()
    except Exception as exc:
        # Nie wywracamy całego panelu, ale błąd będzie widoczny w Railway Logs.
        print(f"[DB] Nie udało się odczytać stanu służby: {exc!r}", flush=True)
        return result

    now = datetime.now(timezone.utc)

    for row in rows:
        # total_saved = zakończone godziny bieżącego tygodnia.
        weekly_saved = int(row["total_seconds"] or 0)
        # lifetime_saved = zakończone godziny od początku historii.
        lifetime_saved = int(row["lifetime_seconds"] or 0)
        start_time = row["start_time"]
        pause_start = row["pause_start"]
        paused = int(row["paused_seconds"] or 0)

        active = bool(start_time)
        on_pause = bool(start_time and pause_start)
        current_shift_seconds = 0

        if start_time:
            try:
                start = datetime.fromisoformat(str(start_time))
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)

                end = now

                # Jeśli trwa przerwa, bieżąca służba zatrzymuje się w momencie
                # rozpoczęcia przerwy.
                if pause_start:
                    p = datetime.fromisoformat(str(pause_start))
                    if p.tzinfo is None:
                        p = p.replace(tzinfo=timezone.utc)
                    end = p

                current_shift_seconds = max(
                    0,
                    int((end - start).total_seconds()) - paused,
                )
            except Exception:
                current_shift_seconds = 0

        # Oba liczniki uwzględniają aktualnie trwającą służbę.
        weekly_with_current = weekly_saved + current_shift_seconds
        lifetime_with_current = lifetime_saved + current_shift_seconds

        result[int(row["user_id"])] = {
            "total_seconds": weekly_with_current,
            "weekly_seconds": weekly_with_current,
            "lifetime_seconds": lifetime_with_current,
            "saved_total_seconds": weekly_saved,
            "saved_lifetime_seconds": lifetime_saved,
            "current_shift_seconds": current_shift_seconds,
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

    # Opcjonalne ręczne role administracyjne nadal są obsługiwane.
    if WEB_ADMIN_ROLE_IDS and (WEB_ADMIN_ROLE_IDS & roles):
        return True

    if not DISCORD_TOKEN or not DISCORD_GUILD_ID:
        return False

    try:
        headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}

        guild_resp = requests.get(
            f"{DISCORD_API}/guilds/{DISCORD_GUILD_ID}",
            headers=headers,
            timeout=10,
        )
        roles_resp = requests.get(
            f"{DISCORD_API}/guilds/{DISCORD_GUILD_ID}/roles",
            headers=headers,
            timeout=10,
        )

        if guild_resp.status_code != 200 or roles_resp.status_code != 200:
            return False

        guild = guild_resp.json()
        user_id = str(member.get("user", {}).get("id", ""))

        # Właściciel serwera jest administratorem.
        if user_id and str(guild.get("owner_id")) == user_id:
            return True

        # Discord permission bit ADMINISTRATOR = 1 << 3.
        ADMINISTRATOR = 1 << 3

        for role in roles_resp.json():
            role_id = int(role.get("id", 0))
            if role_id not in roles:
                continue
            permissions = int(role.get("permissions", "0"))
            if permissions & ADMINISTRATOR:
                return True

    except Exception as exc:
        print(f"[ADMIN] Nie udało się sprawdzić uprawnień Discord: {exc!r}", flush=True)

    return False


def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def validate_csrf():
    expected = session.get("_csrf_token")
    supplied = request.form.get("csrf_token", "")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        abort(400)


@app.context_processor
def inject_globals():
    return {
        "current_user": session.get("discord_user"),
        "is_admin": session.get("is_admin", False),
        "csrf_token": csrf_token,
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
    weekly_total_seconds = 0
    lifetime_total_seconds = 0

    now = datetime.now(timezone.utc)

    for officer in officers:
        state = duty.get(officer["discord_id"], {})
        officer["duty"] = state

        # time_text pozostaje łącznym czasem i jest używany w pozostałych
        # widokach panelu.
        officer["time_text"] = format_seconds(state.get("total_seconds", 0))

        # Na dashboardzie w sekcji "Aktualnie na służbie" pokazujemy WYŁĄCZNIE
        # czas bieżącej sesji od ostatniego START SŁUŻBY.
        officer["current_shift_text"] = format_seconds(
            state.get("current_shift_seconds", 0)
        )

        weekly_total_seconds += state.get("weekly_seconds", 0)
        lifetime_total_seconds += state.get("lifetime_seconds", 0)

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

    active = sorted(active, key=lambda x: x["duty"].get("current_shift_seconds", 0), reverse=True)

    return render_template(
        "dashboard.html",
        officers=officers,
        active=active[:8],
        vacation_count=vacation_count,
        suspended_count=suspended_count,
        weekly_hours=round(weekly_total_seconds / 3600, 1),
        lifetime_hours=round(lifetime_total_seconds / 3600, 1),
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
    officer["weekly_time_text"] = format_seconds(state.get("weekly_seconds", 0))
    officer["lifetime_time_text"] = format_seconds(state.get("lifetime_seconds", 0))
    officer["current_shift_text"] = format_seconds(state.get("current_shift_seconds", 0))
    officer["suspension_until"] = state.get("suspension_until")
    officer["vacation_end"] = state.get("vacation_end")

    profile_meta = get_profile_meta(officer["badge"])
    documents = get_officer_documents(officer["badge"])

    return render_template(
        "officer.html",
        officer=officer,
        profile_meta=profile_meta,
        documents=documents,
        can_upload_own_profile=current_user_owns_officer(officer),
    )



@app.route("/funkcjonariusze/<badge>/zdjecie", methods=["POST"])
@logged_in_required
def upload_officer_photo(badge):
    validate_csrf()

    officer = get_officer_by_badge(badge)
    if not officer:
        abort(404)

    # Każdy zalogowany użytkownik może dodać/zmienić zdjęcie WYŁĄCZNIE
    # na swoim własnym profilu (Discord ID z rosteru musi pasować do sesji).
    if not current_user_owns_officer(officer):
        abort(403)

    try:
        mime, data = _read_upload(request.files.get("photo"), PROFILE_PHOTO_MAX_BYTES)
    except ValueError as exc:
        return render_template("error.html", title="Błąd zdjęcia", message=str(exc)), 400

    conn = pg_connect("usms-upload-photo")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO officer_profiles (
                    badge_number, photo_mime, photo_data,
                    photo_uploaded_by, photo_uploaded_at
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(badge_number) DO UPDATE SET
                    photo_mime = excluded.photo_mime,
                    photo_data = excluded.photo_data,
                    photo_uploaded_by = excluded.photo_uploaded_by,
                    photo_uploaded_at = excluded.photo_uploaded_at
                """,
                (
                    str(badge),
                    mime,
                    psycopg2.Binary(data),
                    int(session["discord_user"]["id"]),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    return redirect(url_for("officer_detail", badge=badge))


@app.route("/funkcjonariusze/<badge>/zdjecie/usun", methods=["POST"])
@admin_required
def delete_officer_photo(badge):
    validate_csrf()

    conn = pg_connect("usms-delete-photo")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE officer_profiles
                SET photo_mime = NULL,
                    photo_data = NULL,
                    photo_uploaded_by = NULL,
                    photo_uploaded_at = NULL
                WHERE badge_number = %s
                """,
                (str(badge),),
            )
        conn.commit()
    finally:
        conn.close()

    return redirect(url_for("officer_detail", badge=badge))


@app.route("/media/profil/<badge>")
@logged_in_required
def officer_photo_media(badge):
    conn = pg_connect("usms-photo-media")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT photo_mime, photo_data
                FROM officer_profiles
                WHERE badge_number = %s AND photo_data IS NOT NULL
                """,
                (str(badge),),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        abort(404)

    return send_file(
        io.BytesIO(bytes(row["photo_data"])),
        mimetype=row["photo_mime"],
        max_age=3600,
        download_name=f"profil-{badge}",
    )


@app.route("/funkcjonariusze/<badge>/dokumenty", methods=["POST"])
@logged_in_required
def upload_officer_document(badge):
    validate_csrf()

    officer = get_officer_by_badge(badge)
    if not officer:
        abort(404)

    # Dokumenty można dodawać tylko do własnego profilu.
    if not current_user_owns_officer(officer):
        abort(403)

    title = (request.form.get("title") or "").strip()
    if not title:
        return render_template(
            "error.html",
            title="Błąd dokumentu",
            message="Podaj nazwę dokumentu.",
        ), 400

    if len(title) > 120:
        title = title[:120]

    try:
        mime, data = _read_upload(request.files.get("document"), DOCUMENT_MAX_BYTES)
    except ValueError as exc:
        return render_template("error.html", title="Błąd dokumentu", message=str(exc)), 400

    conn = pg_connect("usms-upload-document")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO officer_documents (
                    badge_number, title, mime_type, file_data,
                    uploaded_by, uploaded_at
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    str(badge),
                    title,
                    mime,
                    psycopg2.Binary(data),
                    int(session["discord_user"]["id"]),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    return redirect(url_for("officer_detail", badge=badge))


@app.route("/dokument/<int:document_id>")
@logged_in_required
def officer_document_media(document_id):
    conn = pg_connect("usms-document-media")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, title, mime_type, file_data
                FROM officer_documents
                WHERE id = %s
                """,
                (int(document_id),),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        abort(404)

    return send_file(
        io.BytesIO(bytes(row["file_data"])),
        mimetype=row["mime_type"],
        max_age=3600,
        download_name=row["title"],
    )


@app.route("/dokument/<int:document_id>/usun", methods=["POST"])
@admin_required
def delete_officer_document(document_id):
    validate_csrf()

    badge = request.form.get("badge", "").strip()

    conn = pg_connect("usms-delete-document")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM officer_documents WHERE id = %s",
                (int(document_id),),
            )
        conn.commit()
    finally:
        conn.close()

    if badge:
        return redirect(url_for("officer_detail", badge=badge))
    return redirect(url_for("officers"))


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


@app.route("/kompendium")
@logged_in_required
def kompendium():
    return render_template("kompendium.html")


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

# ============================================================
# V12 — EGZAMINY Z KOMPENDIUM
# ============================================================
from datetime import timedelta
from zoneinfo import ZoneInfo
import random

EXAM_TZ = ZoneInfo("Europe/Warsaw")
EXAM_QUESTION_COUNT = 20
EXAM_DURATION_MINUTES = 20
EXAM_PASS_PERCENT = 80

EXAM_QUESTIONS = [
    ("Radio", "Co oznacza kod 10-80?", ["Rozpoczynam pościg", "Napad", "Potrzebne wsparcie", "Strzały"], "Rozpoczynam pościg"),
    ("Radio", "Co oznacza kod 10-81?", ["Pościg zakończony powodzeniem", "Wznowienie pościgu", "Pościg zakończony porażką", "Kradzież pojazdu"], "Pościg zakończony powodzeniem"),
    ("Radio", "Co oznacza kod 10-82?", ["Pościg zakończony porażką", "Pościg zakończony powodzeniem", "W drodze", "Napad"], "Pościg zakończony porażką"),
    ("Radio", "Co oznacza kod 10-83?", ["Wznowienie pościgu", "Rozpoczynam pościg", "Osoba poszukiwana", "Teren czysty"], "Wznowienie pościgu"),
    ("Radio", "Co oznacza kod 10-90?", ["Napad", "Strzały", "Sprzedaż narkotyków", "Ucieczka z więzienia"], "Napad"),
    ("Radio", "Co oznacza kod 10-21?", ["Potrzebne wsparcie", "Nie potrzebuję wsparcia", "Obecna lokalizacja", "Jednostka na miejscu"], "Potrzebne wsparcie"),
    ("Radio", "Co oznacza kod 10-23?", ["Jednostka na miejscu", "W drodze", "Cisza na radiu", "Powtórz"], "Jednostka na miejscu"),
    ("Radio", "Co oznacza kod 10-71?", ["Strzały", "Kradzież pojazdu", "Kolizja", "Osoba z bronią"], "Strzały"),
    ("Radio", "Co oznacza kod 10-72?", ["Sprzedaż narkotyków", "Strzały", "Napad", "Ucieczka z więzienia"], "Sprzedaż narkotyków"),
    ("Radio", "Co oznacza 10-13A?", ["Ranny funkcjonariusz — inne jednostki mogą dojechać", "Ranny funkcjonariusz — inne jednostki nie mogą dojechać", "Ranny strażnik więzienny", "Potrzebne wsparcie"], "Ranny funkcjonariusz — inne jednostki mogą dojechać"),
    ("Radio", "Co oznacza 10-13B?", ["Ranny funkcjonariusz — inne jednostki nie mogą dojechać", "Ranny funkcjonariusz — inne jednostki mogą dojechać", "Ranny strażnik więzienny", "Cisza na radiu"], "Ranny funkcjonariusz — inne jednostki nie mogą dojechać"),
    ("Radio", "Co oznacza 10-13C?", ["Ranny strażnik więzienny", "Ranny funkcjonariusz", "Ucieczka z więzienia", "Osoba z bronią"], "Ranny strażnik więzienny"),
    ("Radio", "Co oznacza CODE 3 w pojeździe?", ["Światła i syrena", "Tylko światła", "Brak sygnalizacji", "Tylko syrena"], "Światła i syrena"),
    ("Radio", "Co oznacza Status 10?", ["Procedury zatrzymania na komendzie", "Rozpoczęcie patrolu", "Zebranie na komendzie", "Prace biurowe"], "Procedury zatrzymania na komendzie"),
    ("Radio", "Kto wyznacza kanał taktyczny po zgłoszeniu np. 10-90?", ["PWC", "Dowolna jednostka", "U1", "Radio Operator bez konsultacji"], "PWC"),

    ("Patrole", "Kto rozdziela patrole podczas Statusu 5?", ["PWC", "U1", "Każdy funkcjonariusz sam", "JSD"], "PWC"),
    ("Patrole", "Kto w standardowym patrolu nadaje komunikaty radiowe, kody i statusy?", ["Pasażer — Radio Operator", "Kierowca", "PWC zawsze", "Supervisor zawsze"], "Pasażer — Radio Operator"),
    ("Patrole", "Jaki jest minimalny skład patrolu pieszego?", ["2 osoby", "1 osoba", "3 osoby", "4 osoby"], "2 osoby"),
    ("Patrole", "Jaki jest maksymalny skład patrolu pieszego?", ["4 osoby", "2 osoby", "3 osoby", "6 osób"], "4 osoby"),
    ("Patrole", "Kto może przydzielić jednostkę EDWARD?", ["PWC", "Każdy Senior Deputy", "U1", "Dowolny RO"], "PWC"),
    ("Patrole", "Jakie oznaczenie ma jednostka powietrzna?", ["EAGLE", "EDWARD", "DAVID", "TOM"], "EAGLE"),
    ("Patrole", "Jakie oznaczenie ma jednostka transportowa?", ["TOM", "ADAM", "FRANK", "OCEAN"], "TOM"),
    ("Patrole", "Jakie oznaczenie ma jednostka piesza?", ["FRANK", "LINCOLN", "MERRY", "DAVID"], "FRANK"),
    ("Patrole", "Jakie oznaczenie w USMS przypisano Special Operations Group?", ["DAVID", "ADAM", "EAGLE", "OCEAN"], "DAVID"),
    ("Patrole", "Od jakiego stopnia dopuszczono Solo MERRY na kodzie zielonym?", ["Senior Deputy U.S Marshal", "Deputy U.S Marshal Trainee", "Deputy U.S Marshal", "Lead Deputy U.S Marshal"], "Senior Deputy U.S Marshal"),
    ("Patrole", "Od jakiego stopnia dopuszczono Solo MERRY na kodzie pomarańczowym?", ["Lead Deputy U.S Marshal", "Senior Deputy U.S Marshal", "Special Deputy U.S Marshal", "Deputy U.S Marshal"], "Lead Deputy U.S Marshal"),
    ("Patrole", "Do jakiego zgłoszenia ma być przede wszystkim wykorzystywany Solo MERRY?", ["10-72", "10-90", "10-64", "10-50"], "10-72"),

    ("Pościgi", "Która jednostka jest najbliżej podejrzanego podczas pościgu?", ["U1", "U0", "U2", "U3"], "U1"),
    ("Pościgi", "Co oznacza U0 podczas pościgu?", ["Jednostkę powietrzną", "Pierwszy radiowóz", "Supervisora", "PWC"], "Jednostkę powietrzną"),
    ("Pościgi", "Czy jednostki mogą samodzielnie wprowadzać manewry pościgowe bez zgody PWC lub SV?", ["Nie", "Tak, zawsze", "Tylko U2", "Tylko po 5 minutach"], "Nie"),
    ("Pościgi", "Jaki kod pościgu obowiązuje standardowo po rozpoczęciu pościgu?", ["Zielony", "Żółty", "Czerwony", "Czarny"], "Zielony"),
    ("Pościgi", "Jaki manewr dochodzi do dostępnych możliwości na kodzie żółtym?", ["PIT", "RUCHOMA", "Ostrzał pojazdu bez ograniczeń", "Brak nowych manewrów"], "PIT"),
    ("Pościgi", "Czy manewr RUCHOMA jest dozwolony?", ["Nie", "Tak", "Tylko na kodzie zielonym", "Tylko przez EDWARD"], "Nie"),
    ("Pościgi", "Jaki kod otrzymuje pojazd osoby trzeciej aktywnie ingerujący w pościg przez zajeżdżanie drogi lub spowalnianie jednostek?", ["Żółty", "Zielony", "Czarny automatycznie", "Nie otrzymuje kodu"], "Żółty"),
    ("Pościgi", "Czy miejski kod zagrożenia i kod pościgowy pojazdu są tym samym systemem?", ["Nie", "Tak", "Tylko przy kodzie czerwonym", "Tylko podczas napadu"], "Nie"),
    ("Pościgi", "Co powinna zawierać ciągła radiówka U1?", ["Kierunek geograficzny, ulice/punkty i kierunek/pas pojazdu", "Wyłącznie lewo/prawo", "Tylko prędkość", "Tylko model pojazdu"], "Kierunek geograficzny, ulice/punkty i kierunek/pas pojazdu"),

    ("Konwoje", "Ilu funkcjonariuszy minimum wymaga konwój?", ["12", "8", "10", "16"], "12"),
    ("Konwoje", "Jaki jest przyjęty próg wyroku dla konwoju?", ["150 miesięcy", "50 miesięcy", "100 miesięcy", "200 miesięcy"], "150 miesięcy"),
    ("Konwoje", "Gdzie trafiają przedmioty zabrane zatrzymanemu podczas konwoju?", ["Do bagażnika pojazdu, którym jest przewożony", "Do dowolnego radiowozu", "Do kieszeni Supervisora", "Pozostają przy zatrzymanym"], "Do bagażnika pojazdu, którym jest przewożony"),
    ("Konwoje", "Jaka jest maksymalna prędkość konwoju w mieście według Kompendium?", ["80 km/h", "60 km/h", "100 km/h", "120 km/h"], "80 km/h"),
    ("Konwoje", "Jaka jest maksymalna prędkość konwoju na autostradzie według Kompendium?", ["120 km/h", "80 km/h", "100 km/h", "140 km/h"], "120 km/h"),
    ("Konwoje", "Jaką formację stosuje się na autostradzie podczas konwoju?", ["Szachownicę", "Pojedynczą linię zawsze", "Krąg", "Brak ustalonej formacji"], "Szachownicę"),
    ("Konwoje", "Czy uczestnik konwoju może użyć lekkiego wkładu balistycznego niezależnie od stopnia i kodu?", ["Tak", "Nie", "Tylko SOG", "Tylko High Command"], "Tak"),
    ("Konwoje", "Jak należy oznaczyć osobę skutecznie odbitą z konwoju?", ["ODBITA OSOBA Z KONWOJU", "UCIECZKA NTS", "WITSEC", "STATUS 11"], "ODBITA OSOBA Z KONWOJU"),

    ("JSD", "Za co odpowiada Judicial Security Division?", ["Bezpieczeństwo federalnego sądownictwa i rozpraw", "Wyłącznie pościgi", "Wyłącznie szkolenia", "Wyłącznie transport lotniczy"], "Bezpieczeństwo federalnego sądownictwa i rozpraw"),
    ("JSD", "Jaki jest minimalny stopień pozwalający dołączyć do JSD?", ["Deputy U.S Marshal", "Deputy U.S Marshal Trainee", "Senior Deputy U.S Marshal", "Lead Deputy U.S Marshal"], "Deputy U.S Marshal"),
    ("JSD", "Kto w JSD zajmuje się nadzorem i planowaniem bezpieczeństwa gmachu DOJ?", ["Senior Inspectors", "Court Security Officers wyłącznie", "PWC", "U1"], "Senior Inspectors"),
    ("JSD", "Od jakiego kodu każdy agent JSD na DOJ zakłada kamizelkę kuloodporną USMS?", ["Pomarańczowego", "Zielonego", "Czerwonego", "Czarnego"], "Pomarańczowego"),
    ("JSD", "Co jest priorytetem podczas incydentu na rozprawie?", ["Ewakuacja sędziego i ławników do bezpiecznej strefy", "Natychmiastowe opuszczenie budynku przez wszystkich agentów", "Przeniesienie akt", "Zamknięcie radia MAIN"], "Ewakuacja sędziego i ławników do bezpiecznej strefy"),
    ("JSD", "Kto dowodzi rozmieszczeniem agentów JSD podczas rozprawy?", ["Senior Inspector", "Dowolny Deputy", "PWC", "U0"], "Senior Inspector"),
    ("JSD", "Komu JSD przekazuje skazanego po doprowadzeniu go do cel zgodnie z procedurą?", ["SOG", "Training Division", "PWC", "GND"], "SOG"),
    ("JSD", "Co należy zrobić z osobą z publiczności przed dopuszczeniem na jawną rozprawę?", ["Wylegitymować, sprawdzić w bazie i przeszukać", "Tylko zapytać o nazwisko", "Wpuścić bez kontroli", "Wyłącznie sprawdzić bilet"], "Wylegitymować, sprawdzić w bazie i przeszukać"),
    ("JSD", "Czy przy sprawach wysokiego ryzyka przewidziano dodatkową linię kontroli przed salą?", ["Tak", "Nie", "Tylko dla funkcjonariuszy", "Tylko po rozprawie"], "Tak"),

    ("UoF", "Czy wolno użyć tasera wobec osoby zakutej?", ["Nie", "Tak", "Tylko dwa razy", "Tylko za zgodą PWC"], "Nie"),
    ("UoF", "Jaki jest limit użycia tasera wobec niezakutej i nieuzbrojonej osoby?", ["2 użycia", "1 użycie", "3 użycia", "Brak limitu"], "2 użycia"),
    ("UoF", "Co oznacza litera B w zasadzie BLOS?", ["Broń", "Bezpieczeństwo", "Barykada", "Balistyka"], "Broń"),
    ("UoF", "Co oznacza litera L w zasadzie BLOS?", ["Lufa", "Linia", "Lokalizacja", "Limit"], "Lufa"),
    ("UoF", "Co oznacza litera O w zasadzie BLOS?", ["Otoczenie", "Ostrzeżenie", "Ochrona", "Odległość"], "Otoczenie"),
    ("UoF", "Co oznacza litera S w zasadzie BLOS?", ["Spust", "Sektor", "Sygnał", "Stanowisko"], "Spust"),
    ("UoF", "Który poziom znajduje się bezpośrednio po 'Pasywnym oporze'?", ["Aktywny opór", "Współpracujący", "Agresywny opór", "Zwiększony agresywny opór"], "Aktywny opór"),

    ("Bean Bag", "Czy Bean Bag może być używany w pomieszczeniach zamkniętych?", ["Nie", "Tak", "Tylko przez SOG", "Tylko na kodzie czarnym"], "Nie"),
    ("Bean Bag", "Jaki jest minimalny dopuszczalny dystans użycia Bean Bag?", ["Więcej niż 3 metry", "1 metr", "Dokładnie 2 metry", "Brak minimum"], "Więcej niż 3 metry"),
    ("Bean Bag", "Ile maksymalnie razy można użyć Bean Bag wobec jednej osoby podczas jednej interwencji?", ["3", "1", "2", "5"], "3"),
    ("Bean Bag", "W które miejsce NIE należy celować Bean Bagiem?", ["Głowę", "Uda", "Pośladki", "Duże grupy mięśniowe"], "Głowę"),
    ("Bean Bag", "Co należy zrobić po każdym strzale z Bean Bag?", ["Ponownie ocenić zagrożenie", "Automatycznie oddać drugi strzał", "Zmienić operatora", "Zakończyć interwencję niezależnie od sytuacji"], "Ponownie ocenić zagrożenie"),
    ("Bean Bag", "Która sytuacja jest wyjątkiem pozwalającym rozważyć Bean Bag bez bezpośredniego zagrożenia życia?", ["Podejrzany wyraźnie zmierza do otwartych drzwi pojazdu, by uciec", "Osoba spokojnie stoi", "Ktoś odmawia podania nazwiska", "Kierowca przekroczył prędkość"], "Podejrzany wyraźnie zmierza do otwartych drzwi pojazdu, by uciec"),

    ("Szkolenia", "Jaki minimalny stopień obowiązuje dla NL I?", ["Deputy U.S Marshal", "Deputy U.S Marshal Trainee", "Senior Deputy U.S Marshal", "Special Deputy U.S Marshal"], "Deputy U.S Marshal"),
    ("Szkolenia", "Jaki dodatkowy wymóg poza NL I obowiązuje przy NL II?", ["Minimum 10 negocjacji jako negocjator", "Minimum 3 pościgi", "SOG", "JSD"], "Minimum 10 negocjacji jako negocjator"),
    ("Szkolenia", "Jaki minimalny stopień obowiązuje dla SEU?", ["Senior Deputy U.S Marshal", "Deputy U.S Marshal", "Lead Deputy U.S Marshal", "Chief of Staff"], "Senior Deputy U.S Marshal"),
    ("Szkolenia", "Jaki minimalny stopień obowiązuje dla ASU?", ["Senior Deputy U.S Marshal", "Deputy U.S Marshal Trainee", "Deputy U.S Marshal", "Special Deputy U.S Marshal"], "Senior Deputy U.S Marshal"),
    ("Szkolenia", "Do czego potrzebne jest szkolenie ASU?", ["Do obsługi jednostki EAGLE", "Do obsługi EDWARD", "Do prowadzenia negocjacji", "Do JSD"], "Do obsługi jednostki EAGLE"),
    ("Szkolenia", "Do czego uprawnia HAW wraz z wymaganymi zezwoleniami?", ["Do korzystania z broni klasy III", "Do prowadzenia rozpraw", "Do nadawania statusu 9", "Do przydzielania EDWARD"], "Do korzystania z broni klasy III"),
    ("Szkolenia", "Jaki minimalny stopień obowiązuje dla HAW?", ["Senior Deputy U.S Marshal", "Deputy U.S Marshal", "Lead Deputy U.S Marshal", "Deputy U.S Marshal Trainee"], "Senior Deputy U.S Marshal"),

    ("Struktura", "Który zakres odznak należy do Deputy U.S Marshal?", ["751–780", "731–750", "711–720", "781–799"], "751–780"),
    ("Struktura", "Który zakres odznak należy do Senior Deputy U.S Marshal?", ["731–750", "751–780", "721–730", "706–710"], "731–750"),
    ("Struktura", "Który zakres odznak należy do Special Deputy U.S Marshal?", ["721–730", "731–750", "711–720", "751–780"], "721–730"),
    ("Struktura", "Który zakres odznak należy do Lead Deputy U.S Marshal?", ["711–720", "721–730", "706–710", "731–750"], "711–720"),
    ("Struktura", "Które odznaki tworzą High Command w przyjętej strukturze?", ["701–705", "706–710", "701–710", "711–720"], "701–705"),
    ("Struktura", "Jaki minimalny stopień obowiązuje dla PWC?", ["Senior Deputy U.S Marshal", "Deputy U.S Marshal", "Special Deputy U.S Marshal", "Lead Deputy U.S Marshal"], "Senior Deputy U.S Marshal"),
    ("Struktura", "Kto prowadzi całość konkretnej akcji jako rola operacyjna?", ["Supervisor (SV)", "PWC zawsze", "Radio Operator", "U0"], "Supervisor (SV)"),
    ("Struktura", "Za co odpowiada Operations Commander?", ["Przygotowanie planu działania i raportowanie do Supervisora", "Przydzielanie numerów odznak", "Wyłącznie prowadzenie pojazdu", "Kontrolę DOJ"], "Przygotowanie planu działania i raportowanie do Supervisora"),

    ("Zatrzymanie", "Ile czasu na rozmowę telefoniczną przysługuje zatrzymanemu według procedury?", ["2 minuty", "1 minuta", "5 minut", "10 minut"], "2 minuty"),
    ("Zatrzymanie", "W jakim terminie można odwołać się od wyroku?", ["7 dni", "24 godziny", "14 dni", "30 dni"], "7 dni"),
    ("Zatrzymanie", "Do jakiego wymiaru kary zasadniczo możliwa jest kaucja, z uwzględnieniem przewidzianych wyjątków?", ["Do 50 miesięcy", "Do 20 miesięcy", "Do 100 miesięcy", "Bez limitu"], "Do 50 miesięcy"),
    ("Zatrzymanie", "Jaka jest minimalna stawka kaucji za miesiąc?", ["$1000", "$500", "$1500", "$3000"], "$1000"),
    ("Zatrzymanie", "Jaki kanał taktyczny przewidziano do procedur Status 10?", ["TAC 8", "TAC 1", "MAIN", "TAC 3"], "TAC 8"),

    ("Negocjacje", "Jakie określenie jest prawidłowe w negocjacjach?", ["Wolny odjazd", "Swobodny odjazd", "Zielone światło", "Bezwarunkowy odjazd"], "Wolny odjazd"),
    ("Negocjacje", "Kiedy omawia się wolny odjazd?", ["Na finalnym etapie negocjacji jako żądanie końcowe", "Na samym początku", "Przed nawiązaniem kontaktu", "Dopiero po pościgu"], "Na finalnym etapie negocjacji jako żądanie końcowe"),
    ("Negocjacje", "Po ilu ostrzeżeniach negocjatora może wystąpić przesłanka do zerwania żądań?", ["3", "1", "2", "5"], "3"),
]


def ensure_exam_tables():
    if not DATABASE_URL:
        return
    conn = pg_connect("usms-exam-init")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS exam_questions (
                    id BIGSERIAL PRIMARY KEY,
                    category TEXT NOT NULL,
                    question TEXT NOT NULL UNIQUE,
                    options JSONB NOT NULL,
                    correct_answer TEXT NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS exam_sessions (
                    id BIGSERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    opens_at TIMESTAMPTZ,
                    closes_at TIMESTAMPTZ,
                    question_count INTEGER NOT NULL DEFAULT 20,
                    duration_minutes INTEGER NOT NULL DEFAULT 20,
                    pass_percent INTEGER NOT NULL DEFAULT 80,
                    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    created_by BIGINT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS exam_overrides (
                    id BIGSERIAL PRIMARY KEY,
                    session_id BIGINT NOT NULL REFERENCES exam_sessions(id) ON DELETE CASCADE,
                    discord_id BIGINT NOT NULL,
                    opens_at TIMESTAMPTZ,
                    closes_at TIMESTAMPTZ,
                    max_attempts INTEGER NOT NULL DEFAULT 1,
                    updated_by BIGINT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(session_id, discord_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS exam_attempts (
                    id BIGSERIAL PRIMARY KEY,
                    session_id BIGINT NOT NULL REFERENCES exam_sessions(id) ON DELETE CASCADE,
                    discord_id BIGINT NOT NULL,
                    badge_number TEXT,
                    display_name TEXT,
                    attempt_no INTEGER NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    deadline_at TIMESTAMPTZ NOT NULL,
                    submitted_at TIMESTAMPTZ,
                    score INTEGER,
                    total INTEGER,
                    percent INTEGER,
                    passed BOOLEAN,
                    status TEXT NOT NULL DEFAULT 'in_progress',
                    UNIQUE(session_id, discord_id, attempt_no)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS exam_attempt_questions (
                    id BIGSERIAL PRIMARY KEY,
                    attempt_id BIGINT NOT NULL REFERENCES exam_attempts(id) ON DELETE CASCADE,
                    question_id BIGINT NOT NULL REFERENCES exam_questions(id),
                    position INTEGER NOT NULL,
                    question_text TEXT NOT NULL,
                    options JSONB NOT NULL,
                    correct_answer TEXT NOT NULL,
                    selected_answer TEXT,
                    is_correct BOOLEAN,
                    UNIQUE(attempt_id, position)
                )
            """)
            for category, question, options, correct in EXAM_QUESTIONS:
                cur.execute("""
                    INSERT INTO exam_questions(category, question, options, correct_answer)
                    VALUES (%s, %s, %s::jsonb, %s)
                    ON CONFLICT (question) DO NOTHING
                """, (category, question, json.dumps(options, ensure_ascii=False), correct))
        conn.commit()
        print(f"✅ WEB: system egzaminów gotowy ({len(EXAM_QUESTIONS)} pytań startowych).", flush=True)
    finally:
        conn.close()

try:
    ensure_exam_tables()
except Exception as exc:
    print(f"⚠️ WEB: nie udało się przygotować tabel egzaminów: {exc!r}", flush=True)


def exam_now():
    return datetime.now(timezone.utc)


def parse_exam_local(value):
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=EXAM_TZ)
    return dt.astimezone(timezone.utc)


def current_officer_identity():
    user = session.get("discord_user") or {}
    did = str(user.get("id", ""))
    badge = None
    name = user.get("username") or "Funkcjonariusz"
    try:
        for officer in load_officers():
            if str(officer.get("discord_id", "")) == did:
                badge = str(officer.get("badge") or "")
                name = officer.get("name") or name
                break
    except Exception:
        pass
    return did, badge, name


def get_exam_access(discord_id):
    if not DATABASE_URL:
        return None
    now = exam_now()
    conn = pg_connect("usms-exam-access")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT s.*,
                       o.opens_at AS override_opens_at,
                       o.closes_at AS override_closes_at,
                       o.max_attempts AS override_max_attempts,
                       (SELECT COUNT(*) FROM exam_attempts a
                         WHERE a.session_id=s.id AND a.discord_id=%s) AS attempts_used
                FROM exam_sessions s
                LEFT JOIN exam_overrides o ON o.session_id=s.id AND o.discord_id=%s
                WHERE s.is_enabled=TRUE
                  AND (
                    (s.opens_at IS NOT NULL AND s.closes_at IS NOT NULL AND %s BETWEEN s.opens_at AND s.closes_at)
                    OR
                    (o.opens_at IS NOT NULL AND o.closes_at IS NOT NULL AND %s BETWEEN o.opens_at AND o.closes_at)
                  )
                ORDER BY s.id DESC LIMIT 1
            """, (int(discord_id), int(discord_id), now, now))
            row = cur.fetchone()
            if not row:
                return None
            d = dict(row)
            max_attempts = d.get("override_max_attempts") or 1
            d["max_attempts"] = max_attempts
            d["can_start"] = int(d.get("attempts_used") or 0) < int(max_attempts)
            return d
    finally:
        conn.close()


def finalize_attempt(attempt_id, forced=False):
    conn = pg_connect("usms-exam-finalize")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM exam_attempts WHERE id=%s FOR UPDATE", (attempt_id,))
            attempt = cur.fetchone()
            if not attempt or attempt["status"] != "in_progress":
                conn.rollback()
                return dict(attempt) if attempt else None
            cur.execute("SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE is_correct=TRUE) AS score FROM exam_attempt_questions WHERE attempt_id=%s", (attempt_id,))
            result = cur.fetchone()
            total = int(result["total"] or 0)
            score = int(result["score"] or 0)
            percent = round((score / total) * 100) if total else 0
            cur.execute("SELECT pass_percent FROM exam_sessions WHERE id=%s", (attempt["session_id"],))
            pass_percent = int(cur.fetchone()["pass_percent"])
            cur.execute("""
                UPDATE exam_attempts
                SET submitted_at=NOW(), score=%s, total=%s, percent=%s, passed=%s, status='completed'
                WHERE id=%s RETURNING *
            """, (score, total, percent, percent >= pass_percent, attempt_id))
            out = dict(cur.fetchone())
        conn.commit()
        return out
    finally:
        conn.close()


@app.route("/egzamin")
@logged_in_required
def exam_home():
    did, badge, name = current_officer_identity()
    access = get_exam_access(did) if did.isdigit() else None
    history = []
    if DATABASE_URL and did.isdigit():
        conn = pg_connect("usms-exam-history")
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT a.*, s.title FROM exam_attempts a
                    JOIN exam_sessions s ON s.id=a.session_id
                    WHERE a.discord_id=%s ORDER BY a.id DESC LIMIT 20
                """, (int(did),))
                history = [dict(x) for x in cur.fetchall()]
        finally:
            conn.close()
    return render_template("exam_home.html", access=access, history=history, badge=badge, officer_name=name)


@app.route("/egzamin/start", methods=["POST"])
@logged_in_required
def exam_start():
    validate_csrf()
    did, badge, name = current_officer_identity()
    if not did.isdigit():
        abort(403)
    access = get_exam_access(did)
    if not access or not access.get("can_start"):
        return render_template("error.html", title="Egzamin niedostępny", message="Nie masz obecnie aktywnego terminu egzaminu albo wykorzystałeś dostępne podejścia."), 403
    conn = pg_connect("usms-exam-start")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS n FROM exam_attempts WHERE session_id=%s AND discord_id=%s", (access["id"], int(did)))
            attempt_no = int(cur.fetchone()["n"]) + 1
            deadline = exam_now() + timedelta(minutes=int(access["duration_minutes"]))
            cur.execute("""
                INSERT INTO exam_attempts(session_id, discord_id, badge_number, display_name, attempt_no, deadline_at)
                VALUES(%s,%s,%s,%s,%s,%s) RETURNING id
            """, (access["id"], int(did), badge, name, attempt_no, deadline))
            attempt_id = cur.fetchone()["id"]
            # Rozkładamy pytania po kategoriach, a resztę dobieramy losowo.
            cur.execute("SELECT DISTINCT category FROM exam_questions WHERE active=TRUE")
            categories = [r["category"] for r in cur.fetchall()]
            chosen = []
            per_category = 1 if len(categories) <= int(access["question_count"]) else 0
            if per_category:
                random.shuffle(categories)
                for cat in categories:
                    cur.execute("SELECT * FROM exam_questions WHERE active=TRUE AND category=%s ORDER BY random() LIMIT 1", (cat,))
                    r = cur.fetchone()
                    if r: chosen.append(dict(r))
            remaining = int(access["question_count"]) - len(chosen)
            ids = [q["id"] for q in chosen]
            if remaining > 0:
                if ids:
                    cur.execute("SELECT * FROM exam_questions WHERE active=TRUE AND NOT (id = ANY(%s)) ORDER BY random() LIMIT %s", (ids, remaining))
                else:
                    cur.execute("SELECT * FROM exam_questions WHERE active=TRUE ORDER BY random() LIMIT %s", (remaining,))
                chosen.extend(dict(r) for r in cur.fetchall())
            random.shuffle(chosen)
            for pos, q in enumerate(chosen, 1):
                opts = list(q["options"])
                random.shuffle(opts)
                cur.execute("""
                    INSERT INTO exam_attempt_questions(attempt_id, question_id, position, question_text, options, correct_answer)
                    VALUES(%s,%s,%s,%s,%s::jsonb,%s)
                """, (attempt_id, q["id"], pos, q["question"], json.dumps(opts, ensure_ascii=False), q["correct_answer"]))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("exam_take", attempt_id=attempt_id))


@app.route("/egzamin/<int:attempt_id>")
@logged_in_required
def exam_take(attempt_id):
    did = str((session.get("discord_user") or {}).get("id", ""))
    conn = pg_connect("usms-exam-take")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT a.*, s.title FROM exam_attempts a JOIN exam_sessions s ON s.id=a.session_id WHERE a.id=%s AND a.discord_id=%s", (attempt_id, int(did)))
            attempt = cur.fetchone()
            if not attempt: abort(404)
            if attempt["status"] != "in_progress":
                return redirect(url_for("exam_result", attempt_id=attempt_id))
            if exam_now() >= attempt["deadline_at"]:
                finalize_attempt(attempt_id, forced=True)
                return redirect(url_for("exam_result", attempt_id=attempt_id))
            cur.execute("SELECT id, position, question_text, options, selected_answer FROM exam_attempt_questions WHERE attempt_id=%s ORDER BY position", (attempt_id,))
            questions = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return render_template("exam_take.html", attempt=dict(attempt), questions=questions)


@app.route("/egzamin/<int:attempt_id>/submit", methods=["POST"])
@logged_in_required
def exam_submit(attempt_id):
    validate_csrf()
    did = str((session.get("discord_user") or {}).get("id", ""))
    conn = pg_connect("usms-exam-submit")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM exam_attempts WHERE id=%s AND discord_id=%s AND status='in_progress' FOR UPDATE", (attempt_id, int(did)))
            attempt = cur.fetchone()
            if not attempt: abort(404)
            cur.execute("SELECT id, correct_answer FROM exam_attempt_questions WHERE attempt_id=%s", (attempt_id,))
            for q in cur.fetchall():
                answer = request.form.get(f"q_{q['id']}")
                cur.execute("UPDATE exam_attempt_questions SET selected_answer=%s, is_correct=%s WHERE id=%s", (answer, bool(answer and answer == q["correct_answer"]), q["id"]))
        conn.commit()
    finally:
        conn.close()
    finalize_attempt(attempt_id)
    return redirect(url_for("exam_result", attempt_id=attempt_id))


@app.route("/egzamin/<int:attempt_id>/wynik")
@logged_in_required
def exam_result(attempt_id):
    did = str((session.get("discord_user") or {}).get("id", ""))
    conn = pg_connect("usms-exam-result")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT a.*, s.title, s.pass_percent FROM exam_attempts a JOIN exam_sessions s ON s.id=a.session_id WHERE a.id=%s AND a.discord_id=%s", (attempt_id, int(did)))
            attempt = cur.fetchone()
            if not attempt: abort(404)
            if attempt["status"] == "in_progress":
                return redirect(url_for("exam_take", attempt_id=attempt_id))
    finally:
        conn.close()
    return render_template("exam_result.html", attempt=dict(attempt))


@app.route("/egzamin/admin")
@admin_required
def exam_admin():
    sessions = []
    officers = []
    if DATABASE_URL:
        conn = pg_connect("usms-exam-admin")
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT s.*,
                      (SELECT COUNT(*) FROM exam_attempts a WHERE a.session_id=s.id) attempts,
                      (SELECT COUNT(*) FROM exam_attempts a WHERE a.session_id=s.id AND a.passed=TRUE) passed_count
                    FROM exam_sessions s ORDER BY s.id DESC LIMIT 20
                """)
                sessions = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    try:
        officers = load_officers()
    except Exception:
        officers = []
    return render_template("exam_admin.html", sessions=sessions, officers=officers, now_local=datetime.now(EXAM_TZ))


@app.route("/egzamin/admin/create", methods=["POST"])
@admin_required
def exam_admin_create():
    validate_csrf()
    user_id = int(session["discord_user"]["id"])
    title = (request.form.get("title") or "Egzamin z Kompendium").strip()[:120]
    mode = request.form.get("mode")
    if mode == "now":
        opens = exam_now(); closes = opens + timedelta(minutes=int(request.form.get("window_minutes") or 30))
    else:
        opens = parse_exam_local(request.form.get("opens_at")); closes = parse_exam_local(request.form.get("closes_at"))
        if not opens or not closes or closes <= opens:
            return render_template("error.html", title="Błędny termin", message="Podaj prawidłową datę rozpoczęcia i zakończenia sesji."), 400
    conn = pg_connect("usms-exam-create")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO exam_sessions(title, opens_at, closes_at, question_count, duration_minutes, pass_percent, created_by)
                VALUES(%s,%s,%s,%s,%s,%s,%s)
            """, (title, opens, closes, EXAM_QUESTION_COUNT, EXAM_DURATION_MINUTES, EXAM_PASS_PERCENT, user_id))
        conn.commit()
    finally: conn.close()
    return redirect(url_for("exam_admin"))


@app.route("/egzamin/admin/session/<int:session_id>/close", methods=["POST"])
@admin_required
def exam_admin_close(session_id):
    validate_csrf()
    conn = pg_connect("usms-exam-close")
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE exam_sessions SET closes_at=NOW() WHERE id=%s", (session_id,))
        conn.commit()
    finally: conn.close()
    return redirect(url_for("exam_admin"))


@app.route("/egzamin/admin/session/<int:session_id>")
@admin_required
def exam_admin_session(session_id):
    conn = pg_connect("usms-exam-session")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM exam_sessions WHERE id=%s", (session_id,)); exam_session = cur.fetchone()
            if not exam_session: abort(404)
            cur.execute("SELECT * FROM exam_attempts WHERE session_id=%s ORDER BY id DESC", (session_id,)); attempts = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM exam_overrides WHERE session_id=%s ORDER BY updated_at DESC", (session_id,)); overrides = [dict(r) for r in cur.fetchall()]
    finally: conn.close()
    try: officers = load_officers()
    except Exception: officers = []
    return render_template("exam_admin_session.html", exam_session=dict(exam_session), attempts=attempts, overrides=overrides, officers=officers)


@app.route("/egzamin/admin/session/<int:session_id>/override", methods=["POST"])
@admin_required
def exam_admin_override(session_id):
    validate_csrf()
    discord_id = (request.form.get("discord_id") or "").strip()
    action = request.form.get("action")
    if not discord_id.isdigit(): abort(400)
    admin_id = int(session["discord_user"]["id"])
    conn = pg_connect("usms-exam-override")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM exam_overrides WHERE session_id=%s AND discord_id=%s", (session_id, int(discord_id)))
            existing = cur.fetchone()
            if action == "schedule":
                opens = parse_exam_local(request.form.get("opens_at")); closes = parse_exam_local(request.form.get("closes_at"))
                if not opens or not closes or closes <= opens: abort(400)
                max_attempts = int(existing["max_attempts"]) if existing else 1
            elif action == "open_now":
                opens = exam_now(); closes = opens + timedelta(minutes=30)
                max_attempts = int(existing["max_attempts"]) if existing else 1
            elif action == "extra_attempt":
                opens = existing["opens_at"] if existing else exam_now()
                closes = existing["closes_at"] if existing and existing["closes_at"] and existing["closes_at"] > exam_now() else exam_now() + timedelta(minutes=30)
                cur.execute("SELECT COUNT(*) AS n FROM exam_attempts WHERE session_id=%s AND discord_id=%s", (session_id, int(discord_id)))
                used = int(cur.fetchone()["n"])
                max_attempts = max(int(existing["max_attempts"]) if existing else 1, used + 1)
            else:
                abort(400)
            cur.execute("""
                INSERT INTO exam_overrides(session_id, discord_id, opens_at, closes_at, max_attempts, updated_by)
                VALUES(%s,%s,%s,%s,%s,%s)
                ON CONFLICT(session_id, discord_id) DO UPDATE SET
                    opens_at=EXCLUDED.opens_at, closes_at=EXCLUDED.closes_at,
                    max_attempts=EXCLUDED.max_attempts, updated_by=EXCLUDED.updated_by, updated_at=NOW()
            """, (session_id, int(discord_id), opens, closes, max_attempts, admin_id))
        conn.commit()
    finally: conn.close()
    return redirect(url_for("exam_admin_session", session_id=session_id))
