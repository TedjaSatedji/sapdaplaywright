import csv
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os
import requests
import asyncio
from playwright.async_api import async_playwright
import discord

# --- NEW: Flag Directory Structure ---
FLAG_DIR = "flags"
ATTENDANCE_FLAG_DIR = os.path.join(FLAG_DIR, "attendance")
os.makedirs(FLAG_DIR, exist_ok=True)
os.makedirs(ATTENDANCE_FLAG_DIR, exist_ok=True) # Create the new subfolder

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

# --- Pause/Attendance Check ---
def is_paused(username: str, course_name: str) -> bool:
    """Check if attendance should be skipped due to pause flags."""
    # Pause flags stay in the main FLAG_DIR
    pause_file = os.path.join(FLAG_DIR, f"pause_user_{username}.flag")
    if os.path.exists(pause_file):
        print(f"⏸️ {username} is paused indefinitely.")
        return True

    safe_course = course_name.replace(' ','_')
    once_file = os.path.join(FLAG_DIR, f"pause_once_{username}_{safe_course}.flag")
    if os.path.exists(once_file):
        print(f"⏸️ Skipping one class {course_name} for {username}.")
        os.remove(once_file)  # consume the flag
        return True

    return False

def has_attended_today(username: str, course_name: str) -> bool:
    """Check if a success flag already exists for this class today."""
    today = datetime.now().strftime("%Y-%m-%d")
    safe_course = course_name.replace(' ','_')
    # Success flags are now in the ATTENDANCE_FLAG_DIR
    flag_file = os.path.join(ATTENDANCE_FLAG_DIR, f"success_{username}_{safe_course}_{today}.flag")
    
    if os.path.exists(flag_file):
        print(f"✅ {username} has already attended {course_name} today.")
        return True
    return False

# --- Retry Attempt Tracking ---
def get_current_attempt(username: str, course_name: str) -> int:
    """Checks for retry flags to determine the current attempt number."""
    today = datetime.now().strftime("%Y-%m-%d")
    safe_course = course_name.replace(' ','_')
    base_flag_name = f"retry_{username}_{safe_course}_{today}_attempt_"
    
    # Check in REVERSE order
    if os.path.exists(os.path.join(ATTENDANCE_FLAG_DIR, f"{base_flag_name}3.flag")):
        return 4  # This signals "don't even try"
    if os.path.exists(os.path.join(ATTENDANCE_FLAG_DIR, f"{base_flag_name}2.flag")):
        return 3  # This is the 3rd attempt
    if os.path.exists(os.path.join(ATTENDANCE_FLAG_DIR, f"{base_flag_name}1.flag")):
        return 2  # This is the 2nd attempt
    
    return 1 # This is the 1st attempt

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

async def login_and_attend(playwright, user, course_name) -> bool:
    """
    Tries to attend a class.
    Returns True on success.
    Returns False on "login fail" (unrecoverable).
    Raises Exception on ALL other failures (retriable).
    """
    browser = await playwright.firefox.launch(headless=True)
    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    username = user["username"]
    password = user["password"]

    try:
        print(f"Processing attendance for {username}...")
        await page.goto("https://spada.upnyk.ac.id/login/index.php", timeout=60000)

        await page.fill("#username", username)
        await page.fill("#password", password)
        await page.click("#loginbtn")

        await page.wait_for_timeout(3000)
        if "login/index.php" in page.url:
            print(f"❌ Login failed for {username}")
            return False # UNRECOVERABLE (soft fail)

        links = await page.query_selector_all("a")
        course_link = None
        for link in links:
            text = (await link.inner_text()).strip().lower()
            if text.startswith(course_name.lower()):
                course_link = link
                break

        if not course_link:
            print(f"Course '{course_name}' not found for {username}")
            raise Exception(f"Course '{course_name}' not found on SPADA.") # RETRIABLE

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
            raise Exception(f"No attendance link found for {course_name}.") # RETRIABLE

        await att_link.click()
        await page.wait_for_timeout(2000)

        time_cells = await page.query_selector_all("td.datecol")
        today = datetime.now().date()
        
        matched = False
        for cell in time_cells:
            nobrs = await cell.query_selector_all("nobr")
            texts = [ (await n.inner_text()).strip() for n in nobrs ]
        
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
            return True # SUCCESS
        
        except Exception as e:
            print(f"ℹ️ Could not submit attendance for {username} (e.g., already submitted or link closed).")
            raise Exception(f"Could not submit attendance for {course_name} (link closed or already submitted).") # RETRIABLE

    except Exception as e:
        if "not found on SPADA" in str(e) or "No attendance link found" in str(e) or "Could not submit attendance" in str(e):
             raise e # Re-raise our nice error
        else:
             print(f"❌ Hard Error for {username}: {e}")
             raise Exception(f"Page timed out or failed to load for {course_name}.") # Generic hard failure
    
    finally:
        await context.close()
        await browser.close()

