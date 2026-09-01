import json
import os
import secrets
import sqlite3
import io
from datetime import datetime, timezone, timedelta

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    RealDictCursor = None
from functools import wraps

import requests
from flask import Flask, abort, redirect, render_template, request, send_file, session, url_for, g
from markupsafe import Markup, escape


app = Flask(__name__)
app.secret_key = os.environ.get("WEB_SECRET_KEY") or secrets.token_hex(32)


def discord_markdown(value):
    """Bezpieczny renderer podstawowego Discord Markdown do podglądu na stronie."""
    import re
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    safe = str(escape(text))

    # Chroń bloki i kod inline przed dalszym formatowaniem.
    protected = []
    def protect_code(match, block=False):
        raw = match.group(1)
        token = f"@@USMSCODE{len(protected)}@@"
        cls = "discord-code-block" if block else "discord-inline-code"
        protected.append(f'<code class="{cls}">{raw}</code>' if not block else f'<pre class="{cls}"><code>{raw}</code></pre>')
        return token

    safe = re.sub(r"```(?:[A-Za-z0-9_+.-]+)?\n?(.*?)```", lambda m: protect_code(m, True), safe, flags=re.S)
    safe = re.sub(r"`([^`\n]+)`", lambda m: protect_code(m, False), safe)

    # Klialne linki i czytelne skróty wzmianek na WWW. Discord dostaje surowy tekst
    # i zamienia @odznaka/@usms na prawdziwe pingi dopiero podczas wysyłki.
    safe = re.sub(
        r"(?i)\b(https?://[^\s<]+)",
        lambda m: f'<a class="discord-link" href="{m.group(1)}" target="_blank" rel="noopener noreferrer">{m.group(1)}</a>',
        safe,
    )
    # Na stronie pokazuj wzmiankę funkcjonariusza czytelnie jako [ODZNAKA] Imię Nazwisko.
    # Surowy zapis @721 pozostaje w bazie i dopiero przy wysyłce na Discord jest
    # zamieniany na prawdziwe <@discord_id>, więc ping nadal działa.
    try:
        mention_labels = getattr(g, "_usms_mention_labels", None)
        if mention_labels is None:
            mention_labels = {
                str(o.get("badge") or "").strip(): str(o.get("full_name") or "").strip()
                for o in load_officers()
                if str(o.get("badge") or "").strip()
            }
            g._usms_mention_labels = mention_labels
    except Exception:
        mention_labels = {}

    def render_person_mention(match):
        badge = match.group(1)
        name = mention_labels.get(badge)
        label = f"[{badge}] {name}" if name else f"[{badge}]"
        return f'<span class="discord-mention">{label}</span>'

    safe = re.sub(r"(?<![A-Za-z0-9_])@(\d{3})(?!\d)", render_person_mention, safe)
    safe = re.sub(r"(?i)(?<![A-Za-z0-9_])@usms\b", r'<span class="discord-mention discord-role-mention">@USMS</span>', safe)

    # Formatowanie inline zgodne z najczęściej używanym Discord Markdown.
    safe = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)
    safe = re.sub(r"__(.+?)__", r"<u>\1</u>", safe)
    safe = re.sub(r"~~(.+?)~~", r"<s>\1</s>", safe)
    safe = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<em>\1</em>", safe)

    out = []
    in_ul = False
    in_ol = False
    for line in safe.split("\n"):
        h = re.match(r"^(#{1,3})\s+(.+)$", line)
        ul = re.match(r"^[-*]\s+(.+)$", line)
        ol = re.match(r"^\d+[.)]\s+(.+)$", line)
        quote = re.match(r"^&gt;\s?(.*)$", line)

        if not ul and in_ul:
            out.append("</ul>"); in_ul = False
        if not ol and in_ol:
            out.append("</ol>"); in_ol = False

        if h:
            level = len(h.group(1))
            out.append(f'<h{level} class="discord-h{level}">{h.group(2)}</h{level}>')
        elif ul:
            if not in_ul:
                out.append('<ul class="discord-list">'); in_ul = True
            out.append(f"<li>{ul.group(1)}</li>")
        elif ol:
            if not in_ol:
                out.append('<ol class="discord-list">'); in_ol = True
            out.append(f"<li>{ol.group(1)}</li>")
        elif quote:
            out.append(f'<blockquote class="discord-quote">{quote.group(1) or "&nbsp;"}</blockquote>')
        elif line.strip() == "":
            out.append('<div class="discord-spacer"></div>')
        elif line.startswith("@@USMSCODE") and line.endswith("@@"):
            out.append(line)
        else:
            out.append(f'<div class="discord-line">{line}</div>')

    if in_ul: out.append("</ul>")
    if in_ol: out.append("</ol>")
    html = "".join(out)
    for idx, fragment in enumerate(protected):
        html = html.replace(f"@@USMSCODE{idx}@@", fragment)
    return Markup(html)


app.jinja_env.filters["discord_markdown"] = discord_markdown

DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "").strip()
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "").strip()
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")


DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
DB_PATH = (os.environ.get("DB_PATH") or "").strip()


if DATABASE_URL:
    print("🐘 WEB: DATABASE_URL wykryte — panel będzie czytał PostgreSQL.", flush=True)
elif DB_PATH:
    print(f"🗄️ WEB: PostgreSQL nieustawiony — fallback SQLite: {DB_PATH}", flush=True)
else:
    print("⚠️ WEB: Brak DATABASE_URL i DB_PATH — stan służby będzie pusty.", flush=True)


DISCORD_API = "https://discord.com/api/v10"

# Kanały, na które administrator może publikować z panelu WEB.
WEB_ANNOUNCEMENT_CHANNELS = {
    "1511317009466396716": "Ogłoszenia ogólne",
    "1524410439381811240": "Ogólne informacje",
    "1541198053254627488": "Status 9",
}


PROFILE_PHOTO_MAX_BYTES = 5 * 1024 * 1024
DOCUMENT_MAX_BYTES = 10 * 1024 * 1024
DOCUMENT_MAX_COUNT = 6
ALLOWED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp"}



