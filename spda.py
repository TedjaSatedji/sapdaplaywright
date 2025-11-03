
"""
SPADA auto-attendance (max two retries per class per day)
- Tracks pause flags and per-class success/retry flags.
- Notifies via Telegram or Discord DMs (per user).
- Auto-corrects schedule time from Moodle attendance table when found.
"""

import asyncio
import csv
import os
import re
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import discord

# =============================================================================
# Configuration & Globals
# =============================================================================

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

SPADA_LOGIN_URL = "https://spada.upnyk.ac.id/login/index.php"

# Flags
FLAG_DIR = "flags"
ATTENDANCE_FLAG_DIR = os.path.join(FLAG_DIR, "attendance")
os.makedirs(FLAG_DIR, exist_ok=True)
os.makedirs(ATTENDANCE_FLAG_DIR, exist_ok=True)

# Discord client (started only if DISCORD_TOKEN is set)
discord_intents = discord.Intents.default()
discord_client = discord.Client(intents=discord_intents)
discord_ready = asyncio.Event()


@discord_client.event
async def on_ready():
    print(f"[Discord] Logged in as {discord_client.user}")
    discord_ready.set()


# =============================================================================
# Notifications
# =============================================================================

async def send_discord(message: str, user_id: str):
    """DM a Discord user by ID."""
    if not DISCORD_TOKEN or not user_id:
        print("Discord token or user ID missing. Skipping Discord message.")
        return
    await discord_ready.wait()
    try:
        user = await discord_client.fetch_user(int(user_id))
        await user.send(message)
    except Exception as e:
        print(f"Failed to send Discord message to {user_id}: {e}")


def send_telegram(message: str, chat_id: str):
    """Send a Telegram message to a chat ID."""
    if not TELEGRAM_TOKEN or not chat_id:
        print("Telegram token or chat ID missing. Skipping Telegram message.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": message}
    try:
        requests.post(url, data=data, timeout=15)
    except Exception as e:
        print(f"Failed to send Telegram message to {chat_id}: {e}")


async def notify_user(message: str, user: dict):
    """Route notification based on user's preferred channel."""
    if user.get("use_discord"):
        await send_discord(message, user["chat_id"])
    else:
        send_telegram(message, user["chat_id"])


# =============================================================================
# Users & Schedule
# =============================================================================

def load_users():
    """
    Read users from environment:
      SPADA_USERNAME_{i}, SPADA_PASSWORD_{i},
      TELEGRAM_CHAT_ID_{i} or DISCORD_USER_ID_{i},
      SCHEDULE_FILE_{i} (default 'schedule.csv')
    """
    users = []
    indices = set()
    for key in os.environ.keys():
        if key.startswith("SPADA_USERNAME_"):
            try:
                indices.add(int(key.split("_")[-1]))
            except ValueError:
                pass

    for i in sorted(indices):
        username = os.getenv(f"SPADA_USERNAME_{i}")
        password = os.getenv(f"SPADA_PASSWORD_{i}")
        telegram_id = os.getenv(f"TELEGRAM_CHAT_ID_{i}")
        discord_id = os.getenv(f"DISCORD_USER_ID_{i}")
        chat_id = discord_id or telegram_id
        use_discord = bool(discord_id)
        schedule_file = os.getenv(f"SCHEDULE_FILE_{i}", "schedule.csv")

        if all([username, password, chat_id]):
            users.append({
                "username": username,
                "password": password,
                "chat_id": chat_id,
                "use_discord": use_discord,
                "schedule_file": schedule_file
            })
    return users


