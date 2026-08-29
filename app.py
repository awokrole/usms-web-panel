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
    )



@app.route("/funkcjonariusze/<badge>/zdjecie", methods=["POST"])
@admin_required
def upload_officer_photo(badge):
    validate_csrf()

    # Sprawdź, czy profil istnieje w rosterze.
    if not any(str(x["badge"]) == str(badge) for x in load_officers()):
        abort(404)

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
@admin_required
def upload_officer_document(badge):
    validate_csrf()

    if not any(str(x["badge"]) == str(badge) for x in load_officers()):
        abort(404)

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