def parse_role_ids(value: str):
    result = set()
    for part in (value or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            result.add(int(part))
    return result


WEB_ADMIN_ROLE_IDS = parse_role_ids(os.environ.get("WEB_ADMIN_ROLE_IDS", ""))
# Rola Discord inspekcji — dostęp tylko do kontroli dokumentów pracowników.
INSPECTION_ROLE_ID = int(os.environ.get("WEB_INSPECTION_ROLE_ID", "1511317008627667034"))



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


RANK_RANGES = [
    (701, 701, "U.S Marshal"),
    (702, 702, "Chief Deputy U.S Marshal Service"),
    (703, 703, "Asisstant Chief Deputy U.S Marshal"),
    (704, 704, "Associate Chief Deputy U.S Marshal"),
    (705, 705, "Chief of Staff"),
    (706, 710, "Supervisiory U.S Marshal"),
    (711, 720, "Lead Deputy U.S Marshal"),
    (721, 730, "Special Deputy U.S Marshal"),
    (731, 750, "Senior Deputy U.S Marshal"),
    (751, 780, "Deputy U.S Marshal"),
    (781, 799, "Deputy U.S Marshal Trainee"),
]


def rank_for_badge(badge, fallback="Brak"):
    try:
        number = int(str(badge).strip())
    except Exception:
        return fallback or "Brak"
    for start, end, rank in RANK_RANGES:
        if start <= number <= end:
            return rank
    return fallback or "Brak"


def ensure_roster_tables():
    """Tworzy PostgreSQL jako źródło danych kadrowych panelu i wykonuje jednorazowy import snapshotu XLSX."""
    if not DATABASE_URL:
        return

    conn = pg_connect("usms-roster-init")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS officers (
                    discord_id BIGINT PRIMARY KEY,
                    badge_number TEXT,
                    rank TEXT NOT NULL DEFAULT '',
                    full_name TEXT NOT NULL,
                    csn TEXT NOT NULL DEFAULT '',
                    vacation_start DATE NULL,
                    vacation_end DATE NULL,
                    suspended BOOLEAN NOT NULL DEFAULT FALSE,
                    suspension_until TIMESTAMPTZ NULL,
                    suspension_reason TEXT NULL,
                    last_promotion DATE NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    hired_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    terminated_at TIMESTAMPTZ NULL,
                    termination_reason TEXT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            for column, definition in {
                "vacation_end": "DATE NULL",
                "suspension_until": "TIMESTAMPTZ NULL",
                "suspension_reason": "TEXT NULL",
                "hired_at": "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
                "terminated_at": "TIMESTAMPTZ NULL",
                "termination_reason": "TEXT NULL",
            }.items():
                cur.execute(f"ALTER TABLE officers ADD COLUMN IF NOT EXISTS {column} {definition}")
            # Unikalność dotyczy aktywnych odznak; była osoba może zachować badge w historii.
            cur.execute("DROP INDEX IF EXISTS idx_officers_badge_unique")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_officers_badge_active_unique ON officers (badge_number) WHERE active=TRUE AND badge_number IS NOT NULL AND BTRIM(badge_number) <> ''")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS officer_trainings (
                    discord_id BIGINT NOT NULL,
                    training_code TEXT NOT NULL,
                    completed BOOLEAN NOT NULL DEFAULT TRUE,
                    granted_by BIGINT NULL,
                    granted_at TIMESTAMPTZ NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (discord_id, training_code)
                )
            """)
            cur.execute("ALTER TABLE officer_trainings ADD COLUMN IF NOT EXISTS granted_by BIGINT NULL")
            cur.execute("ALTER TABLE officer_trainings ADD COLUMN IF NOT EXISTS granted_at TIMESTAMPTZ NULL")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS officer_history (
                    id BIGSERIAL PRIMARY KEY,
                    discord_id BIGINT NOT NULL,
                    event_type TEXT NOT NULL,
                    old_value TEXT NULL,
                    new_value TEXT NULL,
                    reason TEXT NULL,
                    actor_id BIGINT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_officer_history_user ON officer_history(discord_id, created_at DESC)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS training_history (
                    id BIGSERIAL PRIMARY KEY,
                    discord_id BIGINT NOT NULL,
                    training_code TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor_id BIGINT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_training_history_user ON training_history(discord_id, created_at DESC)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS record_history (
                    id BIGSERIAL PRIMARY KEY,
                    discord_id BIGINT NOT NULL,
                    record_type TEXT NOT NULL,
                    old_count INTEGER NOT NULL,
                    new_count INTEGER NOT NULL,
                    reason TEXT NULL,
                    actor_id BIGINT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_record_history_user ON record_history(discord_id, created_at DESC)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS officer_records (
                    discord_id BIGINT PRIMARY KEY,
                    plus_count INTEGER NOT NULL DEFAULT 0,
                    minus_count INTEGER NOT NULL DEFAULT 0,
                    praise_count INTEGER NOT NULL DEFAULT 0,
                    reprimand_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payroll_entries (
                    id BIGSERIAL PRIMARY KEY,
                    discord_id BIGINT NULL,
                    badge_snapshot TEXT NOT NULL,
                    rank_snapshot TEXT NOT NULL DEFAULT '',
                    name_snapshot TEXT NOT NULL DEFAULT '',
                    period_key TEXT NOT NULL,
                    period_label TEXT NOT NULL,
                    hours NUMERIC(10,2) NOT NULL DEFAULT 0,
                    amount NUMERIC(12,2) NOT NULL DEFAULT 0,
                    received BOOLEAN NOT NULL DEFAULT FALSE,
                    is_history BOOLEAN NOT NULL DEFAULT FALSE,
                    imported_from TEXT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (period_key, badge_snapshot)
                )
            """)
            cur.execute("ALTER TABLE payroll_entries ADD COLUMN IF NOT EXISTS multiplier NUMERIC(4,2) NOT NULL DEFAULT 1.00")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS payroll_settings (
                    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                    multiplier NUMERIC(4,2) NOT NULL DEFAULT 1.00,
                    updated_by BIGINT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                INSERT INTO payroll_settings (id, multiplier) VALUES (1, 1.00)
                ON CONFLICT (id) DO NOTHING
            """)

            # v48 — ręczne ogłoszenia oraz Status 9 publikowane z panelu WWW.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS web_announcements (
                    id BIGSERIAL PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('announcement', 'status9')),
                    content TEXT NOT NULL,
                    author_id BIGINT NULL,
                    author_name TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_web_announcements_kind_created ON web_announcements(kind, created_at DESC)")
            # v53 — powiązanie wpisu WWW z wiadomościami Discorda, aby można było
            # edytować i usuwać dokładnie te same wiadomości po publikacji.
            cur.execute("ALTER TABLE web_announcements ADD COLUMN IF NOT EXISTS discord_channel_id TEXT NULL")
            cur.execute("ALTER TABLE web_announcements ADD COLUMN IF NOT EXISTS discord_message_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[]")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS web_migrations (
                    migration_key TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    details TEXT NULL
                )
            """)
            # Jednorazowe podpięcie ostatniego Statusu 9 opublikowanego przed v53.
            # Podane przez administratora wiadomości są dwoma fragmentami jednego wpisu.
            legacy_key = "v53-link-legacy-status9-1543886109308755968"
            cur.execute("SELECT 1 FROM web_migrations WHERE migration_key=%s", (legacy_key,))
            if cur.fetchone() is None:
                cur.execute("""
                    UPDATE web_announcements
                    SET discord_channel_id = %s,
                        discord_message_ids = ARRAY[%s,%s]::TEXT[]
                    WHERE id = (
                        SELECT id FROM web_announcements
                        WHERE kind='status9'
                          AND (discord_message_ids IS NULL OR cardinality(discord_message_ids)=0)
                        ORDER BY created_at DESC
                        LIMIT 1
                    )
                """, ("1541198053254627488", "1543886109308755968", "1543886110848196628"))
                cur.execute(
                    "INSERT INTO web_migrations (migration_key, details) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (legacy_key, "Podpięto ostatni Status 9 do dwóch wiadomości Discord na kanale Status 9."),
                )

            cur.execute("SELECT 1 FROM web_migrations WHERE migration_key=%s", ("database-usms-xlsx-2026-08-29",))
            already = cur.fetchone() is not None
            if not already:
                seed_path = os.path.join(os.path.dirname(__file__), "seed_database_usms.json")
                if os.path.exists(seed_path):
                    with open(seed_path, "r", encoding="utf-8") as fh:
                        seed = json.load(fh)
                    for officer in seed.get("officers", []):
                        cur.execute("""
                            INSERT INTO officers (discord_id, badge_number, rank, full_name, csn, vacation_start, suspended, last_promotion, active)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,TRUE)
                            ON CONFLICT (discord_id) DO UPDATE SET
                                badge_number=EXCLUDED.badge_number,
                                rank=EXCLUDED.rank,
                                full_name=EXCLUDED.full_name,
                                csn=EXCLUDED.csn,
                                vacation_start=EXCLUDED.vacation_start,
                                suspended=EXCLUDED.suspended,
                                last_promotion=EXCLUDED.last_promotion,
                                active=TRUE,
                                updated_at=NOW()
                        """, (
                            int(officer["discord_id"]), str(officer.get("badge") or ""), officer.get("rank") or "",
                            officer.get("full_name") or "", officer.get("csn") or "", officer.get("vacation_start"),
                            bool(officer.get("suspended")), officer.get("last_promotion"),
                        ))
                        for code in officer.get("trainings", []):
                            cur.execute("""
                                INSERT INTO officer_trainings (discord_id, training_code, completed)
                                VALUES (%s,%s,TRUE)
                                ON CONFLICT (discord_id, training_code) DO UPDATE SET completed=TRUE, updated_at=NOW()
                            """, (int(officer["discord_id"]), str(code)))
                        rec = officer.get("records") or {}
                        cur.execute("""
                            INSERT INTO officer_records (discord_id, plus_count, minus_count, praise_count, reprimand_count)
                            VALUES (%s,%s,%s,%s,%s)
                            ON CONFLICT (discord_id) DO UPDATE SET
                                plus_count=EXCLUDED.plus_count, minus_count=EXCLUDED.minus_count,
                                praise_count=EXCLUDED.praise_count, reprimand_count=EXCLUDED.reprimand_count,
                                updated_at=NOW()
                        """, (int(officer["discord_id"]), int(rec.get("plus",0)), int(rec.get("minus",0)), int(rec.get("praise",0)), int(rec.get("reprimand",0))))
                    for row in seed.get("payroll", []):
                        cur.execute("""
                            INSERT INTO payroll_entries (
                                discord_id, badge_snapshot, rank_snapshot, name_snapshot, period_key, period_label,
                                hours, amount, received, is_history, imported_from
                            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (period_key, badge_snapshot) DO NOTHING
                        """, (
                            row.get("discord_id"), str(row.get("badge") or ""), row.get("rank") or "", row.get("full_name") or "",
                            row.get("period_key") or "", row.get("period_label") or "", float(row.get("hours") or 0),
                            float(row.get("amount") or 0), bool(row.get("received")), bool(row.get("history")), seed.get("source")
                        ))
                    cur.execute("INSERT INTO web_migrations (migration_key, details) VALUES (%s,%s)", (
                        "database-usms-xlsx-2026-08-29",
                        f"Zaimportowano {len(seed.get('officers', []))} funkcjonariuszy i {len(seed.get('payroll', []))} pozycji wypłat z lokalnego snapshotu."
                    ))
                    print("✅ WEB: jednorazowy import DATABASE USMS.xlsx do PostgreSQL zakończony.", flush=True)
                else:
                    print("⚠️ WEB: brak seed_database_usms.json — pominięto import początkowy.", flush=True)
        conn.commit()
        print("✅ WEB: tabele kadrowe PostgreSQL gotowe.", flush=True)
    finally:
        conn.close()


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
    ensure_roster_tables()
except Exception as exc:
    print(f"⚠️ WEB: nie udało się przygotować danych kadrowych PostgreSQL: {exc!r}", flush=True)

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


def inspection_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "discord_user" not in session:
            return redirect(url_for("login"))
        if not (session.get("is_inspection", False) or session.get("is_admin", False)):
            return render_template("error.html", title="Brak dostępu",
                                   message="Ta sekcja jest dostępna dla Inspekcji i administracji."), 403
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


def checkbox_to_bool(value):
    return str(value or "").strip().casefold() in {"true", "prawda", "1", "yes", "tak"}


def normalize_badge(value):
    value = str(value or "").strip()
    if value.endswith(".0"):
        value = value[:-2]
    return value


def load_officers():
    """Ładuje listę funkcjonariuszy wyłącznie z PostgreSQL. Google Sheets nie jest używane."""
    if not DATABASE_URL:
        return []

    conn = pg_connect("usms-load-officers")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # `officers.active` jest jedynym źródłem prawdy o zatrudnieniu dla WWW.
            # Tabela `users` przechowuje stan służby bota i może dostarczyć aktualną
            # odznakę, ale brak odznaki w `users` NIE ukrywa aktywnego funkcjonariusza.
            cur.execute("""
                SELECT
                    o.discord_id,
                    o.badge_number AS seeded_badge,
                    o.rank AS seeded_rank,
                    o.full_name,
                    o.csn,
                    o.active,
                    o.last_promotion,
                    o.hired_at,
                    u.user_id AS bot_user_id,
                    CAST(u.badge_number AS TEXT) AS bot_badge
                FROM officers o
                LEFT JOIN users u ON u.user_id = o.discord_id
                WHERE o.active = TRUE
            """)
            base_rows = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT discord_id, training_code FROM officer_trainings WHERE completed=TRUE ORDER BY training_code")
            training_rows = cur.fetchall()
            cur.execute("SELECT discord_id, plus_count, minus_count, praise_count, reprimand_count FROM officer_records")
            record_rows = cur.fetchall()
    finally:
        conn.close()

    trainings = {}
    for row in training_rows:
        trainings.setdefault(int(row["discord_id"]), []).append(row["training_code"])
    records = {int(r["discord_id"]): dict(r) for r in record_rows}

    result = []
    for row in base_rows:
        discord_id = int(row["discord_id"])
        badge = normalize_badge(row.get("bot_badge") or row.get("seeded_badge"))
        rec = records.get(discord_id, {})
        result.append({
            "rank": rank_for_badge(badge, row.get("seeded_rank") or "Brak"),
            "badge": badge or "Brak",
            "full_name": row.get("full_name") or f"Discord {discord_id}",
            "csn": row.get("csn") or "",
            "discord_id": discord_id,
            "trainings": trainings.get(discord_id, []),
            "plus": int(rec.get("plus_count") or 0),
            "minus": int(rec.get("minus_count") or 0),
            "praise": int(rec.get("praise_count") or 0),
            "reprimand": int(rec.get("reprimand_count") or 0),
            "last_promotion": row.get("last_promotion"),
            "hired_at": row.get("hired_at"),
        })

    def key(item):
        try:
            return int(item["badge"])
        except Exception:
            return 999999
    return sorted(result, key=key)


def load_exam_officers():
    return [
        {"discord_id": int(o["discord_id"]), "badge": str(o["badge"]), "full_name": o["full_name"]}
        for o in load_officers()
    ]


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
        "is_inspection": session.get("is_inspection", False),
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
    member_roles = {int(x) for x in member.get("roles", []) if str(x).isdigit()}
    session["is_inspection"] = INSPECTION_ROLE_ID in member_roles

    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def get_payroll_multiplier():
    if not DATABASE_URL:
        return 1.0
    try:
        conn = pg_connect("payroll-multiplier")
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT multiplier FROM payroll_settings WHERE id = 1")
                row = cur.fetchone()
                return float(row[0]) if row else 1.0
        finally:
            conn.close()
    except Exception:
        return 1.0


def get_current_officer():
    user = session.get("discord_user") or {}
    try:
        discord_id = int(user.get("id"))
    except (TypeError, ValueError):
        return None
    try:
        return next((o for o in load_officers() if int(o.get("discord_id")) == discord_id), None)
    except Exception:
        return None


def current_payroll_period():
    # Tydzień pracy USMS: niedziela–sobota.
    today = datetime.now(timezone.utc).date()
    days_since_sunday = (today.weekday() + 1) % 7
    start = today - timedelta(days=days_since_sunday)
    end = start + timedelta(days=6)
    return start, end, f"{start.isoformat()}_{end.isoformat()}", f"{start.strftime('%d.%m.%Y')} – {end.strftime('%d.%m.%Y')}"


