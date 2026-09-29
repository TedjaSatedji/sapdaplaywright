"""
Shared storage & helpers for telegbot.py and discordbot.py.

Owns everything the two bots used to duplicate:
- .env user records (same 5-line block format spda.py reads via os.environ)
- pause flag files (same names spda.py checks)
- SPADA credential verification
- schedule CSV loading/validation and upcoming-class lookup
- Gemini schedule image parsing

spda.py intentionally keeps its own loader; this module only guarantees
that the on-disk format stays compatible with it.
"""

import csv
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests
import urllib3
from dotenv import load_dotenv
import google.generativeai as genai

# =============================================================================
# Config & paths
# =============================================================================

load_dotenv()

ENV_FILE = ".env"
FLAG_DIR = "flags"
SCHEDULE_DIR = "schedules"
ATTENDANCE_FLAG_DIR = os.path.join(FLAG_DIR, "attendance")
SPADA_LOGIN_URL = "https://spada.upnyk.ac.id/login/index.php"

os.makedirs(FLAG_DIR, exist_ok=True)
os.makedirs(SCHEDULE_DIR, exist_ok=True)
os.makedirs(ATTENDANCE_FLAG_DIR, exist_ok=True)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# CSV "Day" column uses Indonesian day names (same mapping as spda.py)
DAY_MAP = {
    "Monday": "Senin", "Tuesday": "Selasa", "Wednesday": "Rabu",
    "Thursday": "Kamis", "Friday": "Jumat", "Saturday": "Sabtu", "Sunday": "Minggu",
}

CSV_HEADER = "CourseName,Day,Time"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
_gemini_model = genai.GenerativeModel("gemini-2.5-flash-lite")


def safe_name(name: str) -> str:
    """Course name -> flag-file token (same rule spda.py uses)."""
    return re.sub(r"\s+", "_", name.strip())


# =============================================================================
# User records (.env)
# =============================================================================
# On-disk format (one block per user, matches what spda.py loads):
#   #--- {username} ---
#   SPADA_USERNAME_{i}=...
#   SPADA_PASSWORD_{i}=...
#   TELEGRAM_CHAT_ID_{i}=...   or   DISCORD_USER_ID_{i}=...
#   SCHEDULE_FILE_{i}=...

@dataclass
class UserRecord:
    index: str
    username: str
    password: str
    telegram_chat_id: str | None
    discord_user_id: str | None
    schedule_path: str

    def chat_id(self, platform: str) -> str | None:
        return self.discord_user_id if platform == "discord" else self.telegram_chat_id


def _value(line: str) -> str:
    return line.strip().split("=", 1)[1] if "=" in line else ""


def _chat_key(platform: str) -> str:
    return "TELEGRAM_CHAT_ID" if platform == "telegram" else "DISCORD_USER_ID"


def _parse_blocks(lines: list[str]):
    """
    Yield (index, fields) for each 5-line user block in the .env file.
    Blocks must keep the documented key order; anything else is left alone.
    """
    i = 0
    while i < len(lines) - 4:
        if (
            lines[i].startswith("#---")
            and lines[i + 1].startswith("SPADA_USERNAME_")
            and lines[i + 2].startswith("SPADA_PASSWORD_")
            and (lines[i + 3].startswith("TELEGRAM_CHAT_ID_") or lines[i + 3].startswith("DISCORD_USER_ID_"))
            and lines[i + 4].startswith("SCHEDULE_FILE_")
        ):
            index_match = re.match(r"SPADA_USERNAME_(\d+)=", lines[i + 1])
            if index_match:
                raw_index = index_match.group(1)
                yield raw_index, {
                    "index": raw_index,
                    "username": _value(lines[i + 1]),
                    "password": _value(lines[i + 2]),
                    "telegram_chat_id": _value(lines[i + 3]) if lines[i + 3].startswith("TELEGRAM_CHAT_ID_") else None,
                    "discord_user_id": _value(lines[i + 3]) if lines[i + 3].startswith("DISCORD_USER_ID_") else None,
                    "schedule_path": _value(lines[i + 4]),
                }
                i += 5
                continue
        i += 1


def _read_env_lines() -> list[str]:
    if not os.path.exists(ENV_FILE):
        return []
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        return f.readlines()


def _write_env_lines(lines: list[str]):
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines)


def read_users(platform: str) -> list[UserRecord]:
    """All user records for a platform ('telegram' | 'discord'), ordered by index."""
    key = "telegram_chat_id" if platform == "telegram" else "discord_user_id"
    users = [
        UserRecord(**fields)
        for _, fields in _parse_blocks(_read_env_lines())
        if fields[key]
    ]
    return users


