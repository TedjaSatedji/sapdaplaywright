import os
import io
import asyncio
import re
from datetime import datetime
from dotenv import load_dotenv
import requests
import urllib3
import discord
from discord import app_commands
import google.generativeai as genai

# ==========================
# Boot & Paths
# ==========================
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ENV_FILE = ".env"
SPADA_LOGIN_URL = "https://spada.upnyk.ac.id/login/index.php"

FLAG_DIR = "flags"
SCHEDULE_DIR = "schedules"
os.makedirs(FLAG_DIR, exist_ok=True)
os.makedirs(SCHEDULE_DIR, exist_ok=True)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================
# State (UI flow)
# ==========================
user_states = {}        # during /setup: user_id -> "awaiting_username"/"awaiting_password"
user_temp_data = {}     # during /setup: user_id -> {"username": ..., "password": ...}
waiting_upload = set()  # user_ids expecting an image upload (after pressing Upload)
pending_csv = {}        # user_id -> csv text awaiting Save/Cancel

# ==========================
# Gemini
# ==========================
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-2.5-flash-lite")

def parse_schedule_with_gemini(image_bytes: bytes) -> str:
    """
    Uses Gemini to parse a schedule image and return CSV rows with header.
    Columns in exact order: CourseName,Day,Time
    """
    image_data = {"mime_type": "image/jpeg", "data": image_bytes}
    prompt = (
        "Extract the class schedule from this image and return only CSV rows. "
        "Columns must be in this exact order: CourseName,Day,Time. "
        "Example:\n"
        "CourseName,Day,Time\n"
        "Matematika,Senin,07:00 - 09:00\n"
        "Fisika,Rabu,10:00 - 12:00\n"
        "Always add column name"
		"Do not add ```csv``` or any code fences\n"
        "Do not forget the space before and after hyphen for the time\n"
        "Do not include class, explanations, or extra text. only Course Name, Day and Time."
    )
    resp = gemini_model.generate_content([prompt, image_data])
    return (resp.text or "").strip()

# ==========================
# ENV helpers
# ==========================

def find_user_index_by_id(user_id: str):
    if not os.path.exists(ENV_FILE):
        return None
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("DISCORD_USER_ID_") and line.strip().split("=", 1)[1] == user_id if "=" in line else False:
                return line.split("_")[-1].split("=")[0].strip()
    return None


def find_username_by_id(user_id: str) -> str | None:
    idx = find_user_index_by_id(user_id)
    if not idx:
        return None
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith(f"SPADA_USERNAME_{idx}="):
                return line.strip().split("=", 1)[1]
    return None


def find_schedule_path(user_id: str) -> str | None:
    idx = find_user_index_by_id(user_id)
    if not idx:
        return None
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith(f"SCHEDULE_FILE_{idx}="):
                return line.strip().split("=", 1)[1]
    return None


def find_saved_credentials(user_id: str) -> dict | None:
    idx = find_user_index_by_id(user_id)
    if not idx or not os.path.exists(ENV_FILE):
        return None

    username = None
    password = None
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith(f"SPADA_USERNAME_{idx}="):
                username = line.strip().split("=", 1)[1] if "=" in line else None
            elif line.startswith(f"SPADA_PASSWORD_{idx}="):
                password = line.strip().split("=", 1)[1] if "=" in line else None

    if not username or not password:
        return None

    return {"username": username, "password": password}


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
                return False, "saved credentials were rejected by SPADA. Please check and update them."
            return False, "SPADA kept the session on the login page. Credentials may be wrong."

    try:
        return attempt_login(verify=True)
    except requests.exceptions.SSLError:
        try:
            return attempt_login(verify=False)
        except requests.Timeout:
            return False, "SPADA did not respond in time. Try again later."
        except requests.RequestException as exc:
            return False, f"couldn't reach SPADA after SSL fallback: {exc}"
    except requests.Timeout:
        return False, "SPADA did not respond in time. Try again later."
    except requests.RequestException as exc:
        return False, f"couldn't reach SPADA: {exc}"