def load_web_announcements(kind=None, limit=20):
    if not DATABASE_URL:
        return []
    conn = pg_connect("usms-web-announcements-load")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if kind in {"announcement", "status9"}:
                cur.execute(
                    "SELECT id, kind, content, author_id, author_name, created_at, discord_channel_id, discord_message_ids FROM web_announcements WHERE kind=%s ORDER BY created_at DESC LIMIT %s",
                    (kind, int(limit)),
                )
            else:
                cur.execute(
                    "SELECT id, kind, content, author_id, author_name, created_at, discord_channel_id, discord_message_ids FROM web_announcements ORDER BY created_at DESC LIMIT %s",
                    (int(limit),),
                )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


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

        # PostgreSQL zwraca vacation_end jako datetime.date, a suspension_until
        # jako datetime/datetime-like. Starsza wersja dashboardu próbowała oba
        # pola parsować wyłącznie przez datetime.fromisoformat(raw), przez co
        # aktywny urlop typu DATE wpadał do except i nie był liczony.
        raw_suspension = state.get("suspension_until")
        if raw_suspension:
            try:
                dt = _as_utc_datetime(raw_suspension)
                if dt and dt > now:
                    suspended_count += 1
            except Exception:
                pass

        raw_vacation = state.get("vacation_end")
        if raw_vacation:
            try:
                if isinstance(raw_vacation, datetime):
                    end_date = raw_vacation.date()
                elif hasattr(raw_vacation, "year") and hasattr(raw_vacation, "month") and hasattr(raw_vacation, "day"):
                    # datetime.date z PostgreSQL.
                    end_date = raw_vacation
                else:
                    end_date = datetime.fromisoformat(str(raw_vacation)).date()
                # Urlop jest aktywny także w dniu wskazanym jako vacation_end.
                if end_date >= now.date():
                    vacation_count += 1
            except Exception:
                pass

    active = sorted(active, key=lambda x: x["duty"].get("current_shift_seconds", 0), reverse=True)

    my_officer = get_current_officer()
    my_profile_meta = get_profile_meta(my_officer["badge"]) if my_officer else None

    try:
        dashboard_announcements = load_web_announcements("announcement", 6)
        status9_rows = load_web_announcements("status9", 1)
        dashboard_status9 = status9_rows[0] if status9_rows else None
    except Exception:
        dashboard_announcements = []
        dashboard_status9 = None

    return render_template(
        "dashboard.html",
        officers=officers,
        active=active[:8],
        vacation_count=vacation_count,
        suspended_count=suspended_count,
        weekly_hours=round(weekly_total_seconds / 3600, 1),
        lifetime_hours=round(lifetime_total_seconds / 3600, 1),
        sheet_error=sheet_error,
        payroll_multiplier=get_payroll_multiplier(),
        my_officer=my_officer,
        my_profile_meta=my_profile_meta,
        dashboard_announcements=dashboard_announcements,
        dashboard_status9=dashboard_status9,
    )


@app.route("/inspekcja/dokumenty")
@inspection_required
def inspection_documents():
    officers_list = load_officers()
    q = request.args.get("q", "").strip().casefold()
    rows = []
    for officer in officers_list:
        docs = get_officer_documents(officer["badge"])
        row = dict(officer)
        row["document_count"] = len(docs)
        rows.append(row)
    if q:
        rows = [r for r in rows if q in str(r.get("full_name", "")).casefold()
                or q in str(r.get("badge", "")).casefold()
                or q in str(r.get("rank", "")).casefold()]
    rows.sort(key=lambda r: str(r.get("badge", "")))
    return render_template("inspection_documents.html", officers=rows, q=request.args.get("q", ""))


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

    rank_groups = []
    for _start, _end, rank_name in RANK_RANGES:
        members = [r for r in records if r.get("rank") == rank_name]
        if members:
            rank_groups.append({"rank": rank_name, "officers": members})

    return render_template("officers.html", officers=records, rank_groups=rank_groups, q=request.args.get("q", ""))


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
    is_own_profile = current_user_owns_officer(officer)

    payroll_summary = None
    if is_own_profile:
        weekly_seconds = int(state.get("weekly_seconds", 0) or 0)
        payroll_multiplier = get_payroll_multiplier()
        payroll_rate = 1500.0
        payroll_summary = {
            "weekly_seconds": weekly_seconds,
            "weekly_time": format_seconds(weekly_seconds),
            "rate": payroll_rate,
            "multiplier": payroll_multiplier,
            "multiplier_label": str(int(payroll_multiplier)) if float(payroll_multiplier).is_integer() else str(payroll_multiplier),
            "amount": (weekly_seconds / 3600) * payroll_rate * payroll_multiplier,
        }

    return render_template(
        "officer.html",
        officer=officer,
        profile_meta=profile_meta,
        documents=documents,
        can_upload_own_profile=is_own_profile,
        is_own_profile=is_own_profile,
        payroll_summary=payroll_summary,
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

    # Maksymalnie 6 dokumentów na profil. Sprawdzamy limit po stronie serwera,
    # żeby nie dało się go ominąć przez ręczne wysłanie formularza.
    existing_documents = get_officer_documents(str(badge))
    if len(existing_documents) >= DOCUMENT_MAX_COUNT:
        return render_template(
            "error.html",
            title="Limit dokumentów",
            message=f"Ten funkcjonariusz ma już maksymalną liczbę {DOCUMENT_MAX_COUNT} dokumentów. Usuń jeden z nich, aby dodać nowy.",
        ), 400

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


@app.route("/taryfikator")
@logged_in_required
def taryfikator():
    return render_template("taryfikator.html")


@app.post("/api/taryfikator/analyze")
@logged_in_required
def taryfikator_analyze():
    """Zwraca wyłącznie identyfikatory zarzutów istniejących w katalogu przesłanym przez kalkulator."""
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return {"ok": False, "error": "Asystent Gemini nie jest skonfigurowany."}, 503

    payload = request.get_json(silent=True) or {}
    description = str(payload.get("description") or "").strip()
    catalog = payload.get("charges") or []

    if len(description) < 5:
        return {"ok": False, "error": "Opis sytuacji jest zbyt krótki."}, 400
    if len(description) > 6000:
        return {"ok": False, "error": "Opis sytuacji jest zbyt długi."}, 400
    if not isinstance(catalog, list) or not catalog:
        return {"ok": False, "error": "Brak katalogu zarzutów."}, 400

    allowed = {}
    safe_catalog = []
    for item in catalog[:500]:
        if not isinstance(item, dict):
            continue
        charge_id = str(item.get("id") or "").strip()[:100]
        name = str(item.get("name") or "").strip()[:300]
        category = str(item.get("category") or "").strip()[:150]
        law = str(item.get("law") or "").strip()[:200]
        if not charge_id or not name:
            continue
        allowed[charge_id] = True
        safe_catalog.append({"id": charge_id, "name": name, "category": category, "law": law})

    if not safe_catalog:
        return {"ok": False, "error": "Brak prawidłowych pozycji taryfikatora."}, 400

    prompt = (
        "Jesteś asystentem funkcjonariusza w fikcyjnym/RP panelu USMS. "
        "Masz wyłącznie podpowiadać zarzuty z dostarczonego katalogu taryfikatora. "
        "NIGDY nie twórz nowych zarzutów, nie zmieniaj nazw i nie korzystaj z prawa spoza katalogu. "
        "Jeżeli opis nie daje podstaw do pozycji z katalogu, zwróć pustą listę. "
        "Uwzględniaj naturalny język, odmiany słów i kontekst zdarzenia. "
        "Nie podawaj uzasadnienia. Zwróć wyłącznie JSON zgodny ze schematem.\n\n"
        f"OPIS SYTUACJI:\n{description}\n\n"
        "KATALOG ZARZUTÓW (wolno wskazać tylko id z tej listy):\n"
        + json.dumps(safe_catalog, ensure_ascii=False)
    )

    preferred_model = (os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash").strip()
    model_candidates = []
    for candidate in (preferred_model, "gemini-2.5-flash", "gemini-2.0-flash"):
        if candidate and candidate not in model_candidates:
            model_candidates.append(candidate)

    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "object",
                "properties": {
                    "charge_ids": {
                        "type": "array",
                        "items": {"type": "string"}
                    }
                },
                "required": ["charge_ids"]
            }
        }
    }

    response = None
    used_model = None
    last_google_error = ""

    for model in model_candidates:
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            candidate_response = requests.post(
                endpoint,
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=body,
                timeout=25,
            )
        except requests.RequestException:
            continue

        if candidate_response.status_code < 400:
            response = candidate_response
            used_model = model
            break

        try:
            last_google_error = candidate_response.json().get("error", {}).get("message", "")
        except Exception:
            last_google_error = ""

        app.logger.warning(
            "Gemini API error model=%s status=%s: %s",
            model,
            candidate_response.status_code,
            last_google_error[:500],
        )

        # 404 usually means the selected model is unavailable for this API/project.
        # Try the next stable fallback model automatically.
        if candidate_response.status_code == 404:
            continue

        # Other API errors are unlikely to be fixed by switching models.
        response = candidate_response
        used_model = model
        break

    if response is None:
        return {"ok": False, "error": "Nie udało się połączyć z dostępnym modelem Gemini."}, 502

    if response.status_code >= 400:
        return {"ok": False, "error": "Gemini chwilowo nie może przeanalizować opisu."}, 502

    app.logger.info("Taryfikator Gemini użył modelu: %s", used_model)

    try:
        result = response.json()
        parts = result["candidates"][0]["content"]["parts"]
        text = "".join(str(part.get("text") or "") for part in parts)
        parsed = json.loads(text)
        ids = parsed.get("charge_ids") or []
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        app.logger.warning("Nieprawidłowa odpowiedź Gemini dla taryfikatora")
        return {"ok": False, "error": "Gemini zwrócił nieprawidłową odpowiedź."}, 502

    unique_ids = []
    for charge_id in ids:
        charge_id = str(charge_id)
        if charge_id in allowed and charge_id not in unique_ids:
            unique_ids.append(charge_id)

    return {"ok": True, "charge_ids": unique_ids, "model": model}


@app.route("/kompendium")
@logged_in_required
def kompendium():
    return render_template("kompendium.html")


@app.route("/dyrektywy")
@logged_in_required
def directives():
    return render_template("directives.html")


@app.route("/akty-prawne")
@logged_in_required
def legal_acts():
    return render_template("legal_acts.html")


@app.route("/start-trainee")
@app.route("/baza-wiedzy/start-trainee")
@logged_in_required
def trainee_start():
    return render_template("trainee_start.html")


@app.route("/materialy-szkoleniowe/haw")
@logged_in_required
def training_haw():
    return render_template("training_haw.html")


@app.route("/materialy-szkoleniowe/ro")
@logged_in_required
def training_ro():
    return render_template("training_ro.html")


@app.route("/materialy-szkoleniowe/nl-i")
@logged_in_required
def training_nli():
    return render_template("training_nli.html")


@app.route("/materialy-szkoleniowe/sv")
@logged_in_required
def training_sv():
    return render_template("training_sv.html")


@app.route("/materialy-szkoleniowe/kpp")
@logged_in_required
def training_kpp():
    return render_template("training_kpp.html")


@app.route("/materialy-szkoleniowe/szpia")
@logged_in_required
def training_szpia():
    return render_template("training_szpia.html")


def _as_utc_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _system_officer_rows():
    officers = load_officers()
    duty = load_duty_state()
    rows = []
    for officer in officers:
        state = duty.get(officer["discord_id"], {})
        officer["duty"] = state
        officer["current_shift_text"] = format_seconds(state.get("current_shift_seconds", 0))
        officer["weekly_text"] = format_seconds(state.get("weekly_seconds", 0))
        officer["lifetime_text"] = format_seconds(state.get("lifetime_seconds", 0))
        rows.append(officer)
    return rows