def find_user(platform: str, chat_id: str) -> UserRecord | None:
    for user in read_users(platform):
        if user.chat_id(platform) == chat_id:
            return user
    return None


def save_user(platform: str, chat_id: str, username: str, password: str) -> UserRecord:
    """Append a new user block and pre-create its (empty) schedule file."""
    index = 1
    for line in _read_env_lines():
        match = re.match(r"SPADA_USERNAME_(\d+)=", line)
        if match:
            index = max(index, int(match.group(1)) + 1)

    schedule_path = os.path.join(SCHEDULE_DIR, f"schedule_{index}.csv")
    if not os.path.exists(schedule_path):
        open(schedule_path, "w", encoding="utf-8").close()

    chat_key = _chat_key(platform)
    with open(ENV_FILE, "a", encoding="utf-8") as f:
        f.write(f"#--- {username} ---\n")
        f.write(f"SPADA_USERNAME_{index}={username}\n")
        f.write(f"SPADA_PASSWORD_{index}={password}\n")
        f.write(f"{chat_key}_{index}={chat_id}\n")
        f.write(f"SCHEDULE_FILE_{index}={schedule_path}\n")

    return UserRecord(
        index=str(index),
        username=username,
        password=password,
        telegram_chat_id=chat_id if platform == "telegram" else None,
        discord_user_id=chat_id if platform == "discord" else None,
        schedule_path=schedule_path,
    )


def update_user(user: UserRecord, username: str, password: str) -> bool:
    """Replace username/password for a record (matched by index); renames its flags."""
    new_lines = []
    found = False
    old_username = None

    for line in _read_env_lines():
        if line.startswith(f"SPADA_USERNAME_{user.index}="):
            found = True
            old_username = _value(line)
            new_lines.append(f"SPADA_USERNAME_{user.index}={username}\n")
        elif line.startswith(f"SPADA_PASSWORD_{user.index}="):
            new_lines.append(f"SPADA_PASSWORD_{user.index}={password}\n")
        elif line.startswith(f"#--- {user.username} ---"):
            new_lines.append(f"#--- {username} ---\n")
        else:
            new_lines.append(line)

    if not found:
        return False

    _write_env_lines(new_lines)
    rename_user_flags(old_username or user.username, username)

    user.username = username
    user.password = password
    return True


def delete_user(user: UserRecord) -> bool:
    """Remove a user block, its schedule file, and all of its flag files."""
    new_lines = []
    found = False
    username = None
    schedule_path = None

    lines = _read_env_lines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(f"SPADA_USERNAME_{user.index}="):
            found = True
            username = _value(line)
            # drop the comment line directly above, if it's still one
            if new_lines and new_lines[-1].startswith("#---"):
                new_lines.pop()
            i += 1
            # swallow the rest of the block (password / chat id / schedule path)
            while i < len(lines) and (
                lines[i].startswith(f"SPADA_PASSWORD_{user.index}=")
                or lines[i].startswith(f"TELEGRAM_CHAT_ID_{user.index}=")
                or lines[i].startswith(f"DISCORD_USER_ID_{user.index}=")
                or lines[i].startswith(f"SCHEDULE_FILE_{user.index}=")
            ):
                if lines[i].startswith(f"SCHEDULE_FILE_{user.index}="):
                    schedule_path = _value(lines[i])
                i += 1
            continue
        new_lines.append(line)
        i += 1

    if not found:
        return False

    _write_env_lines(new_lines)

    if schedule_path and os.path.exists(schedule_path):
        try:
            os.remove(schedule_path)
        except OSError:
            pass

    if username:
        _remove_all_user_flags(username)
    return True


# =============================================================================
# Flags
# =============================================================================

def pause_flag_path(username: str) -> str:
    return os.path.join(FLAG_DIR, f"pause_user_{username}.flag")


def get_pause_state(username: str) -> tuple[str, str | None]:
    """
    Current pause state for a user.
    Returns ("active", None), ("indefinite", None) or ("once", course display name).
    """
    if os.path.exists(pause_flag_path(username)):
        return "indefinite", None

    prefix = f"pause_once_{username}_"
    if os.path.isdir(FLAG_DIR):
        for filename in os.listdir(FLAG_DIR):
            if filename.startswith(prefix):
                course = filename[len(prefix):].replace(".flag", "").replace("_", " ")
                return "once", course
    return "active", None


def set_indefinite_pause(username: str):
    with open(pause_flag_path(username), "w") as f:
        f.write("paused")