def rename_user_flags(old_username: str, new_username: str):
    if not old_username or old_username == new_username:
        return

    rename_specs = [
        (FLAG_DIR, f"pause_user_{old_username}", f"pause_user_{new_username}"),
        (FLAG_DIR, f"pause_once_{old_username}_", f"pause_once_{new_username}_"),
        (os.path.join(FLAG_DIR, "attendance"), f"success_{old_username}_", f"success_{new_username}_"),
        (os.path.join(FLAG_DIR, "attendance"), f"retry_{old_username}_", f"retry_{new_username}_"),
    ]

    for directory, old_prefix, new_prefix in rename_specs:
        if not os.path.isdir(directory):
            continue
        for filename in os.listdir(directory):
            if not filename.startswith(old_prefix):
                continue
            old_path = os.path.join(directory, filename)
            new_path = os.path.join(directory, filename.replace(old_prefix, new_prefix, 1))
            try:
                os.replace(old_path, new_path)
            except Exception:
                pass


def update_credentials(user_id: str, creds: dict) -> bool:
    if not os.path.exists(ENV_FILE):
        return False

    with open(ENV_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    new_lines = []
    found = False
    old_username = None
    i = 0

    while i < len(lines):
        if (
            i + 4 < len(lines)
            and lines[i].startswith("#---")
            and lines[i + 1].startswith("SPADA_USERNAME_")
            and lines[i + 2].startswith("SPADA_PASSWORD_")
            and lines[i + 3].startswith("DISCORD_USER_ID_")
            and lines[i + 4].startswith("SCHEDULE_FILE_")
        ):
            current_user_id = lines[i + 3].strip().split("=", 1)[1] if "=" in lines[i + 3] else ""
            if current_user_id == user_id:
                index = lines[i + 1].split("_")[-1].split("=")[0].strip()
                old_username = lines[i + 1].strip().split("=", 1)[1] if "=" in lines[i + 1] else None
                new_lines.extend([
                    f"#--- {creds['username']} ---\n",
                    f"SPADA_USERNAME_{index}={creds['username']}\n",
                    f"SPADA_PASSWORD_{index}={creds['password']}\n",
                    lines[i + 3],
                    lines[i + 4],
                ])
                found = True
                i += 5
                continue

        new_lines.append(lines[i])
        i += 1

    if not found:
        return False

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    if old_username:
        rename_user_flags(old_username, creds["username"])
    return True


def get_next_index():
    if not os.path.exists(ENV_FILE):
        return 1
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        indices = [
            int(line.split("_")[-1].split("=")[0])
            for line in f if line.startswith("SPADA_USERNAME_")
        ]
        return max(indices) + 1 if indices else 1


def save_to_env(user_id: str, creds: dict):
    idx = get_next_index()
    schedule_path = f"{SCHEDULE_DIR}/schedule_{idx}.csv"
    if not os.path.exists(schedule_path):
        open(schedule_path, "w", encoding="utf-8").close()
    with open(ENV_FILE, "a", encoding="utf-8") as f:
        f.write(f"#--- {creds['username']} ---\n")
        f.write(f"SPADA_USERNAME_{idx}={creds['username']}\n")
        f.write(f"SPADA_PASSWORD_{idx}={creds['password']}\n")
        f.write(f"DISCORD_USER_ID_{idx}={user_id}\n")
        f.write(f"SCHEDULE_FILE_{idx}={schedule_path}\n")


def delete_credentials(user_id: str) -> bool:
    if not os.path.exists(ENV_FILE):
        return False
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
    new_lines = []
    found = False
    i = 0
    schedule_path = None
    username = None
    while i < len(lines):
        if (
            i + 4 < len(lines)
            and lines[i].startswith("#---")
            and lines[i+1].startswith("SPADA_USERNAME_")
            and lines[i+2].startswith("SPADA_PASSWORD_")
            and lines[i+3].startswith("DISCORD_USER_ID_")
            and lines[i+4].startswith("SCHEDULE_FILE_")
            and lines[i+3].strip().split("=", 1)[1] == user_id
        ):
            found = True
            username = lines[i+1].strip().split("=", 1)[1]
            schedule_path = lines[i+4].strip().split("=", 1)[1]
            i += 5
        else:
            new_lines.append(lines[i])
            i += 1
    if found:
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        # remove schedule file entirely
        if schedule_path and os.path.exists(schedule_path):
            try:
                os.remove(schedule_path)
            except Exception:
                pass
        # remove all pause flags (indefinite and once)
        if username:
            for f in os.listdir(FLAG_DIR):
                if f.startswith(f"pause_user_{username}") or f.startswith(f"pause_once_{username}_"):
                    try:
                        os.remove(os.path.join(FLAG_DIR, f))
                    except Exception:
                        pass
    return found

# ==========================
# Pause helpers
# ==========================

def get_next_class(schedule_path: str):
    """Return the name of the next upcoming class (best-effort) for pauseonce flag naming."""
    if not os.path.exists(schedule_path) or os.path.getsize(schedule_path) == 0:
        return None
    now = datetime.now()
    closest_class, closest_start = None, None
    with open(schedule_path, "r", encoding="utf-8") as f:
        lines = f.readlines()[1:]  # skip header
    for line in lines:
        parts = line.strip().split(",")
        if len(parts) < 3:
            continue
        course, day, time_str = parts
        try:
            start_str, end_str = time_str.split(" - ")
            # NOTE: we ignore the day mapping here for simplicity and compare times today
            start_time = datetime.strptime(start_str, "%H:%M").replace(
                year=now.year, month=now.month, day=now.day
            )
        except Exception:
            continue
        if start_time > now:
            if closest_start is None or start_time < closest_start:
                closest_start, closest_class = start_time, course
    return closest_class

# ==========================
# Discord UI (Upload/View/Delete + Confirm Save/Cancel)
# ==========================
class ScheduleMenu(discord.ui.View):
    def __init__(self, user_id: str):
        super().__init__(timeout=180)
        self.user_id = user_id
        
    @discord.ui.button(label="🖼 Upload Schedule Imagae", style=discord.ButtonStyle.primary)
    async def upload(self, interaction: discord.Interaction, button: discord.ui.Button):
        waiting_upload.add(self.user_id)
        pending_csv.pop(self.user_id, None)
        await interaction.response.send_message("🖼 Send your schedule image now (png/jpg).", ephemeral=True)


    @discord.ui.button(label="⬆️ Manual CSV Upload", style=discord.ButtonStyle.success)
    async def upload_csv(self, interaction: discord.Interaction, button: discord.ui.Button):
        waiting_upload.add(f"csv_{self.user_id}")
        pending_csv.pop(self.user_id, None)
        await interaction.response.send_message("⬆️ Send your CSV schedule file now.", ephemeral=True)

    @discord.ui.button(label="📄 View Schedule", style=discord.ButtonStyle.secondary)
    async def view(self, interaction: discord.Interaction, button: discord.ui.Button):
        schedule_path = find_schedule_path(self.user_id)
        if not schedule_path or not os.path.exists(schedule_path) or os.path.getsize(schedule_path) == 0:
            await interaction.response.send_message("⚠️ No schedule saved yet.", ephemeral=True)
            return
        await interaction.response.send_message("📄 Your saved schedule:", file=discord.File(schedule_path), ephemeral=True)

    @discord.ui.button(label="🗑 Delete Schedule", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        schedule_path = find_schedule_path(self.user_id)
        if schedule_path and os.path.exists(schedule_path):
            try:
                os.remove(schedule_path)
            except Exception:
                pass
            await interaction.response.send_message("🗑 Schedule deleted.", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ No schedule to delete.", ephemeral=True)


class ConfirmMenu(discord.ui.View):
    def __init__(self, user_id: str, csv_text: str):
        super().__init__(timeout=240)
        self.user_id = user_id
        self.csv_text = csv_text

    @discord.ui.button(label="✅ Save", style=discord.ButtonStyle.success)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        schedule_path = find_schedule_path(self.user_id)
        if not schedule_path:
            await interaction.response.send_message("❌ Could not locate your schedule file in .env.", ephemeral=True)
            return
        try:
            with open(schedule_path, "w", encoding="utf-8") as f:
                f.write(self.csv_text + ("" if not self.csv_text.endswith("") else ""))
            pending_csv.pop(self.user_id, None)
            await interaction.response.send_message(f"✅ Schedule saved to `{schedule_path}`", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to save: `{e}`", ephemeral=True)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        pending_csv.pop(self.user_id, None)
        waiting_upload.discard(self.user_id)
        await interaction.response.send_message("❌ Schedule upload cancelled.", ephemeral=True)


# ==========================
# Slash commands (union of original + new functionality)
# ==========================
@tree.command(name="start", description="Show help / available commands")
async def start(interaction: discord.Interaction):
    await help_command.callback(interaction)  # alias to /help


@tree.command(name="help", description="Show a help message with available commands")
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        "👋 Hello! Here’s what I can do for you:\n"
        "• **/help** – show this help message\n"
        "• **/setup** – link your SPADA account\n"        "• **/credsedit** – update your saved SPADA username/password\n"
        "• **/credscheck** – verify your saved SPADA credentials\n"        "• **/mystatus** – show your SPADA user, schedule, and pause status\n"
        "• **/pause** – pause attendance indefinitely\n"
        "• **/resume** – resume attendance if paused\n"
        "• **/pauseonce** – skip attendance for your next class\n"
        "• **/delete** – remove your saved credentials\n"
        "• **/schedule** – upload your class schedule\n",
        ephemeral=True
    )


@tree.command(name="cancel", description="Cancel the current setup or pending upload")
async def cancel(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    user_states.pop(user_id, None)
    user_temp_data.pop(user_id, None)
    waiting_upload.discard(user_id)
    pending_csv.pop(user_id, None)
    await interaction.response.send_message("❌ Cancelled.", ephemeral=True)


@tree.command(name="setup", description="Link your SPADA account (guided)")
async def setup(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if find_username_by_id(user_id):
        await interaction.response.send_message("⚠️ You already have an account linked. Use /credsedit to update.", ephemeral=True)
        return

    user_states[user_id] = "awaiting_username"
    await interaction.response.send_message("🟢 What is your SPADA username/NIM?", ephemeral=True)

    def check_u(m: discord.Message):
        return m.author == interaction.user and m.channel == interaction.channel

    try:
        msg_u = await client.wait_for("message", check=check_u, timeout=120)
    except asyncio.TimeoutError:
        user_states.pop(user_id, None)
        await interaction.followup.send("❌ Timed out waiting for username.", ephemeral=True)
        return

    if user_states.get(user_id) != "awaiting_username":
        await interaction.followup.send("❌ Setup cancelled.", ephemeral=True)
        return

    username = msg_u.content.strip()
    user_temp_data[user_id] = {"username": username}
    user_states[user_id] = "awaiting_password"
    await interaction.followup.send("🔐 What is your SPADA password?⚠️ Stored in plain text. Use a unique password.", ephemeral=True)

    def check_p(m: discord.Message):
        return m.author == interaction.user and m.channel == interaction.channel

    try:
        msg_p = await client.wait_for("message", check=check_p, timeout=120)
    except asyncio.TimeoutError:
        user_states.pop(user_id, None)
        user_temp_data.pop(user_id, None)
        await interaction.followup.send("❌ Timed out waiting for password.", ephemeral=True)
        return

    if user_states.get(user_id) != "awaiting_password":
        await interaction.followup.send("❌ Setup cancelled.", ephemeral=True)
        return

    password = msg_p.content.strip()
    user_temp_data[user_id]["password"] = password

    save_to_env(user_id, user_temp_data[user_id])
    user_states.pop(user_id, None)
    user_temp_data.pop(user_id, None)

    await interaction.followup.send("✅ Credentials saved!", ephemeral=True)
    await interaction.followup.send("💡 Don’t forget to upload your schedule with /schedule → Upload Schedule.", ephemeral=True)

@tree.command(name="credscheck", description="Verify your saved SPADA credentials")
async def credscheck(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    creds = find_saved_credentials(user_id)

    if not creds:
        await interaction.response.send_message("⚠️ No saved credentials found. Run /setup first.", ephemeral=True)
        return

    await interaction.response.send_message("🔎 Checking your saved SPADA credentials...", ephemeral=True)
    is_valid, result_message = verify_spada_credentials(creds["username"], creds["password"])

    if is_valid:
        await interaction.followup.send(f"✅ {result_message}", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ {result_message}", ephemeral=True)


@tree.command(name="credsedit", description="Update your saved SPADA username/password")
async def credsedit(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    current_username = find_username_by_id(user_id)

    if not current_username:
        await interaction.response.send_message("⚠️ No saved credentials found. Run /setup first.", ephemeral=True)
        return

    user_states[user_id] = "changing_username"
    await interaction.response.send_message(
        f"🟡 Your current SPADA username/NIM is **{current_username}**\n\nSend your new username/NIM now.",
        ephemeral=True
    )

    def check_u(m: discord.Message):
        return m.author == interaction.user and m.channel == interaction.channel

    try:
        msg_u = await client.wait_for("message", check=check_u, timeout=120)
    except asyncio.TimeoutError:
        user_states.pop(user_id, None)
        await interaction.followup.send("❌ Timed out waiting for username.", ephemeral=True)
        return

    if user_states.get(user_id) != "changing_username":
        await interaction.followup.send("❌ Update cancelled.", ephemeral=True)
        return

    new_username = msg_u.content.strip()
    user_temp_data[user_id] = {"username": new_username}
    user_states[user_id] = "changing_password"
    await interaction.followup.send("🔐 Send your new SPADA password now. (Stored in plain text — use a unique password)", ephemeral=True)

    def check_p(m: discord.Message):
        return m.author == interaction.user and m.channel == interaction.channel

    try:
        msg_p = await client.wait_for("message", check=check_p, timeout=120)
    except asyncio.TimeoutError:
        user_states.pop(user_id, None)
        user_temp_data.pop(user_id, None)
        await interaction.followup.send("❌ Timed out waiting for password.", ephemeral=True)
        return

    if user_states.get(user_id) != "changing_password":
        await interaction.followup.send("❌ Update cancelled.", ephemeral=True)
        return

    new_password = msg_p.content.strip()
    user_temp_data[user_id]["password"] = new_password

    if update_credentials(user_id, user_temp_data[user_id]):
        await interaction.followup.send("✅ Credentials updated.", ephemeral=True)
    else:
        await interaction.followup.send("❌ Couldn't update credentials. Try /setup if the record is missing.", ephemeral=True)

    user_states.pop(user_id, None)
    user_temp_data.pop(user_id, None)

@tree.command(name="mystatus", description="Show linked SPADA account info and pause state")
async def mystatus(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    username = find_username_by_id(user_id)
    schedule_path = find_schedule_path(user_id)

    if not username:
        await interaction.response.send_message("⚠️ No linked SPADA user found.", ephemeral=True)
        return

    pause_file = os.path.join(FLAG_DIR, f"pause_user_{username}.flag")
    if os.path.exists(pause_file):
        pause_state = "⏸️ Paused indefinitely"
    else:
        once_flags = [f for f in os.listdir(FLAG_DIR) if f.startswith(f"pause_once_{username}_")]
        if once_flags:
            paused_class = once_flags[0].replace(f"pause_once_{username}_", "").replace(".flag", "").replace("_", " ")
            pause_state = f"⏸️ Next class ({paused_class}) will be skipped"
        else:
            pause_state = "▶️ Active"

    msg = (
        f"👤 **SPADA User:** {username}\n"
        f"📂 **Schedule:** {schedule_path if schedule_path else 'not linked'}\n"
        f"⏱️ **Status:** {pause_state}"
    )
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="pause", description="Pause attendance indefinitely")
async def pause(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    username = find_username_by_id(user_id)
    if not username:
        await interaction.response.send_message("⚠️ No linked SPADA user found.", ephemeral=True)
        return
    flag_file = os.path.join(FLAG_DIR, f"pause_user_{username}.flag")
    if os.path.exists(flag_file):
        await interaction.response.send_message("⚠️ Already paused indefinitely. Use /resume to clear it.", ephemeral=True)
        return
    # block if a one-time pause exists
    once_flags = [f for f in os.listdir(FLAG_DIR) if f.startswith(f"pause_once_{username}_")]
    if once_flags:
        await interaction.response.send_message("⚠️ You have a one-time pause active. Use /resume first.", ephemeral=True)
        return
    with open(flag_file, "w") as f:
        f.write("paused")
    await interaction.response.send_message("⏸️ Attendance paused indefinitely. Use /resume to re-enable.", ephemeral=True)


@tree.command(name="resume", description="Resume attendance if paused")
async def resume(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    username = find_username_by_id(user_id)
    if not username:
        await interaction.response.send_message("⚠️ No linked SPADA user found.", ephemeral=True)
        return
    flag_file = os.path.join(FLAG_DIR, f"pause_user_{username}.flag")
    if os.path.exists(flag_file):
        try:
            os.remove(flag_file)
        except Exception:
            pass
    # clear any one-time flags too
    once_flags = [f for f in os.listdir(FLAG_DIR) if f.startswith(f"pause_once_{username}_")]
    for fpath in once_flags:
        try:
            os.remove(os.path.join(FLAG_DIR, fpath))
        except Exception:
            pass
    await interaction.response.send_message("▶️ Attendance resumed.", ephemeral=True)


@tree.command(name="pauseonce", description="Pause attendance for your next upcoming class only")
async def pauseonce(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    username = find_username_by_id(user_id)
    schedule_path = find_schedule_path(user_id)
    if not username or not schedule_path:
        await interaction.response.send_message("⚠️ No linked SPADA user or schedule.", ephemeral=True)
        return
    # block if indefinitely paused
    indefinite_flag = os.path.join(FLAG_DIR, f"pause_user_{username}.flag")
    if os.path.exists(indefinite_flag):
        await interaction.response.send_message("⚠️ You are paused indefinitely. Use /resume first.", ephemeral=True)
        return
    # block if a one-time pause already exists
    once_flags = [f for f in os.listdir(FLAG_DIR) if f.startswith(f"pause_once_{username}_")]
    if once_flags:
        await interaction.response.send_message("⚠️ You already have a one-time pause active. Use /resume first.", ephemeral=True)
        return
    next_class = get_next_class(schedule_path)
    if not next_class:
        await interaction.response.send_message("ℹ️ No upcoming class found to pause.", ephemeral=True)
        return
    flag_file = os.path.join(FLAG_DIR, f"pause_once_{username}_{next_class.replace(' ', '_')}.flag")
    with open(flag_file, "w") as f:
        f.write("skip next")
    await interaction.response.send_message(f"⏸️ Next class **{next_class}** will be skipped.", ephemeral=True)


@tree.command(name="delete", description="Remove your saved credentials (also deletes schedule & pause flags)")
async def delete(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if delete_credentials(user_id):
        await interaction.response.send_message("🗑️ Account, schedule, and flags deleted.", ephemeral=True)
    else:
        await interaction.response.send_message("⚠️ No account found to delete.", ephemeral=True)


@tree.command(name="schedule", description="Manage your class schedule (upload/view/delete)")
async def schedule(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if not find_username_by_id(user_id):
        await interaction.response.send_message("⚠️ Please run /setup first.", ephemeral=True)
        return
    await interaction.response.send_message("📌 Manage your schedule:", view=ScheduleMenu(user_id), ephemeral=True)


# ==========================
# Handle image uploads (for the Upload button flow)
# ==========================
@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    user_id = str(message.author.id)
    # Handle CSV upload
    if f"csv_{user_id}" in waiting_upload:
        if not message.attachments:
            return
        attachment = message.attachments[0]
        if not attachment.filename.lower().endswith(".csv"):
            await message.channel.send("⚠️ Please upload a CSV file.", delete_after=8)
            return
        waiting_upload.discard(f"csv_{user_id}")
        try:
            csv_bytes = await attachment.read()
            csv_text = csv_bytes.decode("utf-8")
            # Basic validation: must have header and at least one row
            lines = [l for l in csv_text.strip().splitlines() if l.strip()]
            if not lines or not lines[0].lower().startswith("coursename,day,time"):
                await message.channel.send("❌ CSV must start with header: CourseName,Day,Time", delete_after=10)
                return
            if len(lines) < 2:
                await message.channel.send("❌ CSV must have at least one schedule row.", delete_after=10)
                return
            schedule_path = find_schedule_path(user_id)
            if not schedule_path:
                await message.channel.send("❌ Could not locate your schedule file in .env.", delete_after=10)
                return
            with open(schedule_path, "w", encoding="utf-8") as f:
                f.write(csv_text if csv_text.endswith("\n") else csv_text + "\n")
            await message.channel.send(f"✅ Schedule CSV uploaded and saved to `{schedule_path}`.")
        except Exception as e:
            await message.channel.send(f"❌ Error processing CSV: `{e}`")
        return

    # Handle image upload (existing flow)
    if user_id not in waiting_upload:
        return
    if not message.attachments:
        return
    attachment = message.attachments[0]
    if not attachment.filename.lower().endswith((".jpg", ".jpeg", ".png")):
        await message.channel.send("⚠️ Please upload an image file (png/jpg).", delete_after=8)
        return

    waiting_upload.discard(user_id)
    await message.channel.send("⏳ Processing your schedule image with Gemini...", delete_after=6)
    image_bytes = await attachment.read()
    try:
        csv_text = parse_schedule_with_gemini(image_bytes)
        if not csv_text:
            await message.channel.send("❌ I couldn't read any schedule from that image. Try a clearer photo?", delete_after=8)
            return
        pending_csv[user_id] = csv_text
        buf = io.BytesIO(csv_text.encode("utf-8"))
        buf.name = "schedule_preview.csv"
        await message.channel.send(
            "📄 Here’s the schedule I extracted. Save it?",
            file=discord.File(buf, "schedule_preview.csv"),
            view=ConfirmMenu(user_id, csv_text)
        )
    except Exception as e:
        await message.channel.send(f"❌ Error parsing schedule: `{e}`")


# ==========================
# Boot & sync
# ==========================
@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user}")


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