@app.route("/panel-admina")
@admin_required
def admin_panel():
    return render_template("admin_panel.html")


def _required_trainings_for_rank(rank):
    # FLETC celowo nie jest uwzględniane w podsumowaniu tygodnia.
    required = []
    if rank == "Deputy U.S Marshal Trainee":
        return ["KPP"]
    rank_order = [r[2] for r in RANK_RANGES]
    try:
        idx = rank_order.index(rank)
    except ValueError:
        return required
    deputy_idx = rank_order.index("Deputy U.S Marshal")
    special_idx = rank_order.index("Special Deputy U.S Marshal")
    # RANK_RANGES jest od najwyższego do najniższego stopnia.
    if idx <= deputy_idx:
        required.extend(["KPP", "RO", "NL I", "SV"])
    if idx <= special_idx:
        required.append("SZPIA")
    return required


def _weekly_hours_points(seconds):
    """Punkty za aktywność tygodniową. Norma 10h; bonus kończy się na 50h+."""
    hours = max(0.0, float(seconds or 0) / 3600.0)
    if hours < 2:
        return -25
    if hours < 5:
        return -15
    if hours < 8:
        return -10
    if hours < 10:
        return -5
    if hours < 15:
        return 0
    if hours < 20:
        return 5
    if hours < 30:
        return 10
    if hours < 40:
        return 15
    if hours < 50:
        return 20
    return 25


def _weekly_rating(seconds, plus_count=0, minus_count=0, praise_count=0, reprimand_count=0, training_ok=False):
    """Zwraca punktację 0–100, 1–5 gwiazdek i rozbicie oceny tygodniowej."""
    base = 50
    hours_points = _weekly_hours_points(seconds)
    plus_points = min(max(int(plus_count or 0), 0), 3) * 5
    minus_points = -min(max(int(minus_count or 0), 0), 3) * 5
    praise_points = min(max(int(praise_count or 0), 0), 2) * 10
    reprimand_points = -min(max(int(reprimand_count or 0), 0), 2) * 10
    training_points = 10 if training_ok else 0
    raw = base + hours_points + plus_points + minus_points + praise_points + reprimand_points + training_points
    score = max(0, min(100, raw))
    if score >= 90:
        stars, label = 5, "Wzorowy"
    elif score >= 75:
        stars, label = 4, "Bardzo dobry"
    elif score >= 60:
        stars, label = 3, "Dobry"
    elif score >= 40:
        stars, label = 2, "Wymaga poprawy"
    else:
        stars, label = 1, "Niezadowalający"
    return {
        "score": score, "stars": stars, "label": label,
        "breakdown": [
            ("Baza", base), ("Godziny", hours_points), ("Plusy", plus_points),
            ("Minusy", minus_points), ("Pochwały", praise_points),
            ("Nagany", reprimand_points), ("Szkolenia", training_points),
        ],
    }


@app.route("/panel-admina/podsumowanie-tygodnia")
@admin_required
def weekly_summary():
    records = load_officers()
    duty = load_duty_state()
    groups = []
    for officer in records:
        state = duty.get(officer["discord_id"], {})
        seconds = int(state.get("weekly_seconds", 0) or 0)
        officer["weekly_seconds"] = seconds
        officer["weekly_text"] = format_seconds(seconds)
        officer["norm_met"] = seconds >= 10 * 3600
        officer["norm_missing_text"] = format_seconds(max(0, 10 * 3600 - seconds))
        required = _required_trainings_for_rank(officer.get("rank"))
        completed = set(officer.get("trainings") or [])
        officer["required_trainings"] = required
        officer["required_done"] = [x for x in required if x in completed]
        officer["required_missing"] = [x for x in required if x not in completed]
        officer["training_ok"] = not officer["required_missing"]
        rating = _weekly_rating(
            seconds, officer.get("plus", 0), officer.get("minus", 0),
            officer.get("praise", 0), officer.get("reprimand", 0), officer["training_ok"]
        )
        officer["rating"] = rating["stars"]
        officer["rating_score"] = rating["score"]
        officer["rating_label"] = rating["label"]
        officer["rating_breakdown"] = rating["breakdown"]
        officer["rating_filled"] = "★" * officer["rating"]
        officer["rating_empty"] = "☆" * (5 - officer["rating"])
        officer["is_trainee"] = officer.get("rank") == "Deputy U.S Marshal Trainee"
    for _start, _end, rank_name in RANK_RANGES:
        members = [r for r in records if r.get("rank") == rank_name]
        if members:
            groups.append({"rank": rank_name, "officers": members})
    return render_template("weekly_summary.html", rank_groups=groups)


@app.route("/system/sluzba")
@logged_in_required
def duty_page():
    rows = _system_officer_rows()
    active = [o for o in rows if o["duty"].get("active")]
    paused = [o for o in active if o["duty"].get("on_pause")]
    weekly = sum(o["duty"].get("weekly_seconds", 0) for o in rows)
    lifetime = sum(o["duty"].get("lifetime_seconds", 0) for o in rows)
    rows.sort(key=lambda o: (not o["duty"].get("active"), -int(o["duty"].get("current_shift_seconds", 0)), str(o["badge"])))
    long_shift_threshold = 8 * 3600
    long_shifts = [o for o in active if int(o["duty"].get("current_shift_seconds", 0) or 0) >= long_shift_threshold]
    return render_template("system_duty.html", rows=rows, active=active, paused=paused, long_shifts=long_shifts,
                           long_shift_threshold_hours=8, weekly_hours=round(weekly / 3600, 1), lifetime_hours=round(lifetime / 3600, 1))


@app.route("/moja-wyplata")
@logged_in_required
def my_payroll():
    officer = get_current_officer()
    if not officer:
        return render_template("error.html", title="Brak profilu funkcjonariusza",
                               message="Twoje Discord ID nie jest przypisane do aktywnej karty funkcjonariusza."), 404

    discord_id = int(officer["discord_id"])
    badge = str(officer.get("badge") or "")
    duty = load_duty_state().get(discord_id, {})
    weekly_seconds = int(duty.get("weekly_seconds", 0) or 0)
    hours = weekly_seconds / 3600
    multiplier = get_payroll_multiplier()
    rate = 1500.0
    current_amount = hours * rate * multiplier
    period_start, period_end, period_key, period_label = current_payroll_period()

    history = []
    current_entry = None
    if DATABASE_URL:
        conn = pg_connect("my-payroll")
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT period_key, period_label, badge_snapshot, rank_snapshot, name_snapshot,
                           hours, amount, received, is_history, multiplier, created_at
                    FROM payroll_entries
                    WHERE discord_id = %s OR badge_snapshot = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT 200
                """, (discord_id, badge))
                rows = [dict(r) for r in cur.fetchall()]
                for row in rows:
                    if row.get("period_key") == period_key and current_entry is None:
                        current_entry = row
                    else:
                        history.append(row)
        finally:
            conn.close()

    return render_template("my_payroll.html", officer=officer, history=history,
                           weekly_seconds=weekly_seconds, weekly_time=format_seconds(weekly_seconds),
                           hours=hours, rate=rate, payroll_multiplier=multiplier,
                           current_amount=current_amount, current_entry=current_entry,
                           period_label=period_label)


@app.route("/moj-profil")
@logged_in_required
def my_profile():
    officer = get_current_officer()
    if not officer:
        return render_template("error.html", title="Brak profilu funkcjonariusza",
                               message="Twoje Discord ID nie jest przypisane do aktywnej karty funkcjonariusza."), 404
    return redirect(url_for("officer_detail", badge=officer["badge"]))


@app.route("/system/wyplaty")
@logged_in_required
def payroll_page():
    if not session.get("is_admin", False):
        return redirect(url_for("my_payroll"))

    periods = []
    if DATABASE_URL:
        conn = pg_connect("usms-payroll-periods")
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT period_key, period_label,
                           COUNT(*) AS entries_count,
                           COUNT(*) FILTER (WHERE received) AS received_count,
                           COUNT(*) FILTER (WHERE NOT received) AS pending_count,
                           COALESCE(SUM(amount), 0) AS total_amount,
                           MAX(created_at) AS newest_entry
                    FROM payroll_entries
                    WHERE is_history = TRUE
                    GROUP BY period_key, period_label
                    ORDER BY MAX(created_at) DESC, period_label DESC
                """)
                periods = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    return render_template(
        "system_payroll.html",
        periods=periods,
        payroll_multiplier=get_payroll_multiplier(),
    )


@app.route("/system/wyplaty/<path:period_key>")
@admin_required
def payroll_period_detail(period_key):
    rows = []
    period_label = period_key
    if DATABASE_URL:
        conn = pg_connect("usms-payroll-period-detail")
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, period_key, period_label, badge_snapshot, rank_snapshot, name_snapshot,
                           hours, amount, received, is_history, multiplier, created_at
                    FROM payroll_entries
                    WHERE period_key = %s AND is_history = TRUE
                    ORDER BY CAST(NULLIF(regexp_replace(badge_snapshot, '[^0-9]', '', 'g'), '') AS INTEGER) NULLS LAST,
                             badge_snapshot, name_snapshot
                """, (period_key,))
                rows = [dict(r) for r in cur.fetchall()]
                if rows:
                    period_label = rows[0].get("period_label") or period_key
        finally:
            conn.close()

    if not rows:
        return render_template(
            "error.html",
            title="Nie znaleziono wypłat",
            message="Nie znaleziono zestawienia wypłat dla wybranego tygodnia.",
        ), 404

    received_count = sum(1 for r in rows if r.get("received"))
    total_amount = sum(float(r.get("amount") or 0) for r in rows)
    return render_template(
        "system_payroll_period.html",
        rows=rows,
        period_key=period_key,
        period_label=period_label,
        received_count=received_count,
        pending_count=len(rows) - received_count,
        total_amount=total_amount,
    )


@app.route("/system/wyplaty/mnoznik", methods=["POST"])
@admin_required
def payroll_multiplier_update():
    raw = str(request.form.get("multiplier", "1")).strip().replace(",", ".")
    try:
        multiplier = float(raw)
    except ValueError:
        multiplier = 1.0
    if multiplier not in (1.0, 2.0):
        return render_template("error.html", title="Nieprawidłowy mnożnik",
                               message="Dozwolony mnożnik wypłat to x1 albo x2."), 400
    if not DATABASE_URL:
        return render_template("error.html", title="Brak bazy danych",
                               message="Nie można zapisać mnożnika bez PostgreSQL."), 503
    conn = pg_connect("payroll-multiplier-update")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO payroll_settings (id, multiplier, updated_by, updated_at)
                VALUES (1, %s, %s, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    multiplier = EXCLUDED.multiplier,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = NOW()
            """, (multiplier, int(session["discord_user"]["id"])))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("payroll_page"))


