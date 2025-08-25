import os
import io
import asyncio
from datetime import datetime
from dotenv import load_dotenv
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

FLAG_DIR = "flags"
SCHEDULE_DIR = "schedules"
os.makedirs(FLAG_DIR, exist_ok=True)
os.makedirs(SCHEDULE_DIR, exist_ok=True)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

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
gemini_model = genai.GenerativeModel("gemini-2.0-flash")

def parse_schedule_with_gemini(image_bytes: bytes) -> str:
    image_data = {"mime_type": "image/jpeg", "data": image_bytes}
    prompt = (
        "Extract the class schedule from this image and return only CSV rows. "
        "Columns must be in this exact order: CourseName,Day,Time. "
        "Example:\n"
        "CourseName,Day,Time"
        "Matematika,Senin,07:00 - 09:00\n"
        "Fisika,Rabu,10:00 - 12:00\n"
        "Always add column name"
        "Do not forget the space before and after hyphen for the time"
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
            and user_id in lines[i+3]
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
        if schedule_path and os.path.exists(schedule_path):
            try:
                os.remove(schedule_path)
            except Exception:
                pass
        if username:
            for f in os.listdir(FLAG_DIR):
                if f.startswith(f"pause_user_{username}") or f.startswith(f"pause_once_{username}_"):
                    try:
                        os.remove(os.path.join(FLAG_DIR, f))
                    except Exception:
                        pass
    return found



#dm helper

async def dm_then_hint(interaction: discord.Interaction, *, content=None, file=None, view=None):
    # show the server-side hint (ephemeral)
    if not interaction.response.is_done():
        await interaction.response.send_message("i DMed you, continue there", ephemeral=True)
    else:
        await interaction.followup.send("i DMed you, continue there", ephemeral=True)
    # DM the actual payload
    await interaction.user.send(content=content, file=file, view=view)

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
        except Exception:
            continue
        if start_time > now:
            if closest_start is None or start_time < closest_start:
                closest_start, closest_class = start_time, course
    return closest_class

# ==========================
# Utility: DM-only ack
# ==========================
async def dm_only_ack(interaction: discord.Interaction, *, content=None, file=None, view=None):
    # always DM the user
    if content or file or view:
        await interaction.user.send(content=content, file=file, view=view)
    # silently acknowledge the slash command in the guild
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

# ==========================
# Discord UI (Upload/View/Delete + Confirm Save/Cancel) — now in DM
# ==========================
class ScheduleMenu(discord.ui.View):
    def __init__(self, user_id: str):
        super().__init__(timeout=180)
        self.user_id = user_id

    @discord.ui.button(label="🖼 Upload Schedule Image", style=discord.ButtonStyle.primary)
    async def upload(self, interaction: discord.Interaction, button: discord.ui.Button):
        waiting_upload.add(self.user_id)
        pending_csv.pop(self.user_id, None)
        await interaction.response.send_message("Send your schedule image now (png/jpg).")

    @discord.ui.button(label="⬆️ Manual CSV Upload", style=discord.ButtonStyle.success)
    async def upload_csv(self, interaction: discord.Interaction, button: discord.ui.Button):
        waiting_upload.add(f"csv_{self.user_id}")
        pending_csv.pop(self.user_id, None)
        await interaction.response.send_message("Send your CSV schedule file now.")

    @discord.ui.button(label="📄 View Schedule", style=discord.ButtonStyle.secondary)
    async def view(self, interaction: discord.Interaction, button: discord.ui.Button):
        schedule_path = find_schedule_path(self.user_id)
        if not schedule_path or not os.path.exists(schedule_path) or os.path.getsize(schedule_path) == 0:
            await interaction.response.send_message("No schedule saved yet.")
            return
        await interaction.response.send_message("Your saved schedule:", file=discord.File(schedule_path))

    @discord.ui.button(label="🗑 Delete Schedule", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        schedule_path = find_schedule_path(self.user_id)
        if schedule_path and os.path.exists(schedule_path):
            try:
                os.remove(schedule_path)
            except Exception:
                pass
            await interaction.response.send_message("Schedule deleted.")
        else:
            await interaction.response.send_message("No schedule to delete.")

class ConfirmMenu(discord.ui.View):
    def __init__(self, user_id: str, csv_text: str):
        super().__init__(timeout=240)
        self.user_id = user_id
        self.csv_text = csv_text

    @discord.ui.button(label="✅ Save", style=discord.ButtonStyle.success)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        schedule_path = find_schedule_path(self.user_id)
        if not schedule_path:
            await interaction.response.send_message("Could not locate your schedule file in .env.")
            return
        try:
            with open(schedule_path, "w", encoding="utf-8") as f:
                f.write(self.csv_text + ("" if not self.csv_text.endswith("") else ""))
            pending_csv.pop(self.user_id, None)
            await interaction.response.send_message(f"Schedule saved to `{schedule_path}`")
        except Exception as e:
            await interaction.response.send_message(f"Failed to save: `{e}`")

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        pending_csv.pop(self.user_id, None)
        waiting_upload.discard(self.user_id)
        await interaction.response.send_message("Schedule upload cancelled.")

# ==========================
# Slash commands (DM-only behavior)
# ==========================
@tree.command(name="help", description="Show a help message with available commands")
async def help_command(interaction: discord.Interaction):
    await dm_then_hint(interaction, content=(
        "Here’s what I can do for you:\n"
        "• **/help** – show this help message\n"
        "• **/setup** – link your SPADA account\n"
        "• **/mystatus** – show your SPADA user, schedule, and pause status\n"
        "• **/pause** – pause attendance indefinitely\n"
        "• **/resume** – resume attendance if paused\n"
        "• **/pauseonce** – skip attendance for your next class\n"
        "• **/delete** – remove your saved credentials\n"
        "• **/schedule** – upload your class schedule\n"
    ))


# 4) For EACH of your other commands, send the hint then DM.
#    Replace their current interaction.response.send_message(...) bodies
#    with dm_then_hint(...) + the actual DM content.
#    Below are ready-to-paste versions:

@tree.command(name="cancel", description="Cancel the current setup or pending upload")
async def cancel(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    user_states.pop(user_id, None)
    user_temp_data.pop(user_id, None)
    waiting_upload.discard(user_id)
    pending_csv.pop(user_id, None)
    await dm_then_hint(interaction, content="Cancelled.")


@tree.command(name="setup", description="Link your SPADA account (guided)")
async def setup(interaction: discord.Interaction):
    user = interaction.user
    user_id = str(user.id)
    if find_username_by_id(user_id):
        await dm_then_hint(interaction, content="You already have an account linked. Use /delete first.")
        return

    # show server hint, then continue entirely in DM
    await dm_then_hint(interaction, content="Let’s set you up. I’ll ask in DMs.")
    await user.create_dm()
    dm = user.dm_channel

    user_states[user_id] = "awaiting_username"
    await user.send("What is your SPADA username?")

    def check_u(m: discord.Message):
        return m.author == user and m.channel == dm

    try:
        msg_u = await client.wait_for("message", check=check_u, timeout=120)
    except asyncio.TimeoutError:
        user_states.pop(user_id, None)
        await user.send("Timed out waiting for username.")
        return

    if user_states.get(user_id) != "awaiting_username":
        await user.send("Setup cancelled.")
        return

    username = msg_u.content.strip()
    user_temp_data[user_id] = {"username": username}
    user_states[user_id] = "awaiting_password"
    await user.send("What is your SPADA password? ⚠️ Stored in plain text. Use a unique password.")

    def check_p(m: discord.Message):
        return m.author == user and m.channel == dm

    try:
        msg_p = await client.wait_for("message", check=check_p, timeout=120)
    except asyncio.TimeoutError:
        user_states.pop(user_id, None)
        user_temp_data.pop(user_id, None)
        await user.send("Timed out waiting for password.")
        return

    if user_states.get(user_id) != "awaiting_password":
        await user.send("Setup cancelled.")
        return

    password = msg_p.content.strip()
    user_temp_data[user_id]["password"] = password

    save_to_env(user_id, user_temp_data[user_id])
    user_states.pop(user_id, None)
    user_temp_data.pop(user_id, None)

    await user.send("Credentials saved!")
    await user.send("Don’t forget to upload your schedule with /schedule → Upload Schedule.")


@tree.command(name="mystatus", description="Show linked SPADA account info and pause state")
async def mystatus(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    username = find_username_by_id(user_id)
    schedule_path = find_schedule_path(user_id)
    if not username:
        await dm_then_hint(interaction, content="No linked SPADA user found.")
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
    await dm_then_hint(interaction, content=msg)


@tree.command(name="pause", description="Pause attendance indefinitely")
async def pause(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    username = find_username_by_id(user_id)
    if not username:
        await dm_then_hint(interaction, content="No linked SPADA user found.")
        return
    flag_file = os.path.join(FLAG_DIR, f"pause_user_{username}.flag")
    if os.path.exists(flag_file):
        await dm_then_hint(interaction, content="Already paused indefinitely. Use /resume to clear it.")
        return
    once_flags = [f for f in os.listdir(FLAG_DIR) if f.startswith(f"pause_once_{username}_")]
    if once_flags:
        await dm_then_hint(interaction, content="You have a one-time pause active. Use /resume first.")
        return
    with open(flag_file, "w") as f:
        f.write("paused")
    await dm_then_hint(interaction, content="Attendance paused indefinitely. Use /resume to re-enable.")


@tree.command(name="resume", description="Resume attendance if paused")
async def resume(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    username = find_username_by_id(user_id)
    if not username:
        await dm_then_hint(interaction, content="No linked SPADA user found.")
        return
    flag_file = os.path.join(FLAG_DIR, f"pause_user_{username}.flag")
    if os.path.exists(flag_file):
        try:
            os.remove(flag_file)
        except Exception:
            pass
    once_flags = [f for f in os.listdir(FLAG_DIR) if f.startswith(f"pause_once_{username}_")]
    for fpath in once_flags:
        try:
            os.remove(os.path.join(FLAG_DIR, fpath))
        except Exception:
            pass
    await dm_then_hint(interaction, content="Attendance resumed.")


@tree.command(name="pauseonce", description="Pause attendance for your next upcoming class only")
async def pauseonce(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    username = find_username_by_id(user_id)
    schedule_path = find_schedule_path(user_id)
    if not username or not schedule_path:
        await dm_then_hint(interaction, content="No linked SPADA user or schedule.")
        return
    indefinite_flag = os.path.join(FLAG_DIR, f"pause_user_{username}.flag")
    if os.path.exists(indefinite_flag):
        await dm_then_hint(interaction, content="You are paused indefinitely. Use /resume first.")
        return
    once_flags = [f for f in os.listdir(FLAG_DIR) if f.startswith(f"pause_once_{username}_")]
    if once_flags:
        await dm_then_hint(interaction, content="You already have a one-time pause active. Use /resume first.")
        return
    next_class = get_next_class(schedule_path)
    if not next_class:
        await dm_then_hint(interaction, content="No upcoming class found to pause.")
        return
    flag_file = os.path.join(FLAG_DIR, f"pause_once_{username}_{next_class.replace(' ', '_')}.flag")
    with open(flag_file, "w") as f:
        f.write("skip next")
    await dm_then_hint(interaction, content=f"Next class **{next_class}** will be skipped.")


@tree.command(name="delete", description="Remove your saved credentials (also deletes schedule & pause flags)")
async def delete(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if delete_credentials(user_id):
        await dm_then_hint(interaction, content="Account, schedule, and flags deleted.")
    else:
        await dm_then_hint(interaction, content="No account found to delete.")


@tree.command(name="schedule", description="Manage your class schedule (upload/view/delete)")
async def schedule(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if not find_username_by_id(user_id):
        await dm_then_hint(interaction, content="Please run /setup first.")
        return
    # send the menu in DM; show server hint
    view = ScheduleMenu(user_id)
    await dm_then_hint(interaction, content="Manage your schedule:", view=view)


# ==========================
# Handle uploads/messages — DM only
# ==========================
@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    user_id = str(message.author.id)

    # If message is in a guild channel, nudge to DMs without replying there.
    if isinstance(message.channel, discord.TextChannel):
        # quietly DM a pointer if they sent an attachment in guild while we're waiting
        if user_id in waiting_upload or f"csv_{user_id}" in waiting_upload:
            try:
                await message.author.send("Send that here in DMs, darling. I only handle uploads privately.")
            except discord.Forbidden:
                pass
        return

    # From here on, we are in DMs:

    # CSV upload
    if f"csv_{user_id}" in waiting_upload:
        if not message.attachments:
            return
        attachment = message.attachments[0]
        if not attachment.filename.lower().endswith(".csv"):
            await message.author.send("Please upload a CSV file.")
            return
        waiting_upload.discard(f"csv_{user_id}")
        try:
            csv_bytes = await attachment.read()
            csv_text = csv_bytes.decode("utf-8")
            lines = [l for l in csv_text.strip().splitlines() if l.strip()]
            if not lines or not lines[0].lower().startswith("coursename,day,time"):
                await message.author.send("CSV must start with header: CourseName,Day,Time")
                return
            if len(lines) < 2:
                await message.author.send("CSV must have at least one schedule row.")
                return
            schedule_path = find_schedule_path(user_id)
            if not schedule_path:
                await message.author.send("Could not locate your schedule file in .env.")
                return
            with open(schedule_path, "w", encoding="utf-8") as f:
                f.write(csv_text if csv_text.endswith("\n") else csv_text + "\n")
            await message.author.send(f"Schedule CSV uploaded and saved to `{schedule_path}`.")
        except Exception as e:
            await message.author.send(f"Error processing CSV: `{e}`")
        return

    # Image upload
    if user_id not in waiting_upload:
        return
    if not message.attachments:
        return
    attachment = message.attachments[0]
    if not attachment.filename.lower().endswith((".jpg", ".jpeg", ".png")):
        await message.author.send("Please upload an image file (png/jpg).")
        return

    waiting_upload.discard(user_id)
    await message.author.send("Processing your schedule image with Gemini...")
    image_bytes = await attachment.read()
    try:
        csv_text = parse_schedule_with_gemini(image_bytes)
        if not csv_text:
            await message.author.send("I couldn't read any schedule from that image. Try a clearer photo?")
            return
        pending_csv[user_id] = csv_text
        buf = io.BytesIO(csv_text.encode("utf-8"))
        buf.name = "schedule_preview.csv"
        await message.author.send(
            "Here’s the schedule I extracted. Save it?",
            file=discord.File(buf, "schedule_preview.csv"),
            view=ConfirmMenu(user_id, csv_text)
        )
    except Exception as e:
        await message.author.send(f"Error parsing schedule: `{e}`")

# ==========================
# Boot & sync
# ==========================
@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user}")

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