def _clear_retry_flags(username: str, course_name: str, today: str):
    """Helper to delete all retry flags for a user/course/day."""
    safe_course = course_name.replace(' ','_')
    base_flag_name = f"retry_{username}_{safe_course}_{today}_attempt_"
    for i in range(1, 4): # Clear all possible attempt flags
        flag_path = os.path.join(ATTENDANCE_FLAG_DIR, f"{base_flag_name}{i}.flag")
        if os.path.exists(flag_path):
            try:
                os.remove(flag_path)
            except Exception as e:
                print(f"Warning: could not remove retry flag {flag_path}: {e}")


async def limited_login_and_attend(semaphore, playwright, user, course_name, attempt):
    """Manages ONE attempt and notifications for the attendance task."""
    async with semaphore:
        
        today = datetime.now().strftime("%Y-%m-%d")
        safe_course = course_name.replace(' ','_')
        
        if attempt > 3:
            print(f"❌ Already failed 3 times for {user['username']} - {course_name}. Skipping.")
            return

        try:
            print(f"Attempt {attempt}/3 for {user['username']} - {course_name}")
            
            success = await login_and_attend(playwright, user, course_name)
            
            if success:
                print(f"✅ Success for {user['username']}")
                await notify_user(f"✅ {course_name} attendance submitted successfully for {user['username']}.", user)
                
                # Create success flag in the subfolder
                flag_file = os.path.join(ATTENDANCE_FLAG_DIR, f"success_{user['username']}_{safe_course}_{today}.flag")
                with open(flag_file, "w") as f:
                    f.write("attended")
                
                # Clear all retry flags on success
                _clear_retry_flags(user['username'], course_name, today)
                return  # Success, we are done

            else:
                # This block is ONLY reached if login_and_attend returned False (Login Fail)
                print(f"❌ Login failed for {user['username']}. No retry.")
                await notify_user(f"❌ Login failed for {user['username']}. Please recheck credentials.", user)
                return # Don't retry login failures

        except Exception as e:
            # This block is for ALL other failures (no link, timeout, etc.)
            print(f"❌ Retriable failure for {user['username']}: {e}")
            user_friendly_error = str(e)
            
            # --- Renaming Logic ---
            # 1. Create the new attempt flag
            retry_flag_file = os.path.join(ATTENDANCE_FLAG_DIR, f"retry_{user['username']}_{safe_course}_{today}_attempt_{attempt}.flag")
            with open(retry_flag_file, "w") as f:
                f.write(f"failed on attempt {attempt}")
            
            # 2. Delete the PREVIOUS attempt flag (if it exists)
            if attempt > 1:
                prev_attempt = attempt - 1
                prev_flag_file = os.path.join(ATTENDANCE_FLAG_DIR, f"retry_{user['username']}_{safe_course}_{today}_attempt_{prev_attempt}.flag")
                if os.path.exists(prev_flag_file):
                    try:
                        os.remove(prev_flag_file)
                    except Exception as e:
                        print(f"Warning: could not remove previous flag {prev_flag_file}: {e}")

            # --- MODIFIED: Send notification on FIRST and LAST attempt ONLY ---
            if attempt == 1:
                # First failure
                await notify_user(f"ℹ️ {user_friendly_error} (Attempt 1/3). Will retry on the next run.", user)
            elif attempt == 3:
                # Final failure
                await notify_user(f"❌ Failed to attend {course_name} after 3 attempts. (Last error: {user_friendly_error})", user)
            # On attempt 2, no notification will be sent.
            
            return

# --- Flag Cleanup Function ---
def cleanup_old_flags():
    """Deletes any success OR retry flags not from today."""
    print("🧹 Running cleanup for old flags...")
    today_suffix = f"_{datetime.now().strftime('%Y-%m-%d')}"
    
    # Scan the subfolder
    for filename in os.listdir(ATTENDANCE_FLAG_DIR):
        # We only care about success and retry flags
        if filename.startswith("success_") or filename.startswith("retry_"):
            if today_suffix not in filename:
                print(f"Removing old flag: {filename}")
                try:
                    os.remove(os.path.join(ATTENDANCE_FLAG_DIR, filename))
                except Exception as e:
                    print(f"Failed to remove {filename}: {e}")

# --- Main ---
async def main():
    cleanup_old_flags()

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
                if has_attended_today(user["username"], current_class):
                    continue 

                if is_paused(user["username"], current_class):
                    await notify_user(f"⏸️ Skipped attendance for {current_class} (paused).", user)
                    continue
                
                # --- FIXED: Check attempt number before scheduling ---
                current_attempt = get_current_attempt(user["username"], current_class)

                if current_attempt > 3:
                    print(f"Skipping {current_class} for {user['username']}: Already failed 3 attempts.")
                    continue # Skip this user, all attempts used

                print(f"Current class for {user['username']}: {current_class} (Attempt {current_attempt})")
                async def delayed_task(user=user, current_class=current_class, delay=delay, attempt=current_attempt):
                    await asyncio.sleep(delay)
                    await limited_login_and_attend(semaphore, playwright, user, current_class, attempt)
                
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