@app.route("/system/urlopy-zawieszenia")
@logged_in_required
def absences_page():
    rows = _system_officer_rows()
    now = datetime.now(timezone.utc)
    vacations, suspensions = [], []
    for o in rows:
        state = o.get("duty", {})
        vacation_end = state.get("vacation_end") or o.get("vacation_end")
        vacation_start = state.get("vacation_start") or o.get("vacation_start")
        suspension_until = state.get("suspension_until") or o.get("suspension_until")
        if vacation_end:
            try:
                end_date = vacation_end.date() if isinstance(vacation_end, datetime) else vacation_end
                if isinstance(end_date, str):
                    end_date = datetime.fromisoformat(end_date).date()
                if end_date >= now.date():
                    item = dict(o); item["vacation_end"] = end_date; item["vacation_start"] = vacation_start
                    vacations.append(item)
            except Exception:
                pass
        dt = _as_utc_datetime(suspension_until)
        if dt and dt > now:
            item = dict(o); item["suspension_until"] = dt.strftime("%Y-%m-%d %H:%M")
            item["suspension_reason"] = o.get("suspension_reason")
            suspensions.append(item)
    return render_template("system_absences.html", vacations=vacations, suspensions=suspensions)


USMS_PING_ROLE_ID = "1511317008720068650"


def resolve_web_announcement_mentions(content: str):
    """Zamień skróty panelu na prawdziwe wzmianki Discorda.

    @<odznaka> -> <@discord_id> dla aktywnego funkcjonariusza
    @usms       -> <@&rola_usms>
    Zwraca też whitelistę allowed_mentions, żeby Discord faktycznie wysłał ping.
    """
    import re
    text = str(content or "")
    officers = load_officers()
    badge_map = {
        str(o.get("badge") or "").strip(): str(o.get("discord_id") or "").strip()
        for o in officers
        if str(o.get("badge") or "").strip() and str(o.get("discord_id") or "").strip().isdigit()
    }
    mentioned_users = []

    def badge_repl(match):
        badge = match.group(1)
        discord_id = badge_map.get(badge)
        if not discord_id:
            return match.group(0)
        if discord_id not in mentioned_users:
            mentioned_users.append(discord_id)
        return f"<@{discord_id}>"

    text = re.sub(r"(?<![A-Za-z0-9_])@(\d{3})(?!\d)", badge_repl, text)
    role_ping = bool(re.search(r"(?<![A-Za-z0-9_])@usms\b", text, flags=re.I))
    text = re.sub(r"(?<![A-Za-z0-9_])@usms\b", f"<@&{USMS_PING_ROLE_ID}>", text, flags=re.I)

    allowed = {"parse": [], "replied_user": False}
    if mentioned_users:
        allowed["users"] = mentioned_users[:100]
    if role_ping:
        allowed["roles"] = [USMS_PING_ROLE_ID]
    return text, allowed


def send_web_announcement_to_discord(channel_id: str, content: str):
    """Wyślij wpis z panelu WEB i zwróć ID wszystkich utworzonych wiadomości."""
    channel_id = str(channel_id or "").strip()
    if channel_id not in WEB_ANNOUNCEMENT_CHANNELS:
        raise ValueError("Nieprawidłowy kanał Discord.")
    if not DISCORD_TOKEN:
        raise RuntimeError("Brak DISCORD_TOKEN w konfiguracji strony.")

    chunks, allowed_mentions = _split_discord_message(content)
    message_ids = []
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}
    for chunk in chunks:
        response = requests.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            headers=headers,
            json={"content": chunk, "allowed_mentions": allowed_mentions},
            timeout=12,
        )
        if response.status_code not in {200, 201}:
            try:
                details = response.json().get("message") or response.text
            except Exception:
                details = response.text
            raise RuntimeError(f"Discord API {response.status_code}: {details[:300]}")
        message_ids.append(str(response.json().get("id") or ""))
    return [x for x in message_ids if x]


def _split_discord_message(content: str):
    """Zamień wzmianki i podziel treść zgodnie z limitem 2000 znaków Discorda."""
    text, allowed_mentions = resolve_web_announcement_mentions(content)
    chunks = []
    while text:
        if len(text) <= 2000:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, 2001)
        if cut < 1000:
            cut = text.rfind(" ", 0, 2001)
        if cut < 1000:
            cut = 2000
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return (chunks or [""]), allowed_mentions


def edit_web_announcement_on_discord(channel_id: str, message_ids, content: str):
    """Edytuj istniejące wiadomości Discord bez ponownego wysyłania pingów.

    Jeśli po edycji treść ma inną liczbę fragmentów, brakujące wiadomości są
    tworzone, a nadmiarowe usuwane. Nowe fragmenty powstałe wyłącznie wskutek
    wydłużenia edytowanego wpisu również mają wyłączone pingi.
    """
    channel_id = str(channel_id or "").strip()
    if channel_id not in WEB_ANNOUNCEMENT_CHANNELS:
        raise ValueError("Nieprawidłowy kanał Discord.")
    if not DISCORD_TOKEN:
        raise RuntimeError("Brak DISCORD_TOKEN w konfiguracji strony.")
    chunks, _ = _split_discord_message(content)
    existing = [str(x) for x in (message_ids or []) if str(x).strip()]
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}
    no_ping = {"parse": [], "users": [], "roles": [], "replied_user": False}
    result_ids = []
    for idx, chunk in enumerate(chunks):
        payload = {"content": chunk, "allowed_mentions": no_ping}
        if idx < len(existing):
            mid = existing[idx]
            response = requests.patch(
                f"{DISCORD_API}/channels/{channel_id}/messages/{mid}",
                headers=headers, json=payload, timeout=12,
            )
            if response.status_code != 200:
                try: details = response.json().get("message") or response.text
                except Exception: details = response.text
                raise RuntimeError(f"Discord API {response.status_code}: {details[:300]}")
            result_ids.append(mid)
        else:
            response = requests.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers=headers, json=payload, timeout=12,
            )
            if response.status_code not in {200, 201}:
                try: details = response.json().get("message") or response.text
                except Exception: details = response.text
                raise RuntimeError(f"Discord API {response.status_code}: {details[:300]}")
            result_ids.append(str(response.json().get("id") or ""))
    for mid in existing[len(chunks):]:
        response = requests.delete(
            f"{DISCORD_API}/channels/{channel_id}/messages/{mid}",
            headers={"Authorization": f"Bot {DISCORD_TOKEN}"}, timeout=12,
        )
        if response.status_code not in {200, 204, 404}:
            try: details = response.json().get("message") or response.text
            except Exception: details = response.text
            raise RuntimeError(f"Discord API {response.status_code}: {details[:300]}")
    return [x for x in result_ids if x]


def delete_web_announcement_from_discord(channel_id: str, message_ids):
    channel_id = str(channel_id or "").strip()
    if not channel_id or not message_ids:
        return
    if not DISCORD_TOKEN:
        raise RuntimeError("Brak DISCORD_TOKEN w konfiguracji strony.")
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
    for mid in [str(x) for x in message_ids if str(x).strip()]:
        response = requests.delete(
            f"{DISCORD_API}/channels/{channel_id}/messages/{mid}", headers=headers, timeout=12,
        )
        if response.status_code not in {200, 204, 404}:
            try: details = response.json().get("message") or response.text
            except Exception: details = response.text
            raise RuntimeError(f"Discord API {response.status_code}: {details[:300]}")


@app.route("/system/ogloszenia", methods=["GET", "POST"])
@admin_required
def announcements_page():
    error = None
    if request.method == "POST":
        validate_csrf()
        kind = (request.form.get("kind") or "announcement").strip()
        content = (request.form.get("content") or "").strip()
        channel_id = (request.form.get("channel_id") or "").strip()
        if kind not in {"announcement", "status9"}:
            error = "Nieprawidłowy typ wpisu."
        elif not content:
            error = "Wpisz treść przed opublikowaniem."
        elif channel_id not in WEB_ANNOUNCEMENT_CHANNELS:
            error = "Wybierz prawidłowy kanał Discord."
        elif len(content) > 12000:
            error = "Treść jest za długa (maksymalnie 12 000 znaków)."
        elif not DATABASE_URL:
            error = "Brak połączenia z PostgreSQL."
        else:
            user = session.get("discord_user") or {}
            try:
                author_id = int(user.get("id")) if user.get("id") else None
            except (TypeError, ValueError):
                author_id = None
            author_name = str(user.get("username") or "Administrator")[:120]
            conn = pg_connect("usms-web-announcements-create")
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO web_announcements (kind, content, author_id, author_name, discord_channel_id) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                        (kind, content, author_id, author_name, channel_id),
                    )
                    item_id = cur.fetchone()[0]
                conn.commit()
            finally:
                conn.close()

            try:
                message_ids = send_web_announcement_to_discord(channel_id, content)
                conn = pg_connect("usms-web-announcements-link-discord")
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE web_announcements SET discord_channel_id=%s, discord_message_ids=%s WHERE id=%s",
                            (channel_id, message_ids, item_id),
                        )
                    conn.commit()
                finally:
                    conn.close()
            except Exception as exc:
                # Wpis pozostaje na stronie; administrator dostaje jasny komunikat,
                # że Discord nie przyjął wiadomości.
                return redirect(url_for(
                    "announcements_page", saved=1, discord_error=str(exc)[:500]
                ))
            return redirect(url_for(
                "announcements_page", saved=1, discord_sent=1,
                discord_channel=WEB_ANNOUNCEMENT_CHANNELS.get(channel_id, channel_id)
            ))

    rows = []
    bot_rows = []
    try:
        rows = load_web_announcements(None, 100)
    except Exception as exc:
        error = error or f"Nie udało się pobrać wpisów: {exc}"

    # Zachowujemy starszą historię sesji bota jako materiał pomocniczy.
    if DATABASE_URL:
        conn = pg_connect("usms-announcements-page")
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                try:
                    cur.execute("SELECT author_id, channel_id, message_id, created_at FROM announcement_sessions ORDER BY created_at DESC LIMIT 50")
                    bot_rows = [dict(r) for r in cur.fetchall()]
                except Exception:
                    conn.rollback()
                    bot_rows = []
        finally:
            conn.close()
    return render_template(
        "system_announcements.html", rows=rows, bot_rows=bot_rows,
        guild_id=DISCORD_GUILD_ID or "", error=error, saved=request.args.get("saved") == "1",
        discord_sent=request.args.get("discord_sent") == "1",
        discord_error=request.args.get("discord_error"),
        discord_channel=request.args.get("discord_channel"),
        edited=request.args.get("edited") == "1",
        deleted=request.args.get("deleted") == "1",
        discord_updated=request.args.get("discord_updated") == "1",
        announcement_channels=WEB_ANNOUNCEMENT_CHANNELS,
        mention_officers=[{
            "badge": str(o.get("badge") or ""),
            "name": str(o.get("full_name") or ""),
            "discord_id": str(o.get("discord_id") or ""),
        } for o in load_officers()],
    )


@app.post("/system/ogloszenia/<int:item_id>/usun")
@admin_required
def delete_web_announcement(item_id):
    validate_csrf()
    discord_error = None
    if DATABASE_URL:
        conn = pg_connect("usms-web-announcements-delete")
        row = None
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT discord_channel_id, discord_message_ids FROM web_announcements WHERE id=%s", (item_id,))
                row = cur.fetchone()
                cur.execute("DELETE FROM web_announcements WHERE id=%s", (item_id,))
            conn.commit()
        finally:
            conn.close()
        if row and row.get("discord_channel_id") and row.get("discord_message_ids"):
            try:
                delete_web_announcement_from_discord(row["discord_channel_id"], row["discord_message_ids"])
            except Exception as exc:
                discord_error = str(exc)[:500]
    return redirect(url_for("announcements_page", deleted=1, discord_error=discord_error))


