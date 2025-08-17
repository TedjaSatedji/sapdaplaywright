import os
import io
import asyncio
from dotenv import load_dotenv
import discord
from discord import app_commands
import google.generativeai as genai

# ==========================
# Boot
# ==========================
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ENV_FILE = ".env"

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ==========================
# State (mirror Telegram's)
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
    """
    Uses Gemini to parse a schedule image and return CSV rows (no header).
    Order: CourseName,Day,Time
    """
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
        "Do not include, explanations, or extra text."
    )
    resp = gemini_model.generate_content([prompt, image_data])
    return (resp.text or "").strip()

# ==========================
# Helpers (mirror Telegram)
# ==========================
def is_user_exist(user_id: str) -> bool:
    if not os.path.exists(ENV_FILE):
        return False
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("DISCORD_USER_ID_") and user_id in line:
                return True
    return False

def find_user_index_by_id(user_id: str):
    if not os.path.exists(ENV_FILE):
        return None
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("DISCORD_USER_ID_") and user_id in line:
                return line.split("_")[-1].split("=")[0].strip()
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
    schedule_dir = "schedules"
    os.makedirs(schedule_dir, exist_ok=True)
    schedule_path = f"{schedule_dir}/schedule_{idx}.csv"
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
            i += 5
        else:
            new_lines.append(lines[i])
            i += 1
    if found:
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    return found

