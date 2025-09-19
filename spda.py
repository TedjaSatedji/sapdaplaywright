
import csv
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os
import requests
import asyncio
from playwright.async_api import async_playwright
import discord

FLAG_DIR = "flags"
os.makedirs(FLAG_DIR, exist_ok=True)

# Load environment variables
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# --- Discord Client ---
intents = discord.Intents.default()
discord_client = discord.Client(intents=intents)
discord_ready = asyncio.Event()

@discord_client.event
async def on_ready():
    print(f"[Discord] Logged in as {discord_client.user}")
    discord_ready.set()

async def send_discord(message, user_id):
    if not DISCORD_TOKEN or not user_id:
        print("Discord token or user ID is missing. Skipping Discord message.")
        return
    await discord_ready.wait()
    try:
        user = await discord_client.fetch_user(int(user_id))
        await user.send(message)
    except Exception as e:
        print(f"Failed to send Discord message to {user_id}: {e}")

# --- Telegram ---
def send_telegram(message, chat_id):
    if not TELEGRAM_TOKEN or not chat_id:
        print("Telegram token or chat ID is missing. Skipping notification.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"Failed to send Telegram message to {chat_id}: {e}")

async def notify_user(message, user):
    if user.get("use_discord"):
        await send_discord(message, user["chat_id"])
    else:
        send_telegram(message, user["chat_id"])

# --- User Loading ---
def load_users():
    users = []
    env_keys = os.environ.keys()
    indices = set()
    for key in env_keys:
        if key.startswith("SPADA_USERNAME_"):
            try:
                idx = int(key.split("_")[-1])
                indices.add(idx)
            except ValueError:
                continue
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

# --- Pause Check ---
def is_paused(username: str, course_name: str) -> bool:
    """Check if attendance should be skipped due to pause flags."""
    # Indefinite pause
    pause_file = os.path.join(FLAG_DIR, f"pause_user_{username}.flag")
    if os.path.exists(pause_file):
        print(f"⏸️ {username} is paused indefinitely.")
        return True

    # One-time pause for this class
    once_file = os.path.join(FLAG_DIR, f"pause_once_{username}_{course_name.replace(' ','_')}.flag")
    if os.path.exists(once_file):
        print(f"⏸️ Skipping one class {course_name} for {username}.")
        os.remove(once_file)  # consume the flag
        return True

    return False

# --- Schedule ---
def load_schedule(csv_file):
    with open(csv_file, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))

def get_current_class(schedule):
    now = datetime.now()
    day_map = {
        "Monday": "Senin", "Tuesday": "Selasa", "Wednesday": "Rabu",
        "Thursday": "Kamis", "Friday": "Jumat", "Saturday": "Sabtu", "Sunday": "Minggu"
    }
    today = day_map[now.strftime("%A")]
    current_time = now

    for entry in schedule:
        if entry["Day"] == today:
            start_time_str, end_time_str = entry["Time"].split(" - ")
            start_time = datetime.strptime(start_time_str, "%H:%M").replace(
                year=current_time.year, month=current_time.month, day=current_time.day
            )
            end_time = datetime.strptime(end_time_str, "%H:%M").replace(
                year=current_time.year, month=current_time.month, day=current_time.day
            )

            # Allow attendance if within 15 minutes of start
            if start_time <= current_time <= start_time + timedelta(minutes=15):
                return entry["CourseName"]

    return None

async def update_schedule_time(course_name, new_time, schedule_file, user):
    """Update the schedule CSV only if time changed, and notify the user."""
    rows = []
    updated = False
    old_time = None

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

        msg = f"⏰ Your schedule for *{course_name}* was corrected from **{old_time}** to **{new_time}**."
        print(msg)
        await notify_user(msg, user)
        