def load_schedule(csv_file: str):
    with open(csv_file, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def get_current_class(schedule):
    """
    Returns the CourseName if now is within [start, start+15m] for today (Indonesian day names in CSV).
    CSV columns: CourseName, Day, Time (e.g., "08:00 - 09:40")
    """
    now = datetime.now()
    day_map = {
        "Monday": "Senin", "Tuesday": "Selasa", "Wednesday": "Rabu",
        "Thursday": "Kamis", "Friday": "Jumat", "Saturday": "Sabtu", "Sunday": "Minggu"
    }
    today = day_map[now.strftime("%A")]

    for entry in schedule:
        if entry.get("Day") != today:
            continue

        try:
            start_str, end_str = entry["Time"].split(" - ")
            start_time = datetime.strptime(start_str.strip(), "%H:%M").replace(
                year=now.year, month=now.month, day=now.day
            )
            # allow inside the first 15 minutes window
            if start_time <= now <= start_time + timedelta(minutes=15):
                return entry["CourseName"]
        except Exception:
            continue

    return None


async def update_schedule_time(course_name, new_time, schedule_file, user):
    """
    Update the schedule CSV only if the time changed and notify the user.
    """
    rows, updated, old_time = [], False, None
    with open(schedule_file, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["CourseName"].lower().startswith(course_name.lower()):
                old_time = row["Time"]
                if row["Time"] != new_time:
                    row["Time"] = new_time
                    updated = True
            rows.append(row)

    if updated:
        with open(schedule_file, "w", newline='', encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["CourseName", "Day", "Time"])
            writer.writeheader()
            writer.writerows(rows)

        msg = f"⏰ Schedule for *{course_name}* corrected: **{old_time} → {new_time}**."
        print(msg)
        await notify_user(msg, user)


# =============================================================================
# Flags
# =============================================================================

def _safe(name: str) -> str:
    return re.sub(r"\s+", "_", name.strip())


def is_paused(username: str, course_name: str) -> bool:
    """Check indefinite pause or single-class pause flags."""
    if os.path.exists(os.path.join(FLAG_DIR, f"pause_user_{username}.flag")):
        print(f"⏸️ {username} is paused indefinitely.")
        return True

    once_path = os.path.join(FLAG_DIR, f"pause_once_{username}_{_safe(course_name)}.flag")
    if os.path.exists(once_path):
        print(f"⏸️ Skipping one class {course_name} for {username}.")
        try:
            os.remove(once_path)
        except Exception:
            pass
        return True

    return False


def has_attended_today(username: str, course_name: str) -> bool:
    """Success flag exists for this user/course/today?"""
    today = datetime.now().strftime("%Y-%m-%d")
    flag = os.path.join(
        ATTENDANCE_FLAG_DIR, f"success_{username}_{_safe(course_name)}_{today}.flag"
    )
    if os.path.exists(flag):
        print(f"✅ {username} already attended {course_name} today.")
        return True
    return False


def get_current_attempt(username: str, course_name: str) -> int:
    """
    Determine attempt number based on retry flags.
    Returns:
      1: first attempt
      2: second attempt
      3: stop (already used two retries)
    """
    today = datetime.now().strftime("%Y-%m-%d")
    base = f"retry_{username}_{_safe(course_name)}_{today}_attempt_"
    if os.path.exists(os.path.join(ATTENDANCE_FLAG_DIR, f"{base}2.flag")):
        return 3
    if os.path.exists(os.path.join(ATTENDANCE_FLAG_DIR, f"{base}1.flag")):
        return 2
    return 1


def _clear_retry_flags(username: str, course_name: str, today: str):
    """Remove retry flags (attempts 1..2) on success."""
    base = f"retry_{username}_{_safe(course_name)}_{today}_attempt_"
    for i in (1, 2):
        path = os.path.join(ATTENDANCE_FLAG_DIR, f"{base}{i}.flag")
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                print(f"Warning: could not remove retry flag {path}: {e}")


def cleanup_old_flags():
    """Delete success/retry flags that are not from today."""
    print("🧹 Cleaning up old flags...")
    today_suffix = f"_{datetime.now().strftime('%Y-%m-%d')}"
    for filename in os.listdir(ATTENDANCE_FLAG_DIR):
        if filename.startswith(("success_", "retry_")) and today_suffix not in filename:
            try:
                os.remove(os.path.join(ATTENDANCE_FLAG_DIR, filename))
                print(f"Removed old flag: {filename}")
            except Exception as e:
                print(f"Failed to remove {filename}: {e}")


# =============================================================================
# Moodle helpers
# =============================================================================

def _parse_moodle_date(s: str):
    """
    Parse Moodle date formats like:
      "Sat 6 Sep 2025", "Friday, 12 September 2025" (commas stripped by caller),
      "6 Sep 2025", "6 September 2025"
    Returns date() or None.
    """
    s = s.strip().replace(",", "")
    fmts = [
        "%a %d %b %Y",
        "%a %d %B %Y",
        "%A %d %b %Y",
        "%A %d %B %Y",
        "%d %b %Y",
        "%d %B %Y",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _normalize_time_str(t: str) -> str:
    """Turn '10AM' into '10:00AM', etc."""
    t = t.strip().upper()
    if re.match(r"^\d{1,2}(AM|PM)$", t):
        t = t[:-2] + ":00" + t[-2:]
    return t


# =============================================================================
# Core: Playwright flow
# =============================================================================

async def login_and_attend(playwright, user: dict, course_name: str) -> bool:
    """
    Try to submit attendance.
    Returns:
      True  -> success attendance submitted
      False -> unrecoverable login failure (do not retry)
    Raises:
      Exception -> retriable issues (no link, closed, timeouts, etc.)
    """
    browser = await playwright.firefox.launch(headless=True)
    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    username = user["username"]
    password = user["password"]

    try:
        print(f"Processing attendance for {username}...")
        await page.goto(SPADA_LOGIN_URL, timeout=60_000)

        await page.fill("#username", username)
        await page.fill("#password", password)
        await page.click("#loginbtn")

        await page.wait_for_timeout(3000)
        if "login/index.php" in page.url:
            print(f"❌ Login failed for {username}")
            return False  # unrecoverable

        # Find course link (anchor whose text startswith course_name)
        course_link = None
        for link in await page.query_selector_all("a"):
            try:
                text = (await link.inner_text()).strip().lower()
                if text.startswith(course_name.lower()):
                    course_link = link
                    break
            except Exception:
                continue

        if not course_link:
            raise Exception(f"Course '{course_name}' not found on SPADA.")

        await course_link.click()
        await page.wait_for_timeout(2000)

        # Find attendance activity
        att_link = None
        for activity in await page.query_selector_all("li.activity.attendance a"):
            try:
                text = (await activity.inner_text()).lower()
                if "attendance" in text or "presensi" in text:
                    att_link = activity
                    break
            except Exception:
                continue

        if not att_link:
            raise Exception(f"No attendance link found for {course_name}.")

        await att_link.click()
        await page.wait_for_timeout(2000)

        # Try to match today's row and correct schedule time
        today = datetime.now().date()
        matched = False
        for cell in await page.query_selector_all("td.datecol"):
            nobrs = await cell.query_selector_all("nobr")
            texts = [(await n.inner_text()).strip() for n in nobrs]
            if len(texts) >= 2:
                date_val = _parse_moodle_date(texts[0])
                if date_val == today:
                    real_time = texts[1]
                    try:
                        start_str, end_str = [s.strip() for s in real_time.split("-")]
                        start_str = _normalize_time_str(start_str)
                        end_str = _normalize_time_str(end_str)
                        start_fmt = datetime.strptime(start_str, "%I:%M%p").strftime("%H:%M")
                        end_fmt = datetime.strptime(end_str, "%I:%M%p").strftime("%H:%M")
                        new_time = f"{start_fmt} - {end_fmt}"
                        await update_schedule_time(course_name, new_time, user["schedule_file"], user)
                    except Exception as e:
                        print(f"⚠️ Failed to parse real time '{real_time}': {e}")
                    matched = True
                    break

        if not matched:
            print("ℹ️ No attendance row for today; skipped schedule correction.")

        # Attempt to submit "Present"
        try:
            await page.click("a:has-text('Submit attendance')", timeout=4_000)
            await page.wait_for_selector("label.form-check-label", timeout=4_000)

            labels = await page.query_selector_all("label.form-check-label")
            picked = False
            for label in labels:
                try:
                    span = await label.query_selector(".statusdesc")
                    if span and (await span.inner_text()).strip().lower() == "present":
                        radio = await label.query_selector("input")
                        await radio.click()
                        picked = True
                        break
                except Exception:
                    continue

            if not picked:
                raise Exception("Attendance statuses not found.")

            await page.click("#id_submitbutton")
            print(f"✅ Attendance submitted for {username}!")
            return True

        except Exception:
            raise Exception(f"Could not submit attendance for {course_name} (link closed or already submitted).")

    except Exception as e:
        if any(key in str(e) for key in [
            "not found on SPADA",
            "No attendance link found",
            "Could not submit attendance"
        ]):
            raise  # clean, user-facing error
        print(f"❌ Hard Error for {username}: {e}")
        raise Exception(f"Page timed out or failed to load for {course_name}.")
    finally:
        await context.close()
        await browser.close()


async def limited_login_and_attend(semaphore, playwright, user: dict, course_name: str, attempt: int):
    """
    Run a single attempt. Max attempts per day: 2.
    - attempt 1: notify on failure (info)
    - attempt 2: notify on failure (final)
    """
    async with semaphore:
        today = datetime.now().strftime("%Y-%m-%d")
        safe_course = _safe(course_name)

        if attempt > 2:
            print(f"❌ Already failed 2 times for {user['username']} - {course_name}. Skipping.")
            return

        try:
            print(f"Attempt {attempt}/2 for {user['username']} - {course_name}")
            success = await login_and_attend(playwright, user, course_name)

            if success:
                await notify_user(f"✅ {course_name} attendance submitted for {user['username']}.", user)
                # success flag
                flag_file = os.path.join(ATTENDANCE_FLAG_DIR, f"success_{user['username']}_{safe_course}_{today}.flag")
                with open(flag_file, "w") as f:
                    f.write("attended")
                _clear_retry_flags(user['username'], course_name, today)
                return

            # unrecoverable login failure
            await notify_user(f"❌ Login failed for {user['username']}. Please recheck credentials.", user)
            return

        except Exception as e:
            # retriable failure
            err = str(e)
            print(f"❌ Retriable failure for {user['username']}: {err}")

            # write this attempt flag
            retry_flag = os.path.join(ATTENDANCE_FLAG_DIR, f"retry_{user['username']}_{safe_course}_{today}_attempt_{attempt}.flag")
            try:
                with open(retry_flag, "w") as f:
                    f.write(f"failed on attempt {attempt}")
            except Exception as e2:
                print(f"Warning: could not write retry flag: {e2}")

            # remove previous attempt flag to keep only the latest
            if attempt > 1:
                prev_flag = os.path.join(ATTENDANCE_FLAG_DIR, f"retry_{user['username']}_{safe_course}_{today}_attempt_{attempt-1}.flag")
                if os.path.exists(prev_flag):
                    try:
                        os.remove(prev_flag)
                    except Exception as e3:
                        print(f"Warning: could not remove previous flag {prev_flag}: {e3}")

            # notify on attempt 1 and 2
            if attempt == 1:
                await notify_user(f"ℹ️ {err} (Attempt 1/2). Will try again.", user)
            elif attempt == 2:
                await notify_user(f"❌ Failed to attend {course_name} after 2 attempts. Last error: {err}", user)


# =============================================================================
# Main
# =============================================================================

async def run_main():
    cleanup_old_flags()

    users = load_users()
    if not users:
        print("No users found in .env")
        return

    semaphore = asyncio.Semaphore(4)

    async with async_playwright() as pw:
        tasks = []
        stagger = 0
        for user in users:
            schedule_path = user["schedule_file"]
            if not os.path.exists(schedule_path):
                print(f"Schedule file not found: {schedule_path}")
                continue

            schedule = load_schedule(schedule_path)
            current_class = get_current_class(schedule)

            if not current_class:
                print(f"No class at the moment for {user['username']}.")
                continue

            if has_attended_today(user["username"], current_class):
                continue

            if is_paused(user["username"], current_class):
                await notify_user(f"⏸️ Skipped attendance for {current_class} (paused).", user)
                continue

            current_attempt = get_current_attempt(user["username"], current_class)
            if current_attempt > 2:
                print(f"Skipping {current_class} for {user['username']}: already failed 2 attempts.")
                continue

            print(f"Current class for {user['username']}: {current_class} (Attempt {current_attempt}/2)")

            async def task(u=user, cls=current_class, wait=stagger, att=current_attempt):
                await asyncio.sleep(wait)  # mild staggering
                await limited_login_and_attend(semaphore, pw, u, cls, att)

            tasks.append(task())
            stagger += 2  # two seconds between user starts

        if tasks:
            await asyncio.gather(*tasks)


async def maybe_start_discord():
    """
    Start Discord client if token is present; otherwise mark as 'ready'
    so the rest of the flow (Telegram-only) can continue.
    """
    if DISCORD_TOKEN:
        try:
            await discord_client.start(DISCORD_TOKEN)
        except Exception as e:
            print(f"[Discord] Failed to start: {e}")
            # Let Telegram path continue
            discord_ready.set()
    else:
        discord_ready.set()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()

    # Start Discord (if configured) in background
    loop.create_task(maybe_start_discord())

    # Run main logic
    loop.run_until_complete(run_main())