# ==========================
# Discord UI (mirrors Telegram inline buttons)
# ==========================
class ScheduleMenu(discord.ui.View):
    def __init__(self, user_id: str):
        super().__init__(timeout=120)
        self.user_id = user_id

    @discord.ui.button(label="🖼 Upload Schedule", style=discord.ButtonStyle.primary)
    async def upload(self, interaction: discord.Interaction, button: discord.ui.Button):
        waiting_upload.add(self.user_id)
        # clear any prior pending CSV
        pending_csv.pop(self.user_id, None)
        await interaction.response.send_message("🖼 Please send your schedule image now (in this channel or DM me).", ephemeral=True)

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
            os.remove(schedule_path)
            # recreate empty file so path stays valid
            open(schedule_path, "w", encoding="utf-8").close()
            await interaction.response.send_message("🗑 Schedule deleted.", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ No schedule to delete.", ephemeral=True)

class ConfirmMenu(discord.ui.View):
    def __init__(self, user_id: str, csv_text: str):
        super().__init__(timeout=60)
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
                f.write(self.csv_text)
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
# Slash commands (same as Telegram)
# ==========================
@tree.command(name="start", description="Show help / available commands")
async def start(interaction: discord.Interaction):
    help_text = (
        "hi hi~ 💫\n\n"
        "Here’s what I can do for you:\n"
        "• /setup - link your SPADA account\n"
        "• /me - show which SPADA user is linked\n"
        "• /delete - remove your saved credentials\n"
        "• /schedule - manage your class schedule (upload/view/delete)\n"
        "• /cancel - cancel the current setup or pending upload\n\n"
        "After /setup I'll remind you to upload your schedule with /schedule → Upload Schedule."
    )
    await interaction.response.send_message(help_text, ephemeral=True)

@tree.command(name="cancel", description="Cancel the current setup or pending upload")
async def cancel(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    had_any = False
    if user_states.pop(user_id, None) is not None:
        had_any = True
    if user_temp_data.pop(user_id, None) is not None:
        had_any = True
    if waiting_upload.discard(user_id) is not None:
        had_any = True
    if pending_csv.pop(user_id, None) is not None:
        had_any = True

    # Always respond similarly to Telegram
    await interaction.response.send_message("❌ Setup cancelled.", ephemeral=True)

@tree.command(name="setup", description="Link your SPADA account")
async def setup(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if is_user_exist(user_id):
        await interaction.response.send_message("⚠️ You already have credentials saved. Use /delete first.", ephemeral=True)
        return

    # mark state so /cancel can clear it
    user_states[user_id] = "awaiting_username"
    await interaction.response.send_message("🟢 What is your SPADA username?", ephemeral=True)

    def check_username(m: discord.Message):
        return m.author == interaction.user and m.channel == interaction.channel

    try:
        msg = await client.wait_for("message", check=check_username, timeout=120)
    except asyncio.TimeoutError:
        user_states.pop(user_id, None)
        await interaction.followup.send("❌ Timed out waiting for username.", ephemeral=True)
        return

    # if user canceled during wait, abort
    if user_states.get(user_id) != "awaiting_username":
        await interaction.followup.send("❌ Setup cancelled.", ephemeral=True)
        return

    username = msg.content.strip()
    user_temp_data[user_id] = {"username": username}
    user_states[user_id] = "awaiting_password"
    await interaction.followup.send("🔐 What is your SPADA password?\n\n⚠️ Stored in plain text. Use a unique password.", ephemeral=True)

    def check_password(m: discord.Message):
        return m.author == interaction.user and m.channel == interaction.channel

    try:
        msg = await client.wait_for("message", check=check_password, timeout=120)
    except asyncio.TimeoutError:
        user_states.pop(user_id, None)
        user_temp_data.pop(user_id, None)
        await interaction.followup.send("❌ Timed out waiting for password.", ephemeral=True)
        return

    if user_states.get(user_id) != "awaiting_password":
        await interaction.followup.send("❌ Setup cancelled.", ephemeral=True)
        return

    password = msg.content.strip()
    user_temp_data[user_id]["password"] = password

    # Save credentials to .env (same behavior as Telegram)
    save_to_env(user_id, user_temp_data[user_id])
    user_states.pop(user_id, None)
    user_temp_data.pop(user_id, None)

    await interaction.followup.send("✅ Credentials saved!", ephemeral=True)
    # gentle reminder to upload schedule
    await interaction.followup.send("💡 Don’t forget to upload your schedule with /schedule → Upload Schedule.", ephemeral=True)

@tree.command(name="me", description="Show which SPADA user is linked")
async def me(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if not os.path.exists(ENV_FILE):
        await interaction.response.send_message("ℹ️ No credentials found for your account.", ephemeral=True)
        return
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
    username = None
    for i in range(len(lines)):
        if lines[i].startswith("DISCORD_USER_ID_") and user_id in lines[i]:
            if i >= 2 and lines[i-2].startswith("SPADA_USERNAME_"):
                username = lines[i-2].split("=", 1)[1].strip()
            break
    if username:
        await interaction.response.send_message(f"👤 Your SPADA username: `{username}`", ephemeral=True)
    else:
        await interaction.response.send_message("ℹ️ No credentials found for your account.", ephemeral=True)

@tree.command(name="delete", description="Remove your saved credentials")
async def delete(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if delete_credentials(user_id):
        await interaction.response.send_message("🗑️ Your credentials have been deleted.", ephemeral=True)
    else:
        await interaction.response.send_message("⚠️ No credentials found for your account.", ephemeral=True)

@tree.command(name="schedule", description="Manage your class schedule (upload/view/delete)")
async def schedule(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if not is_user_exist(user_id):
        await interaction.response.send_message("⚠️ Please run /setup first.", ephemeral=True)
        return
    await interaction.response.send_message("📌 Manage your schedule:", view=ScheduleMenu(user_id), ephemeral=True)

# ==========================
# Handle image uploads (same flow as Telegram)
# ==========================
@client.event
async def on_message(message: discord.Message):
    # allow commands to be processed
    # await client.process_commands(message)

    # only process user uploads when they're in waiting_upload
    user_id = str(message.author.id)
    if user_id not in waiting_upload:
        return
    if not message.attachments:
        return

    attachment = message.attachments[0]
    if not attachment.filename.lower().endswith((".jpg", ".jpeg", ".png")):
        await message.channel.send("⚠️ Please upload an image file (png/jpg).", delete_after=8)
        return

    # consume the upload slot
    waiting_upload.discard(user_id)
    await message.channel.send("⏳ Processing your schedule image with Gemini...", delete_after=6)

    image_bytes = await attachment.read()
    try:
        csv_text = parse_schedule_with_gemini(image_bytes)
        if not csv_text:
            await message.channel.send("❌ I couldn't read any schedule from that image. Try a clearer photo?", delete_after=8)
            return

        pending_csv[user_id] = csv_text
        csv_file = io.BytesIO(csv_text.encode("utf-8"))
        csv_file.name = "schedule_preview.csv"

        await message.channel.send(
            "📄 Here’s the schedule I extracted. Save it?",
            file=discord.File(csv_file, "schedule_preview.csv"),
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