# --- helper: parse Moodle date strings like "Sat 6 Sep 2025" / "Friday, 12 September 2025"
def _parse_moodle_date(s: str):
    s = s.strip()
    fmts = [
        "%a %d %b %Y",        # Sat 06 Sep 2025 / Sat 6 Sep 2025
        "%a %d %B %Y",        # Sat 06 September 2025
        "%A %d %b %Y",        # Saturday 6 Sep 2025
        "%A %d %B %Y",        # Saturday 6 September 2025
        "%d %b %Y",           # 6 Sep 2025
        "%d %B %Y",           # 6 September 2025
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


# --- Playwright Automation ---
def normalize_time_str(t: str) -> str:
    t = t.strip().upper()
    # If missing minutes (like "10AM"), add ":00"
    if re.match(r"^\d{1,2}(AM|PM)$", t):
        t = t.replace("AM", ":00AM").replace("PM", ":00PM")
    return t

async def login_and_attend(playwright, user, course_name):
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context()
    page = await context.new_page()

    username = user["username"]
    password = user["password"]

    try:
        print(f"Processing attendance for {username}...")
        for attempt in range(3):
            try:
                await page.goto("https://spada.upnyk.ac.id/login/index.php", timeout=60000)
                break
            except Exception as e:
                if attempt == 2:
                    print(f"❌ Failed to load login page for {username} after 3 attempts.")
                    await notify_user(f"❌ Error loading the page for {username}, ask the admin.", user)
                    return
                await asyncio.sleep(5)

        await page.fill("#username", username)
        await page.fill("#password", password)
        await page.click("#loginbtn")

        await page.wait_for_timeout(3000)
        if "login/index.php" in page.url:
            print(f"❌ Login failed for {username}")
            await notify_user(f"❌ Login failed for {username}. Please recheck credentials.", user)
            return

        links = await page.query_selector_all("a")
        course_link = None
        for link in links:
            text = (await link.inner_text()).strip().lower()
            if text.startswith(course_name.lower()):
                course_link = link
                break

        if not course_link:
            print(f"Course '{course_name}' not found for {username}")
            await notify_user(f"Course '{course_name}' not found for {username}", user)
            return

        await course_link.click()
        await page.wait_for_timeout(2000)
        
        att_link = None
        activities = await page.query_selector_all("li.activity.attendance a")
        for activity in activities:
            text = (await activity.inner_text()).lower()
            if "attendance" in text or "presensi" in text:
                att_link = activity
                break

        if not att_link:
            print(f"No attendance link for {username}")
            await notify_user(f"No attendance link found in {course_name} for you.", user)
            return

        await att_link.click()
        await page.wait_for_timeout(2000)

        # Scrape the <td class="datecol"> cells and use ONLY today's row
        time_cells = await page.query_selector_all("td.datecol")
        today = datetime.now().date()  # use system tz of the runner
        
        matched = False
        for cell in time_cells:
            nobrs = await cell.query_selector_all("nobr")
            texts = [ (await n.inner_text()).strip() for n in nobrs ]
        
            # Expect: texts[0] = date, texts[1] = "3:30PM - 3:45PM"
            if len(texts) >= 2:
                date_text = texts[0]
                date_val = _parse_moodle_date(date_text)
                if date_val == today:
                    real_time = texts[1]
                    try:
                        start_str, end_str = real_time.split("-")
                        start_str = normalize_time_str(start_str)
                        end_str = normalize_time_str(end_str)
        
                        start_fmt = datetime.strptime(start_str.strip(), "%I:%M%p").strftime("%H:%M")
                        end_fmt = datetime.strptime(end_str.strip(), "%I:%M%p").strftime("%H:%M")
                        new_time = f"{start_fmt} - {end_fmt}"
                        await update_schedule_time(course_name, new_time, user["schedule_file"], user)
                    except Exception as e:
                        print(f"⚠️ Failed to parse real time {real_time}: {e}")
                    matched = True
                    break
                
        if not matched:
            print("ℹ️ No attendance row for today; skipping schedule correction to avoid stale times.")


        try:
            await page.click("a:has-text('Submit attendance')", timeout=4000)
            await page.wait_for_selector("label.form-check-label", timeout=4000)

            labels = await page.query_selector_all("label.form-check-label")
            for label in labels:
                try:
                    span = await label.query_selector(".statusdesc")
                    if span and (await span.inner_text()).strip().lower() == "present":
                        radio = await label.query_selector("input")
                        await radio.click()
                        break
                except:
                    continue

            await page.click("#id_submitbutton")
            print(f"✅ Attendance submitted for {username}!")
            await notify_user(f"✅ {course_name} attendance submitted successfully for {username}.", user)
        except:
            print(f"ℹ️ Could not submit attendance for {username}.")
            await notify_user(f"ℹ️ No active attendance found for {course_name}. {username}", user)

    except Exception as e:
        print(f"❌ Error for {username}: {e}")
        await notify_user(f"❌ Error during attendance for {username}, ask the admin.", user)
    finally:
        await context.close()
        await browser.close()

async def limited_login_and_attend(semaphore, playwright, user, course_name):
    async with semaphore:
        await login_and_attend(playwright, user, course_name)

# --- Main ---

async def main():
    users = load_users()
    if not users:
        print("No users found in .env")
        return

    semaphore = asyncio.Semaphore(4)

    async with async_playwright() as playwright:
        tasks = []
        delay = 0
        for user in users:
            schedule_path = user["schedule_file"]
            if not os.path.exists(schedule_path):
                print(f"Schedule file not found: {schedule_path}")
                continue
            schedule = load_schedule(schedule_path)
            current_class = get_current_class(schedule)
            if current_class:
                # Check pause before scheduling attendance
                if is_paused(user["username"], current_class):
                    await notify_user(f"⏸️ Skipped attendance for {current_class} (paused).", user)
                    continue

                print(f"Current class for {user['username']}: {current_class}")
                async def delayed_task(user=user, current_class=current_class, delay=delay):
                    await asyncio.sleep(delay)
                    await limited_login_and_attend(semaphore, playwright, user, current_class)
                tasks.append(delayed_task())
                delay += 2
            else:
                print(f"No class at the moment for {user['username']}.")
        if tasks:
            await asyncio.gather(*tasks)

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(discord_client.start(DISCORD_TOKEN))
    loop.run_until_complete(main())