def set_once_pause(username: str, course: str):
    """Flag a single course to be skipped (name must match the schedule CSV)."""
    path = os.path.join(FLAG_DIR, f"pause_once_{username}_{safe_name(course)}.flag")
    with open(path, "w") as f:
        f.write("skip next")


def clear_all_pauses(username: str):
    """Remove the indefinite pause and any one-time skip flags."""
    try:
        os.remove(pause_flag_path(username))
    except OSError:
        pass
    prefix = f"pause_once_{username}_"
    if os.path.isdir(FLAG_DIR):
        for filename in os.listdir(FLAG_DIR):
            if filename.startswith(prefix):
                try:
                    os.remove(os.path.join(FLAG_DIR, filename))
                except OSError:
                    pass


def _remove_all_user_flags(username: str):
    """Remove every flag file belonging to a user (pause + attendance)."""
    exact = {f"pause_user_{username}.flag"}
    prefixes = (
        f"pause_once_{username}_",
        f"success_{username}_",
        f"retry_{username}_",
    )
    for directory in (FLAG_DIR, ATTENDANCE_FLAG_DIR):
        if not os.path.isdir(directory):
            continue
        for filename in os.listdir(directory):
            if filename in exact or filename.startswith(prefixes):
                try:
                    os.remove(os.path.join(directory, filename))
                except OSError:
                    pass


def rename_user_flags(old_username: str, new_username: str):
    """Rename every flag file belonging to old_username so it follows the new name."""
    if not old_username or old_username == new_username:
        return

    exact_jobs = [
        (FLAG_DIR, f"pause_user_{old_username}.flag", f"pause_user_{new_username}.flag"),
    ]
    prefix_jobs = [
        (FLAG_DIR, f"pause_once_{old_username}_", f"pause_once_{new_username}_"),
        (ATTENDANCE_FLAG_DIR, f"success_{old_username}_", f"success_{new_username}_"),
        (ATTENDANCE_FLAG_DIR, f"retry_{old_username}_", f"retry_{new_username}_"),
    ]

    for directory, old_name, new_name in exact_jobs:
        old_path = os.path.join(directory, old_name)
        if os.path.exists(old_path):
            try:
                os.replace(old_path, os.path.join(directory, new_name))
            except OSError:
                pass

    for directory, old_prefix, new_prefix in prefix_jobs:
        if not os.path.isdir(directory):
            continue
        for filename in os.listdir(directory):
            if not filename.startswith(old_prefix):
                continue
            old_path = os.path.join(directory, filename)
            new_path = os.path.join(directory, filename.replace(old_prefix, new_prefix, 1))
            try:
                os.replace(old_path, new_path)
            except OSError:
                pass


# =============================================================================
# SPADA credential verification
# =============================================================================

def verify_spada_credentials(username: str, password: str) -> tuple[bool, str]:
    def attempt_login(verify: bool) -> tuple[bool, str]:
        with requests.Session() as session:
            login_page = session.get(SPADA_LOGIN_URL, timeout=20, verify=verify)
            login_page.raise_for_status()

            token_match = re.search(r'name=["\']logintoken["\']\s+value=["\']([^"\']+)["\']', login_page.text)
            payload = {
                "username": username,
                "password": password,
                "anchor": "",
            }
            if token_match:
                payload["logintoken"] = token_match.group(1)

            response = session.post(
                SPADA_LOGIN_URL,
                data=payload,
                timeout=20,
                allow_redirects=True,
                verify=verify,
            )
            response.raise_for_status()

            final_url = response.url.lower()
            body = response.text.lower()
            if "login/index.php" not in final_url:
                if verify:
                    return True, "saved credentials are valid!"
                return True, "saved credentials are valid! (SPADA's SSL certificate could not be verified)"
            if "loginerrors" in body or "invalidlogin" in body:
                return False, "saved credentials were rejected by SPADA. please check and update them."
            return False, "SPADA kept the session on the login page. credentials may be wrong."

    try:
        return attempt_login(verify=True)
    except requests.exceptions.SSLError:
        try:
            return attempt_login(verify=False)
        except requests.Timeout:
            return False, "SPADA did not respond in time. try again later."
        except requests.RequestException as exc:
            return False, f"couldn't reach SPADA after SSL fallback: {exc}"
    except requests.Timeout:
        return False, "SPADA did not respond in time. try again later."
    except requests.RequestException as exc:
        return False, f"couldn't reach SPADA: {exc}"


# =============================================================================
# Schedule CSV
# =============================================================================

