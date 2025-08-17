import discord
from discord.ext import commands
import os
import io
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ENV_FILE = ".env"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# --- Intents ---
intents = discord.Intents.default()
intents.message_content = True
client = commands.Bot(command_prefix="!", intents=intents)

# --- State tracking ---
user_states = {}
user_temp_data = {}
pending_csv = {}

# --- Gemini setup ---
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-2.0-flash")

async def parse_schedule_with_gemini(image_bytes: bytes) -> str:
    """Use Gemini to parse schedule image into CSV text"""
    image_data = {
        "mime_type": "image/png",
        "data": image_bytes
    }
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
    response = gemini_model.generate_content([prompt, image_data])
    return response.text.strip()

def find_schedule_path(user_id: str) -> str | None:
    """Locate the user's schedule file path in .env"""
    if not os.path.exists(ENV_FILE):
        return None
    with open(ENV_FILE, "r") as f:
        lines = f.readlines()

    user_index = None
    for line in lines:
        if line.startswith("DISCORD_USER_ID_") and user_id in line:
            user_index = line.split("_")[-1].split("=")[0]
            break

    if not user_index:
        return None

    for line in lines:
        if line.startswith(f"SCHEDULE_FILE_{user_index}="):
            return line.strip().split("=", 1)[1]
    return None

# --- Credential helpers ---
def is_user_id_exist(user_id):
    if not os.path.exists(ENV_FILE):
        return False
    with open(ENV_FILE, "r") as f:
        return any("DISCORD_USER_ID_" in line and user_id in line for line in f)

def get_next_index():
    if not os.path.exists(ENV_FILE):
        return 1
    with open(ENV_FILE, "r") as f:
        indices = [int(line.split("_")[-1].split("=")[0]) for line in f if line.startswith("SPADA_USERNAME_")]
        return max(indices) + 1 if indices else 1

def save_to_env(user_id, creds):
    index = get_next_index()
    schedule_dir = "schedules"
    os.makedirs(schedule_dir, exist_ok=True)
    schedule_path = f"{schedule_dir}/schedule_{index}.csv"
    if not os.path.exists(schedule_path):
        open(schedule_path, "w").close()
    with open(ENV_FILE, "a") as f:
        f.write(f"#--- {creds['username']} ---\n")
        f.write(f"SPADA_USERNAME_{index}={creds['username']}\n")
        f.write(f"SPADA_PASSWORD_{index}={creds['password']}\n")
        f.write(f"DISCORD_USER_ID_{index}={user_id}\n")
        f.write(f"SCHEDULE_FILE_{index}={schedule_path}\n")

# --- Events ---
@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

@client.event
async def on_message(message: discord.Message):
    user_id = str(message.author.id)

    if message.author == client.user:
        return

    # --- Setup flow (DM) ---
    if isinstance(message.channel, discord.DMChannel) and user_id in user_states:
        text = message.content.strip()
        state = user_states[user_id]
        if state == "awaiting_username":
            user_temp_data[user_id] = {"username": text}
            user_states[user_id] = "awaiting_password"
            await message.channel.send(
                "🔐 What is your SPADA password?\n\n⚠️ Stored in plain text. Use a unique password."
            )
        elif state == "awaiting_password":
            user_temp_data[user_id]["password"] = text
            save_to_env(user_id, user_temp_data[user_id])
            await message.channel.send("✅ Credentials saved successfully!")
            user_states.pop(user_id)
            user_temp_data.pop(user_id)

    # --- Schedule upload flow (DM) ---
    elif isinstance(message.channel, discord.DMChannel) and message.attachments:
        if not is_user_id_exist(user_id):
            await message.channel.send("⚠️ Please run /setup first before sending your schedule.")
            return

        attachment = message.attachments[0]
        if not attachment.filename.lower().endswith((".png", ".jpg", ".jpeg")):
            await message.channel.send("⚠️ Please upload an image file (png/jpg).")
            return

        image_bytes = await attachment.read()
        await message.channel.send("⏳ Processing your schedule image with Gemini...")

        try:
            csv_text = await parse_schedule_with_gemini(image_bytes)
            pending_csv[user_id] = csv_text

            # Send CSV preview back
            csv_file = io.BytesIO(csv_text.encode("utf-8"))
            csv_file.name = "schedule_preview.csv"
            await message.channel.send(
                "📄 Here’s the schedule I extracted.\nReply with `yes` to save, or `no` to cancel.",
                file=discord.File(csv_file)
            )
        except Exception as e:
            await message.channel.send(f"❌ Error parsing schedule: {e}")

    # --- Confirmation (DM) ---
    elif isinstance(message.channel, discord.DMChannel) and user_id in pending_csv:
        if message.content.lower() == "yes":
            schedule_path = find_schedule_path(user_id)
            if not schedule_path:
                await message.channel.send("❌ Could not locate your schedule file in .env.")
                pending_csv.pop(user_id, None)
                return

            with open(schedule_path, "w", encoding="utf-8") as f:
                f.write(pending_csv[user_id])

            await message.channel.send(f"✅ Schedule saved to `{schedule_path}`")
            pending_csv.pop(user_id, None)

        elif message.content.lower() == "no":
            await message.channel.send("❌ Schedule upload cancelled. Please resend if needed.")
            pending_csv.pop(user_id, None)

    await client.process_commands(message)

# --- Slash Commands ---
@client.tree.command(name="start", description="Show help message with all available commands.")
async def start_command(interaction: discord.Interaction):
    commands_text = (
        "🤖 Available commands:\n"
        "/start - Show this help message\n"
        "/me - Check your saved SPADA data\n"
        "/setup - Start SPADA setup\n"
        "/delete - Delete your saved credentials\n"
        "/cancel - Cancel current setup\n"
        "📷 DM me an image of your schedule to auto-generate CSV"
    )
    await interaction.response.send_message(commands_text, ephemeral=True)

@client.tree.command(name="me", description="Check for existing SPADA credentials.")
async def me_command(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if not os.path.exists(ENV_FILE):
        await interaction.response.send_message("ℹ️ No credentials found for your account.", ephemeral=True)
        return
    with open(ENV_FILE, "r") as f:
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

@client.tree.command(name="setup", description="Save your SPADA credentials.")
async def setup_command(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if is_user_id_exist(user_id):
        await interaction.response.send_message("⚠️ You already have credentials saved. Use /delete first.", ephemeral=True)
        return
    user_states[user_id] = "awaiting_username"
    await interaction.user.send("🟢 What is your SPADA username?")
    await interaction.response.send_message("✅ Check your DM to continue setup.", ephemeral=True)

@client.tree.command(name="cancel", description="Cancel setup.")
async def cancel_command(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    user_states.pop(user_id, None)
    user_temp_data.pop(user_id, None)
    await interaction.response.send_message("❌ Setup cancelled.", ephemeral=True)

@client.tree.command(name="delete", description="Delete saved credentials.")
async def delete_command(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if not os.path.exists(ENV_FILE):
        await interaction.response.send_message("⚠️ No credentials found.", ephemeral=True)
        return
    with open(ENV_FILE, "r") as f:
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
        with open(ENV_FILE, "w") as f:
            f.writelines(new_lines)
        await interaction.response.send_message("🗑️ Credentials deleted.", ephemeral=True)
    else:
        await interaction.response.send_message("⚠️ No credentials found.", ephemeral=True)

client.run(DISCORD_TOKEN)
