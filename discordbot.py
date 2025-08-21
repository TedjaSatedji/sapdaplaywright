import os
import io
from datetime import datetime
from dotenv import load_dotenv
import discord
from discord import app_commands
import google.generativeai as genai

# ==========================
# Setup
# ==========================
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ENV_FILE = ".env"

FLAG_DIR = "flags"
SCHEDULE_DIR = "schedules"
os.makedirs(FLAG_DIR, exist_ok=True)
os.makedirs(SCHEDULE_DIR, exist_ok=True)

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ==========================
# Gemini model
# ==========================
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-2.0-flash")

def parse_schedule_with_gemini(image_bytes: bytes) -> str:
    image_data = {"mime_type": "image/jpeg", "data": image_bytes}
    prompt = (
        "Extract the class schedule from this image and return only CSV rows. "
        "Columns must be in this exact order: CourseName,Day,Time. "
        "Example:\n"
        "CourseName,Day,Time\n"
        "Matematika,Senin,07:00 - 09:00\n"
        "Fisika,Rabu,10:00 - 12:00\n"
        "Always add column name\n"
        "Do not forget the space before and after hyphen for the time\n"
        "Do not include class, explanations, or extra text."
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
            if line.startswith("DISCORD_USER_ID_") and user_id in line:
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
    new_lines, found, i = [], False, 0
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
            and user_id in lines[i+3]
        ):
            found = True
            # Extract username and schedule_path for deletion
            username = lines[i+1].strip().split("=", 1)[1]
            schedule_path = lines[i+4].strip().split("=", 1)[1]
            i += 5
        else:
            new_lines.append(lines[i])
            i += 1
    if found:
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        # Delete schedule file
        if schedule_path and os.path.exists(schedule_path):
            try:
                os.remove(schedule_path)
            except Exception:
                pass
        # Delete all flag files for this user
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
    if not os.path.exists(schedule_path) or os.path.getsize(schedule_path) == 0:
        return None
    now = datetime.now()
    closest_class, closest_start = None, None
    with open(schedule_path, "r", encoding="utf-8") as f:
        lines = f.readlines()[1:]
    for line in lines:
        parts = line.strip().split(",")
        if len(parts) < 3:
            continue
        course, day, time_str = parts
        try:
            start_str, end_str = time_str.split(" - ")
            start_time = datetime.strptime(start_str, "%H:%M").replace(
                year=now.year, month=now.month, day=now.day
            )
        except:
            continue
        if start_time > now:
            if closest_start is None or start_time < closest_start:
                closest_start, closest_class = start_time, course
    return closest_class

# ==========================
# Slash Commands
# ==========================
@tree.command(name="help", description="Show a help message with available commands")
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        "👋 Hello! Here’s what I can do for you:\n"
        "• **/help** – show this help message\n"
        "• **/setup** – link your SPADA account\n"
        "• **/mystatus** – show your SPADA user, schedule, and pause status\n"
        "• **/pause** – pause attendance indefinitely\n"
        "• **/resume** – resume attendance if paused\n"
        "• **/pauseonce** – skip attendance for your next class\n"
        "• **/delete** – remove your saved credentials\n"
        "• **/schedule** – upload your class schedule\n",
        ephemeral=True
    )

@tree.command(name="setup", description="Link your SPADA account")
async def setup(interaction: discord.Interaction, username: str, password: str):
    user_id = str(interaction.user.id)
    if find_username_by_id(user_id):
        await interaction.response.send_message("⚠️ You already have an account linked. Use /delete to reset.", ephemeral=True)
        return
    save_to_env(user_id, {"username": username, "password": password})
    await interaction.response.send_message("✅ Account linked! Now upload your schedule with /schedule.", ephemeral=True)

@tree.command(name="delete", description="Delete your linked account")
async def delete(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if delete_credentials(user_id):
        await interaction.response.send_message("🗑️ Account, schedule, and flags deleted.", ephemeral=True)
    else:
        await interaction.response.send_message("⚠️ No account found to delete.", ephemeral=True)

@tree.command(name="schedule", description="Upload your schedule image")
async def schedule(interaction: discord.Interaction, attachment: discord.Attachment):
    user_id = str(interaction.user.id)
    schedule_path = find_schedule_path(user_id)
    if not schedule_path:
        await interaction.response.send_message("⚠️ Please link your account first with /setup.", ephemeral=True)
        return
    try:
        img_bytes = await attachment.read()
        csv_data = parse_schedule_with_gemini(img_bytes)
        with open(schedule_path, "w", encoding="utf-8") as f:
            f.write(csv_data + "\n")
        await interaction.response.send_message("✅ Schedule saved!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to parse schedule: {e}", ephemeral=True)

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
    # Prevent pausing indefinitely if already paused indefinitely
    if os.path.exists(flag_file):
        await interaction.response.send_message("⚠️ You are already paused indefinitely. Use /resume to clear it before pausing again.", ephemeral=True)
        return
    # Prevent pausing indefinitely if a pause_once flag exists
    once_flags = [f for f in os.listdir(FLAG_DIR) if f.startswith(f"pause_once_{username}_")]
    if once_flags:
        await interaction.response.send_message("⚠️ You have a one-time pause active. Use /resume to clear it before pausing indefinitely.", ephemeral=True)
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
    # Remove indefinite pause flag
    if os.path.exists(flag_file):
        os.remove(flag_file)
    # Remove any pause_once flags for this user
    once_flags = [f for f in os.listdir(FLAG_DIR) if f.startswith(f"pause_once_{username}_")]
    for f in once_flags:
        try:
            os.remove(os.path.join(FLAG_DIR, f))
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
    # Prevent pausing once if already paused indefinitely
    indefinite_flag = os.path.join(FLAG_DIR, f"pause_user_{username}.flag")
    if os.path.exists(indefinite_flag):
        await interaction.response.send_message("⚠️ You are already paused indefinitely. Use /resume to clear it before pausing once.", ephemeral=True)
        return
    # Prevent pausing once if a pause_once flag already exists
    once_flags = [f for f in os.listdir(FLAG_DIR) if f.startswith(f"pause_once_{username}_")]
    if once_flags:
        await interaction.response.send_message("⚠️ You already have a one-time pause active. Use /resume to clear it before pausing once again.", ephemeral=True)
        return
    next_class = get_next_class(schedule_path)
    if not next_class:
        await interaction.response.send_message("ℹ️ No upcoming class found to pause.", ephemeral=True)
        return
    flag_file = os.path.join(FLAG_DIR, f"pause_once_{username}_{next_class.replace(' ','_')}.flag")
    with open(flag_file, "w") as f:
        f.write("skip next")
    await interaction.response.send_message(f"⏸️ Next class **{next_class}** will be skipped.", ephemeral=True)

# ==========================
# Boot
# ==========================
@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user}")

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