def parse_schedule_text(csv_text: str) -> list[dict]:
    """Parse CSV text into usable schedule rows (for previews and validation)."""
    try:
        rows = list(csv.DictReader(csv_text.splitlines()))
    except Exception:
        return []
    return [
        row for row in rows
        if row.get("CourseName") and row.get("Day") and row.get("Time")
    ]


def load_schedule_rows(schedule_path: str) -> list[dict]:
    """Rows from a schedule CSV; tolerates missing, empty or malformed files."""
    if not schedule_path or not os.path.exists(schedule_path) or os.path.getsize(schedule_path) == 0:
        return []
    try:
        with open(schedule_path, encoding="utf-8") as f:
            return parse_schedule_text(f.read())
    except Exception:
        return []


def validate_schedule_csv(csv_text: str) -> str | None:
    """Return a friendly error message, or None if the CSV looks usable."""
    lines = [l for l in csv_text.strip().splitlines() if l.strip()]
    if not lines:
        return "the file is empty."
    if not lines[0].lower().startswith("coursename,day,time"):
        return f"the first line must be the header: {CSV_HEADER}"
    if len(lines) < 2:
        return "it needs at least one schedule row."
    if not parse_schedule_text(csv_text):
        return "i couldn't find any usable rows — the columns must be CourseName,Day,Time."
    return None


def normalize_schedule_csv(csv_text: str) -> str:
    """Clean up extracted CSV text: make sure the header line exists and file ends with a newline."""
    lines = [l.strip() for l in csv_text.strip().splitlines() if l.strip()]
    if lines and not lines[0].lower().startswith("coursename"):
        lines.insert(0, CSV_HEADER)
    return "\n".join(lines) + "\n"


def save_schedule_csv(schedule_path: str, csv_text: str):
    with open(schedule_path, "w", encoding="utf-8") as f:
        f.write(csv_text if csv_text.endswith("\n") else csv_text + "\n")


def delete_schedule_file(schedule_path: str, recreate_empty: bool = True):
    """
    Remove the CSV; by default keep an empty file behind so the path in .env
    stays valid (spda.py skips users whose schedule file is missing).
    """
    if schedule_path and os.path.exists(schedule_path):
        try:
            os.remove(schedule_path)
        except OSError:
            pass
    if recreate_empty and schedule_path:
        open(schedule_path, "w", encoding="utf-8").close()


def upcoming_classes(schedule_path: str, within_days: int = 7) -> list[dict]:
    """
    Schedule classes happening within the next `within_days` days, soonest first.
    Each item: {"course", "day", "time", "start": datetime}.
    Includes classes that started up to 15 minutes ago (spda.py's attendance
    window), so they can still be skipped.
    """
    now = datetime.now()
    upcoming = []

    for row in load_schedule_rows(schedule_path):
        try:
            start_str, _ = row["Time"].split(" - ")
            start_time = datetime.strptime(start_str.strip(), "%H:%M")
        except ValueError:
            continue

        day_name = row["Day"].strip().capitalize()
        for offset in range(within_days + 1):
            day = now + timedelta(days=offset)
            if DAY_MAP[day.strftime("%A")] != day_name:
                continue
            candidate = start_time.replace(year=day.year, month=day.month, day=day.day)
            if candidate >= now - timedelta(minutes=15):
                upcoming.append({
                    "course": row["CourseName"].strip(),
                    "day": day_name,
                    "time": row["Time"].strip(),
                    "start": candidate,
                })
                break

    upcoming.sort(key=lambda c: c["start"])
    return upcoming


def get_next_class(schedule_path: str) -> str | None:
    """Name of the next upcoming class (any day of the week), or None."""
    classes = upcoming_classes(schedule_path)
    return classes[0]["course"] if classes else None


# =============================================================================
# Gemini schedule extraction
# =============================================================================

_GEMINI_PROMPT = (
    "Extract the class schedule from this image and return only CSV rows. "
    "Columns must be in this exact order: CourseName,Day,Time. "
    "Example:\n"
    "CourseName,Day,Time\n"
    "Matematika,Senin,07:00 - 09:00\n"
    "Fisika,Rabu,10:00 - 12:00\n"
    "Always add the header column name\n"
    "Do not add ```csv``` or any code fences\n"
    "Do not forget the space before and after hyphen for the time\n"
    "Do not include class, explanations, or extra text. only Course Name, Day and Time."
)


def parse_schedule_with_gemini(image_bytes: bytes) -> str:
    """Send a schedule image to Gemini and return raw CSV text."""
    image_data = {"mime_type": "image/jpeg", "data": image_bytes}
    resp = _gemini_model.generate_content([_GEMINI_PROMPT, image_data])
    return (resp.text or "").strip()