@app.route("/system/ogloszenia/<int:item_id>/edytuj", methods=["GET", "POST"])
@admin_required
def edit_web_announcement(item_id):
    if not DATABASE_URL:
        return redirect(url_for("announcements_page", discord_error="Brak połączenia z PostgreSQL."))
    conn = pg_connect("usms-web-announcements-edit-load")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM web_announcements WHERE id=%s", (item_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        abort(404)

    error = None
    if request.method == "POST":
        validate_csrf()
        content = (request.form.get("content") or "").strip()
        kind = (request.form.get("kind") or row["kind"]).strip()
        channel_id = str(row.get("discord_channel_id") or request.form.get("channel_id") or "").strip()
        if kind not in {"announcement", "status9"}:
            error = "Nieprawidłowy typ wpisu."
        elif not content:
            error = "Treść nie może być pusta."
        elif len(content) > 12000:
            error = "Treść jest za długa (maksymalnie 12 000 znaków)."
        else:
            discord_error = None
            new_ids = list(row.get("discord_message_ids") or [])
            if channel_id and new_ids:
                try:
                    new_ids = edit_web_announcement_on_discord(channel_id, new_ids, content)
                except Exception as exc:
                    discord_error = str(exc)[:500]
            conn = pg_connect("usms-web-announcements-edit-save")
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE web_announcements SET kind=%s, content=%s, discord_message_ids=%s WHERE id=%s",
                        (kind, content, new_ids, item_id),
                    )
                conn.commit()
            finally:
                conn.close()
            return redirect(url_for(
                "announcements_page", edited=1,
                discord_error=discord_error,
                discord_updated=1 if channel_id and new_ids and not discord_error else None,
            ))

    return render_template(
        "system_announcement_edit.html", row=dict(row), error=error,
        announcement_channels=WEB_ANNOUNCEMENT_CHANNELS,
        mention_officers=[{
            "badge": str(o.get("badge") or ""),
            "name": str(o.get("full_name") or ""),
            "discord_id": str(o.get("discord_id") or ""),
        } for o in load_officers()],
    )


@app.route("/logi")
@admin_required
def logs():
    rows, error = [], None
    log_channel_id = os.environ.get("DISCORD_LOG_CHANNEL_ID", "1542575481776373770").strip()
    if not DISCORD_TOKEN or not log_channel_id:
        error = "Brak konfiguracji bota Discord lub kanału logów."
    else:
        try:
            r = requests.get(
                f"{DISCORD_API}/channels/{log_channel_id}/messages?limit=50",
                headers={"Authorization": f"Bot {DISCORD_TOKEN}"}, timeout=10,
            )
            if r.status_code != 200:
                error = f"Discord API zwróciło kod {r.status_code}."
            else:
                for msg in r.json():
                    author = msg.get("author") or {}
                    message_id = str(msg.get("id") or "")
                    message_url = (
                        f"https://discord.com/channels/{DISCORD_GUILD_ID}/{log_channel_id}/{message_id}"
                        if DISCORD_GUILD_ID and message_id else None
                    )
                    rows.append({
                        "author": author.get("global_name") or author.get("username") or "Discord",
                        "author_id": str(author.get("id") or ""),
                        "timestamp": (msg.get("timestamp") or "").replace("T", " ")[:19],
                        "content": msg.get("content") or "",
                        "embeds": msg.get("embeds") or [],
                        "message_id": message_id,
                        "message_url": message_url,
                    })
        except Exception as exc:
            error = f"Nie udało się pobrać logów Discord: {exc}"
    return render_template("system_logs.html", rows=rows, error=error)


@app.route("/system/status")
@admin_required
def system_status():
    db_ok = False; discord_ok = False
    officer_count = payroll_count = announcement_count = 0
    if DATABASE_URL:
        try:
            conn = pg_connect("usms-system-status")
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1"); db_ok = cur.fetchone()[0] == 1
                    cur.execute("SELECT COUNT(*) FROM officers WHERE active=TRUE"); officer_count = cur.fetchone()[0]
                    cur.execute("SELECT COUNT(*) FROM payroll_entries"); payroll_count = cur.fetchone()[0]
                    cur.execute("SELECT COUNT(*) FROM announcement_sessions"); announcement_count = cur.fetchone()[0]
            finally:
                conn.close()
        except Exception:
            db_ok = False
    if DISCORD_TOKEN:
        try:
            r = requests.get(f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bot {DISCORD_TOKEN}"}, timeout=7)
            discord_ok = r.status_code == 200
        except Exception:
            discord_ok = False
    return render_template("system_status.html", db_ok=db_ok, discord_ok=discord_ok,
                           database_url_set=bool(DATABASE_URL),
                           discord_configured=bool(DISCORD_TOKEN and DISCORD_GUILD_ID),
                           officer_count=officer_count, payroll_count=payroll_count,
                           announcement_count=announcement_count,
                           checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


@app.errorhandler(500)
def internal_error(error):
    return render_template("error.html", title="Błąd serwera",
                           message="Wystąpił błąd po stronie panelu. Sprawdź logi Railway."), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=False)

# ============================================================
# V13 — EGZAMINY OTWARTE Z WERYFIKACJĄ ODPOWIEDZI
# ============================================================
from datetime import timedelta
from zoneinfo import ZoneInfo
import random
import re
import unicodedata
from difflib import SequenceMatcher

EXAM_TZ = ZoneInfo("Europe/Warsaw")
EXAM_QUESTION_COUNT = 20
EXAM_DURATION_MINUTES = 10
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

    ("Zatrzymanie", "W jakim terminie można odwołać się od wyroku?", ["7 dni", "24 godziny", "14 dni", "30 dni"], "7 dni"),
    ("Zatrzymanie", "Do jakiego wymiaru kary zasadniczo możliwa jest kaucja, z uwzględnieniem przewidzianych wyjątków?", ["Do 50 miesięcy", "Do 20 miesięcy", "Do 100 miesięcy", "Bez limitu"], "Do 50 miesięcy"),
    ("Zatrzymanie", "Jaka jest minimalna stawka kaucji za miesiąc?", ["$1000", "$500", "$1500", "$3000"], "$1000"),
    ("Zatrzymanie", "Jaki kanał taktyczny przewidziano do procedur Status 10?", ["TAC 8", "TAC 1", "MAIN", "TAC 3"], "TAC 8"),

    # Dyrektywy PIA — dokładnie jedno pytanie z każdej dyrektywy.
    # Te cztery pytania nie są rozszerzane o warianty językowe.
    ("UoF", "Jakie elementy obejmuje zasada BLOS?", ["Broń", "Lufa", "Otoczenie", "Spust", "Balistyka", "Status radiowy"], "Broń | Lufa | Otoczenie | Spust"),
    ("Struktura operacyjna", "Które role operacyjne wymagają minimum stopnia Senior Deputy U.S Marshal?", ["PWC", "APWC", "OC", "SV", "U1"], "PWC | APWC | OC"),
    ("Pościgi", "Który z poniższych manewrów RUCHOMA jest dozwolony podczas pościgu?", ["RUCHOMA przy kodzie zielonym", "RUCHOMA przy kodzie żółtym", "RUCHOMA po zgodzie U1", "RUCHOMA wyłącznie przez EDWARD"], ""),

    ("Dyrektywa PIA 4 (RFN)", "Co oznacza Dyrektywa PIA nr 4 — Rozdział Floty Niejednorodnej (RFN)?", [], "Dyrektywa RFN oznacza, że podczas konwojów nie należy wykorzystywać prywatnych pojazdów; konwój ma zachować spójność i czytelność, aby nie powodować chaosu ani problemów z identyfikacją pojazdów."),
    ("Dyrektywa PIA 14 (3P)", "Co oznacza Dyrektywa PIA nr 14 — Procedura Przemyślanego Pasa (3P)?", [], "Dyrektywa 3P oznacza zachowanie bezpieczeństwa przy zatrzymanym: przy bezpośrednim kontakcie należy zdjąć pas taktyczny lub wyposażenie możliwe do wykorzystania przez zatrzymanego, a gdy kontakt fizyczny nie jest potrzebny zachować co najmniej metr odstępu od krat."),
    ("Dyrektywa PIA 22 (PUP)", "Co oznacza Dyrektywa PIA nr 22 — Przechowywanie Ujawnionych Przedmiotów (PUP)?", [], "Dyrektywa PUP oznacza zabezpieczanie przedmiotów zatrzymanego, które mogą umożliwiać kontakt ze światem zewnętrznym lub obchodzenie procedur, w indywidualnej szafce depozytowej przypisanej do zatrzymanego."),
    ("Dyrektywa PIA 24 (RS1)", "Co oznacza Dyrektywa PIA nr 24 — Równe Szanse 1 (RS1)?", [], "Dyrektywa RS1 oznacza równe traktowanie funkcjonariuszy: te same zasady i konsekwencje mają obowiązywać niezależnie od płci, wyglądu lub osobistych relacji, bez faworyzowania."),

    ("Negocjacje", "Jakie określenie jest prawidłowe w negocjacjach?", ["Wolny odjazd", "Swobodny odjazd", "Zielone światło", "Bezwarunkowy odjazd"], "Wolny odjazd"),
    ("Negocjacje", "Kiedy omawia się wolny odjazd?", ["Na finalnym etapie negocjacji jako żądanie końcowe", "Na samym początku", "Przed nawiązaniem kontaktu", "Dopiero po pościgu"], "Na finalnym etapie negocjacji jako żądanie końcowe"),
    ("Negocjacje", "Po ilu ostrzeżeniach negocjatora może wystąpić przesłanka do zerwania żądań?", ["3", "1", "2", "5"], "3"),
]


