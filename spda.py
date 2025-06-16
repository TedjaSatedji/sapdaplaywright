import csv
from datetime import datetime
from dotenv import load_dotenv
import os
import requests
import asyncio
from playwright.async_api import async_playwright

# Load environment variables
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

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
        chat_id = os.getenv(f"TELEGRAM_CHAT_ID_{i}")
        schedule_file = os.getenv(f"SCHEDULE_FILE_{i}", "schedule.csv")  # Default fallback
        if all([username, password, chat_id]):
            users.append({
                "username": username,
                "password": password,
                "chat_id": chat_id,
                "schedule_file": schedule_file
            })
    return users

# --- Telegram Bot ---
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
    current_time = now.strftime("%H:%M")

    for entry in schedule:
        if entry["Day"] == today:
            start_time, end_time = entry["Time"].split(" - ")
            if start_time <= current_time <= end_time:
                return entry["CourseName"]
    return None

# --- Playwright Automation ---
async def login_and_attend(playwright, user, course_name):
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context()
    page = await context.new_page()

    username = user["username"]
    password = user["password"]
    chat_id = user["chat_id"]

    try:
        print(f"Processing attendance for {username}...")
        await page.goto("https://spada.upnyk.ac.id/login/index.php")

        await page.fill("#username", username)
        await page.fill("#password", password)
        await page.click("#loginbtn")

        await page.wait_for_timeout(3000)
        if "login/index.php" in page.url:
            print(f"❌ Login failed for {username}")
            send_telegram(f"❌ Login failed for {username}. Please recheck credentials.", chat_id)
            return

        # Find course
        links = await page.query_selector_all("a")
        course_link = None
        for link in links:
            text = (await link.inner_text()).strip().lower()
            if text.startswith(course_name.lower()):
                course_link = link
                break

        if not course_link:
            print(f"Course '{course_name}' not found for {username}")
            send_telegram(f"Course '{course_name}' not found for {username}", chat_id)
            return

        await course_link.click()
        await page.wait_for_timeout(2000)

        # Find attendance link
        links = await page.query_selector_all("a")
        att_link = None
        for link in links:
            text = (await link.inner_text()).lower()
            if "presensi" in text or "attendance" in text:
                att_link = link
                break

        if not att_link:
            print(f"No attendance link for {username}")
            send_telegram(f"No attendance link found in {course_name} for you.", chat_id)
            return

        await att_link.click()
        await page.wait_for_timeout(2000)

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
            send_telegram(f"✅ {course_name} attendance submitted successfully for {username}.", chat_id)
        except:
            print(f"ℹ️ Could not submit attendance for {username}.")
            send_telegram(f"ℹ️ No active attendance found for {course_name}. {username}", chat_id)

    except Exception as e:
        print(f"❌ Error for {username}: {e}")
        send_telegram(f"❌ Error during attendance for {username}: {e}", chat_id)
    finally:
        await context.close()
        await browser.close()

# --- Main ---
async def main():
    users = load_users()
    if not users:
        print("No users found in .env")
        return

    async with async_playwright() as playwright:
        tasks = []
        for user in users:
            schedule_path = user["schedule_file"]
            if not os.path.exists(schedule_path):
                print(f"Schedule file not found: {schedule_path}")
                continue
            schedule = load_schedule(schedule_path)
            current_class = get_current_class(schedule)
            if current_class:
                print(f"Current class for {user['username']}: {current_class}")
                # Each user gets their own login_and_attend task, even if schedule file is the same
                tasks.append(login_and_attend(playwright, user, current_class))
            else:
                print(f"No class at the moment for {user['username']}.")
        if tasks:
            await asyncio.gather(*tasks)
if __name__ == "__main__":
    asyncio.run(main())