# Rozszerzamy pulę do 300 pozycji na podstawie Kompendium i Dyrektyw PIA, bez dopisywania niepotwierdzonych zasad.
# Każdy fakt z bazowej puli otrzymuje kilka wariantów sformułowania pytania.
# Podczas jednego egzaminu backend nie losuje dwóch pytań z tą samą odpowiedzią wzorcową,
# więc funkcjonariusz nie dostanie dwóch wariantów tego samego faktu.
# V25 — wielokrotny wybór. Pytanie może mieć 0, 1 lub wiele poprawnych odpowiedzi.
_MULTI_CORRECT_OVERRIDES = {
    "Co powinna zawierać ciągła radiówka U1?": ["Kierunek geograficzny", "Ulice lub charakterystyczne punkty", "Kierunek/pas poruszania się pojazdu"],
    "Jakie elementy obejmuje zasada BLOS?": ["Broń", "Lufa", "Otoczenie", "Spust"],
    "Które role operacyjne wymagają minimum stopnia Senior Deputy U.S Marshal?": ["PWC", "APWC", "OC"],
}
_ZERO_CORRECT_OVERRIDES = {"Który z poniższych manewrów RUCHOMA jest dozwolony podczas pościgu?"}
_OPTION_OVERRIDES = {
    "Co powinna zawierać ciągła radiówka U1?": ["Kierunek geograficzny", "Ulice lub charakterystyczne punkty", "Kierunek/pas poruszania się pojazdu", "Wyłącznie prędkość pojazdu", "Wyłącznie model pojazdu"],
    "Jakie elementy obejmuje zasada BLOS?": ["Broń", "Lufa", "Otoczenie", "Spust", "Balistyka", "Status radiowy"],
    "Które role operacyjne wymagają minimum stopnia Senior Deputy U.S Marshal?": ["PWC", "APWC", "OC", "SV", "U1"],
    "Który z poniższych manewrów RUCHOMA jest dozwolony podczas pościgu?": ["RUCHOMA przy kodzie zielonym", "RUCHOMA przy kodzie żółtym", "RUCHOMA po zgodzie U1", "RUCHOMA wyłącznie przez EDWARD"],
}
_DIRECTIVE_OPTIONS = {
    "Co oznacza Dyrektywa PIA nr 4 — Rozdział Floty Niejednorodnej (RFN)?": ["Podczas konwojów nie należy wykorzystywać prywatnych pojazdów; konwój ma zachować spójność i czytelność.", "Każdy funkcjonariusz powinien używać prywatnego pojazdu, aby utrudnić rozpoznanie konwoju.", "Dyrektywa określa zasady używania tasera.", "Dyrektywa dotyczy przechowywania rzeczy zatrzymanego."],
    "Co oznacza Dyrektywa PIA nr 14 — Procedura Przemyślanego Pasa (3P)?": ["Przy bezpośrednim kontakcie z zatrzymanym należy zdjąć pas/wyposażenie możliwe do wykorzystania przez niego, a bez potrzeby kontaktu zachować co najmniej metr od krat.", "Przy każdej rozmowie z zatrzymanym trzeba wejść do celi z pełnym wyposażeniem.", "Dyrektywa pozwala pozostawić telefon zatrzymanemu.", "Dyrektywa określa formację konwoju na autostradzie."],
    "Co oznacza Dyrektywa PIA nr 22 — Przechowywanie Ujawnionych Przedmiotów (PUP)?": ["Przedmioty zatrzymanego umożliwiające kontakt z zewnątrz lub obchodzenie procedur zabezpiecza się w indywidualnej szafce depozytowej.", "Wszystkie przedmioty zatrzymanego pozostają przy nim w celi.", "Telefony przekazuje się dowolnemu funkcjonariuszowi do prywatnego przechowania.", "Dyrektywa reguluje przydział jednostek EDWARD."],
    "Co oznacza Dyrektywa PIA nr 24 — Równe Szanse 1 (RS1)?": ["Funkcjonariuszy obowiązują równe zasady i konsekwencje bez faworyzowania ze względu na płeć, wygląd lub osobiste relacje.", "Przełożony może odstąpić od konsekwencji z powodów osobistych.", "Dyrektywa dotyczy wyłącznie zasad ubioru.", "Dyrektywa pozwala różnicować konsekwencje za tę samą pomyłkę zależnie od osoby."],
}

def _base_question_text(question):
    for prefix in ("Zgodnie z Kompendium — ", "Na podstawie obowiązujących procedur — ", "Wiedza operacyjna USMS — "):
        if question.startswith(prefix):
            q = question[len(prefix):]
            return q[:1].upper() + q[1:]
    return question

def _mcq_payload(question, options, correct_answer):
    base = _base_question_text(question)
    opts = list(_OPTION_OVERRIDES.get(base) or _DIRECTIVE_OPTIONS.get(base) or options or [])
    if base in _ZERO_CORRECT_OVERRIDES:
        correct = []
    elif base in _MULTI_CORRECT_OVERRIDES:
        correct = list(_MULTI_CORRECT_OVERRIDES[base])
    elif base in _DIRECTIVE_OPTIONS:
        correct = [opts[0]]
    else:
        correct = [correct_answer] if correct_answer else []
    return opts, correct

def _expand_exam_question_pool(base_questions, target=300):
    expanded = list(base_questions)
    # Dyrektywy mają pozostać dokładnie czterema pytaniami (po jednym na dyrektywę).
    # Do 300 pozycji rozszerzamy wyłącznie pytania z Kompendium/procedur.
    expansion_source = [q for q in base_questions if not str(q[0]).startswith("Dyrektywa PIA")]
    default_prefixes = [
        "Zgodnie z Kompendium — ",
        "Na podstawie obowiązujących procedur — ",
        "Wiedza operacyjna USMS — ",
    ]
    i = 0
    while len(expanded) < target and expansion_source:
        category, question, options, correct = expansion_source[i % len(expansion_source)]
        prefix = default_prefixes[(i // len(expansion_source)) % len(default_prefixes)]
        q = question.strip()
        if q:
            q = q[0].lower() + q[1:] if len(q) > 1 else q.lower()
        expanded.append((category, prefix + q, list(options), correct))
        i += 1
    return expanded[:target]


EXAM_QUESTIONS = _expand_exam_question_pool(EXAM_QUESTIONS, 300)


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
                    duration_minutes INTEGER NOT NULL DEFAULT 10,
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
            # V13 — odpowiedzi otwarte i audyt oceniania. ALTER-y są idempotentne,
            # dzięki czemu aktualizacja działa również na istniejącej bazie z v12.
            cur.execute("ALTER TABLE exam_attempt_questions ADD COLUMN IF NOT EXISTS grading_status TEXT")
            cur.execute("ALTER TABLE exam_attempt_questions ADD COLUMN IF NOT EXISTS grading_reason TEXT")
            cur.execute("ALTER TABLE exam_attempt_questions ADD COLUMN IF NOT EXISTS similarity_score INTEGER")
            cur.execute("ALTER TABLE exam_attempt_questions ADD COLUMN IF NOT EXISTS reviewed_by BIGINT")
            cur.execute("ALTER TABLE exam_attempt_questions ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE exam_questions ADD COLUMN IF NOT EXISTS correct_answers JSONB")
            cur.execute("ALTER TABLE exam_attempt_questions ADD COLUMN IF NOT EXISTS correct_answers JSONB")
            cur.execute("ALTER TABLE exam_attempt_questions ADD COLUMN IF NOT EXISTS selected_answers JSONB")
            cur.execute("ALTER TABLE exam_attempts ADD COLUMN IF NOT EXISTS pending_review INTEGER NOT NULL DEFAULT 0")
            # V16 — egzamin trwa 10 minut. Zmieniamy także domyślną wartość
            # w istniejącej bazie oraz aktywne/przyszłe sesje utworzone w starszej wersji.
            cur.execute("ALTER TABLE exam_sessions ALTER COLUMN duration_minutes SET DEFAULT 10")
            cur.execute("UPDATE exam_sessions SET duration_minutes=10 WHERE is_enabled=TRUE AND closes_at >= NOW()")
            # Indeksy pod równoczesne podejścia wielu funkcjonariuszy.
            cur.execute("CREATE INDEX IF NOT EXISTS idx_exam_attempts_session_discord ON exam_attempts(session_id, discord_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_exam_attempt_questions_attempt ON exam_attempt_questions(attempt_id)")

            # V24 — usuwamy z aktywnej puli wszystkie starsze pytania z dyrektyw.
            # Historia zakończonych podejść pozostaje w exam_attempt_questions.
            cur.execute("UPDATE exam_questions SET active=FALSE WHERE category LIKE 'Dyrektywa PIA%'")

            for category, question, options, correct in EXAM_QUESTIONS:
                mc_options, mc_correct = _mcq_payload(question, options, correct)
                legacy_correct = " | ".join(mc_correct)
                cur.execute("""
                    INSERT INTO exam_questions(category, question, options, correct_answer, correct_answers)
                    VALUES(%s,%s,%s::jsonb,%s,%s::jsonb)
                    ON CONFLICT (question) DO UPDATE SET category=EXCLUDED.category, options=EXCLUDED.options,
                        correct_answer=EXCLUDED.correct_answer, correct_answers=EXCLUDED.correct_answers, active=TRUE
                """, (category, question, json.dumps(mc_options, ensure_ascii=False), legacy_correct, json.dumps(mc_correct, ensure_ascii=False)))
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


def _normalize_exam_text(value):
    """Normalizacja odpowiedzi bez zmiany znaczenia: wielkość liter, polskie znaki, interpunkcja."""
    text = unicodedata.normalize("NFKD", (value or "").strip().lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("u.s.", "us").replace("u.s", "us")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


_EXAM_STOPWORDS = {
    "a", "albo", "ale", "by", "byc", "czy", "dla", "do", "i", "jak", "jest", "ma", "na",
    "niego", "o", "od", "oraz", "po", "pod", "przed", "przez", "sie", "to", "w", "we",
    "z", "za", "ze", "osoba", "osoby", "jednostka", "jednostki", "funkcjonariusz", "funkcjonariusza"
}

# Najczęściej spotykane równoważne skróty / sformułowania. Nie są to nowe odpowiedzi merytoryczne —
# to tylko warianty zapisu odpowiedzi już istniejących w banku pytań.
_EXAM_ALIASES = {
    "pwc": ["primary watch commander"],
    "sog": ["special operations group"],
    "senior inspector": ["senior inspectors"],
    "senior inspectors": ["senior inspector"],
    "jednostke powietrzna": ["jednostka powietrzna", "u0", "eagle"],
    "pasazer radio operator": ["pasazer", "radio operator", "ro", "pasazer ro"],
    "swiatla i syrena": ["swiatla syrena", "sygnalizacja swietlna i dzwiekowa", "code 3"],
    "procedury zatrzymania na komendzie": ["procedury zatrzymania", "status 10"],
    "rozpoczynam poscig": ["rozpoczecie poscigu", "rozpoczynam poscig", "start poscigu"],
    "poscig zakonczony powodzeniem": ["zakonczenie poscigu powodzeniem", "poscig udany"],
    "poscig zakonczony porazka": ["zakonczenie poscigu porazka", "poscig nieudany"],
    "wznowienie poscigu": ["wznowienie poscigu za", "wznawiam poscig"],
    "potrzebne wsparcie": ["potrzebuje wsparcia", "prosba o wsparcie"],
    "sprzedaz narkotykow": ["handel narkotykami", "sprzedawanie narkotykow"],
    "ranny funkcjonariusz inne jednostki moga dojechac": ["ranny funkcjonariusz mozna dojechac", "ranny fp inne jednostki moga dojechac"],
    "ranny funkcjonariusz inne jednostki nie moga dojechac": ["ranny funkcjonariusz nie mozna dojechac", "ranny fp inne jednostki nie moga dojechac"],
    "ranny straznik wiezienny": ["ranny prison guard", "ranny straznik"],
    "bezpieczenstwo federalnego sadownictwa i rozpraw": ["ochrona federalnego sadownictwa i rozpraw", "ochrona sadow i rozpraw", "bezpieczenstwo sadow i rozpraw"],
    "ewakuacja sedziego i lawnikow do bezpiecznej strefy": ["ewakuacja sedziego i lawnikow", "ochrona i ewakuacja sedziego i lawnikow"],
    "wylegitymowac sprawdzic w bazie i przeszukac": ["wylegitymowac sprawdzic baze przeszukac", "id baza przeszukanie"],
}


def _meaningful_tokens(text):
    return [t for t in _normalize_exam_text(text).split() if t not in _EXAM_STOPWORDS and len(t) > 1]


def grade_free_text(answer, expected):
    """
    Trzystopniowe ocenianie:
    - correct: odpowiedź jednoznacznie poprawna,
    - incorrect: odpowiedź jednoznacznie błędna/pusta,
    - review: odpowiedź podobna, ale wymaga decyzji administratora/TD.
    Dzięki temu system nie oblewa za literówkę, ale nie zgaduje przy odpowiedziach niejednoznacznych.
    """
    raw = (answer or "").strip()
    a = _normalize_exam_text(raw)
    e = _normalize_exam_text(expected)
    if not a:
        return "incorrect", False, 0, "Brak odpowiedzi"

    accepted = {e, *(_EXAM_ALIASES.get(e, []))}
    accepted = {_normalize_exam_text(x) for x in accepted if x}
    if a in accepted:
        return "correct", True, 100, "Zgodna z zaakceptowanym wariantem"

    # Krótkie odpowiedzi (PWC/SOG/Tak/Nie/U1/12 itp.) — akceptujemy je także w pełnym zdaniu,
    # ale unikamy automatycznej decyzji, gdy pojawia się jednocześnie sprzeczne Tak/Nie.
    if e in {"tak", "nie"}:
        toks = set(a.split())
        if e in toks and not ({"tak", "nie"} - {e}) & toks:
            return "correct", True, 100, "Jednoznaczna odpowiedź Tak/Nie"
        return "incorrect", False, 0, "Odpowiedź przeczy oczekiwanej odpowiedzi Tak/Nie"

    e_tokens = _meaningful_tokens(e)
    a_tokens = set(_meaningful_tokens(a))
    if len(e_tokens) == 1 and e_tokens[0] in a_tokens:
        return "correct", True, 100, "Kluczowa odpowiedź występuje w zdaniu"

    # Odpowiedzi liczbowe: wystarczy właściwa wartość, o ile użytkownik nie podał innej konkurencyjnej liczby.
    expected_numbers = re.findall(r"\d+", e)
    answer_numbers = re.findall(r"\d+", a)
    if expected_numbers and set(expected_numbers).issubset(set(answer_numbers)):
        unexpected = set(answer_numbers) - set(expected_numbers)
        if not unexpected:
            return "correct", True, 100, "Zawiera prawidłową wartość liczbową"

    # Porównujemy z najlepszym wariantem. SequenceMatcher daje miarę podobieństwa 0..1.
    best_ratio = max(SequenceMatcher(None, a, x, autojunk=False).ratio() for x in accepted)
    similarity = int(round(best_ratio * 100))

    unique_expected = set(e_tokens)
    coverage = (len(unique_expected & a_tokens) / len(unique_expected)) if unique_expected else 0.0

    # Mocna zgodność tekstu albo prawie wszystkie istotne elementy — zalicz automatycznie.
    if best_ratio >= 0.84:
        return "correct", True, similarity, "Wysokie podobieństwo treści"
    if len(unique_expected) >= 2 and coverage >= 0.85:
        return "correct", True, max(similarity, int(coverage * 100)), "Zawiera wymagane elementy odpowiedzi"

    # Jednoznacznie daleka odpowiedź — odrzuć. Graniczne przypadki idą do ręcznej weryfikacji.
    if best_ratio < 0.34 and coverage < 0.34:
        return "incorrect", False, similarity, "Brak wymaganych elementów odpowiedzi"
    return "review", None, similarity, "Odpowiedź niejednoznaczna — wymaga ręcznej weryfikacji"


def recalculate_attempt(attempt_id):
    conn = pg_connect("usms-exam-recalculate")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM exam_attempts WHERE id=%s FOR UPDATE", (attempt_id,))
            attempt = cur.fetchone()
            if not attempt:
                return
            cur.execute("""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE is_correct=TRUE) AS score,
                       COUNT(*) FILTER (WHERE grading_status='review') AS pending
                FROM exam_attempt_questions WHERE attempt_id=%s
            """, (attempt_id,))
            stats = cur.fetchone()
            total = int(stats["total"] or 0); score = int(stats["score"] or 0); pending = int(stats["pending"] or 0)
            percent = round((score / total) * 100) if total else 0
            cur.execute("SELECT pass_percent FROM exam_sessions WHERE id=%s", (attempt["session_id"],))
            pass_percent = int(cur.fetchone()["pass_percent"] or EXAM_PASS_PERCENT)
            status = "pending_review" if pending else "completed"
            passed = None if pending else (percent >= pass_percent)
            cur.execute("""
                UPDATE exam_attempts
                SET score=%s,total=%s,percent=%s,passed=%s,status=%s,pending_review=%s,
                    submitted_at=COALESCE(submitted_at,NOW())
                WHERE id=%s
            """, (score,total,percent,passed,status,pending,attempt_id))
        conn.commit()
    finally:
        conn.close()


def finalize_attempt(attempt_id, forced=False):
    # Ocenianie jest wykonywane przy zapisie odpowiedzi. Ta funkcja tylko podsumowuje próbę.
    recalculate_attempt(attempt_id)


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
    # Każdy użytkownik ma własne podejście powiązane z Discord ID.
    # Nie ma globalnej blokady „jedna osoba na sesję” — wiele osób może
    # rozpocząć tę samą sesję równocześnie, a ich pytania i timery są niezależne.
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
            # Nie dobieramy dwóch wariantów tego samego faktu (ta sama odpowiedź wzorcowa).
            # Najpierw usuwamy ewentualne powtórzenia odpowiedzi z doboru kategorii.
            unique_chosen = []
            seen_answers = set()
            for q in chosen:
                key = _normalize_exam_text(q.get("correct_answer") or "")
                if key not in seen_answers:
                    unique_chosen.append(q)
                    seen_answers.add(key)
            chosen = unique_chosen
            remaining = int(access["question_count"]) - len(chosen)
            ids = [q["id"] for q in chosen]
            answers = [q["correct_answer"] for q in chosen]
            if remaining > 0:
                params = []
                where = "active=TRUE"
                if ids:
                    where += " AND NOT (id = ANY(%s))"
                    params.append(ids)
                if answers:
                    where += " AND NOT (correct_answer = ANY(%s))"
                    params.append(answers)
                params.append(remaining)
                cur.execute(f"""
                    SELECT * FROM (
                        SELECT DISTINCT ON (correct_answer) *
                        FROM exam_questions
                        WHERE {where}
                        ORDER BY correct_answer, random()
                    ) AS unique_facts
                    ORDER BY random()
                    LIMIT %s
                """, tuple(params))
                chosen.extend(dict(r) for r in cur.fetchall())
            random.shuffle(chosen)
            for pos, q in enumerate(chosen, 1):
                opts = list(q["options"])
                random.shuffle(opts)
                correct_answers = q.get("correct_answers")
                if correct_answers is None:
                    correct_answers = [q["correct_answer"]] if q.get("correct_answer") else []
                cur.execute("""
                    INSERT INTO exam_attempt_questions(attempt_id, question_id, position, question_text, options, correct_answer, correct_answers)
                    VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb)
                """, (attempt_id, q["id"], pos, q["question"], json.dumps(opts, ensure_ascii=False),
                      q["correct_answer"], json.dumps(correct_answers, ensure_ascii=False)))
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
            cur.execute("SELECT id, position, question_text, options, selected_answer, selected_answers FROM exam_attempt_questions WHERE attempt_id=%s ORDER BY position", (attempt_id,))
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
            cur.execute("SELECT id, correct_answer, correct_answers FROM exam_attempt_questions WHERE attempt_id=%s", (attempt_id,))
            for q in cur.fetchall():
                selected = request.form.getlist(f"q_{q['id']}")
                none_selected = request.form.get(f"q_{q['id']}_none") == "1"
                if none_selected:
                    selected = []
                selected = list(dict.fromkeys(x for x in selected if x))
                correct = q.get("correct_answers")
                if correct is None:
                    correct = [q["correct_answer"]] if q.get("correct_answer") else []
                is_correct = set(selected) == set(correct)
                answer_text = "Żadna z powyższych" if none_selected else (" | ".join(selected) if selected else "Brak odpowiedzi")
                reason = "Dokładnie poprawny zestaw odpowiedzi" if is_correct else "Zaznaczony zestaw nie jest kompletnym poprawnym zestawem"
                cur.execute("""
                    UPDATE exam_attempt_questions
                    SET selected_answer=%s, selected_answers=%s::jsonb, is_correct=%s, grading_status=%s, grading_reason=%s,
                        similarity_score=%s, reviewed_by=NULL, reviewed_at=NULL
                    WHERE id=%s
                """, (answer_text, json.dumps(selected, ensure_ascii=False), is_correct,
                      "correct" if is_correct else "incorrect", reason, 100 if is_correct else 0, q["id"]))
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
            cur.execute("""
                SELECT position, question_text, options, selected_answer, selected_answers, correct_answer, correct_answers, is_correct,
                       grading_status, grading_reason, similarity_score
                FROM exam_attempt_questions WHERE attempt_id=%s ORDER BY position
            """, (attempt_id,))
            questions = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return render_template("exam_result.html", attempt=dict(attempt), questions=questions)


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
        opens = exam_now(); closes = opens + timedelta(minutes=10)
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


@app.route("/egzamin/admin/session/<int:session_id>/delete", methods=["POST"])
@admin_required
def exam_admin_delete(session_id):
    """Trwale usuwa dowolną sesję egzaminacyjną wraz z jej podejściami.

    Administrator może usunąć także aktywną sesję oraz sesję, w której ktoś
    aktualnie pisze egzamin. Dzięki kluczom obcym ON DELETE CASCADE usuwane są
    również indywidualne terminy, podejścia i zapisane odpowiedzi. Bank pytań
    pozostaje bez zmian.
    """
    validate_csrf()
    conn = pg_connect("usms-exam-delete")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM exam_sessions WHERE id=%s FOR UPDATE", (session_id,))
            if not cur.fetchone():
                abort(404)
            cur.execute("DELETE FROM exam_sessions WHERE id=%s", (session_id,))
        conn.commit()
    finally:
        conn.close()
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
    officers = load_exam_officers()
    exam_session = dict(exam_session)
    can_delete = True
    return render_template("exam_admin_session.html", exam_session=exam_session, attempts=attempts, overrides=overrides, officers=officers, can_delete=can_delete)


@app.route("/egzamin/admin/attempt/<int:attempt_id>")
@admin_required
def exam_admin_attempt(attempt_id):
    conn = pg_connect("usms-exam-admin-attempt")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT a.*, s.title, s.pass_percent FROM exam_attempts a
                JOIN exam_sessions s ON s.id=a.session_id WHERE a.id=%s
            """, (attempt_id,))
            attempt = cur.fetchone()
            if not attempt: abort(404)
            cur.execute("""
                SELECT id, position, question_text, options, selected_answer, selected_answers, correct_answer, correct_answers, is_correct,
                       grading_status, grading_reason, similarity_score, reviewed_by, reviewed_at
                FROM exam_attempt_questions WHERE attempt_id=%s ORDER BY position
            """, (attempt_id,))
            questions = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return render_template("exam_admin_attempt.html", attempt=dict(attempt), questions=questions)


@app.route("/egzamin/admin/attempt/<int:attempt_id>/review/<int:answer_id>", methods=["POST"])
@admin_required
def exam_admin_review_answer(attempt_id, answer_id):
    validate_csrf()
    decision = request.form.get("decision")
    if decision not in {"correct", "incorrect"}:
        abort(400)
    admin_id = int(session["discord_user"]["id"])
    conn = pg_connect("usms-exam-admin-review")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE exam_attempt_questions
                SET is_correct=%s, grading_status=%s, grading_reason=%s,
                    reviewed_by=%s, reviewed_at=NOW()
                WHERE id=%s AND attempt_id=%s
            """, (decision == "correct", "manual_correct" if decision == "correct" else "manual_incorrect",
                  "Zweryfikowano ręcznie przez administratora", admin_id, answer_id, attempt_id))
            if cur.rowcount != 1:
                abort(404)
        conn.commit()
    finally:
        conn.close()
    recalculate_attempt(attempt_id)
    return redirect(url_for("exam_admin_attempt", attempt_id=attempt_id))


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
