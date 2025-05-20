import discord
from discord.ext import tasks, commands
import os
import json
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from discord.ui import Select, View, Modal, TextInput
from aiohttp import web


intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Constants
GUILD_ID = 1355091741501554859

SETTINGS_FILE = "settings.json"
HISTORY_FILE = "history.json"
ARCHIVE_FOLDER = "archive"
LOA_FILE = "loas.json"

DEFAULT_MESSAGE = "Are you available for turf at 8pm?"
DEFAULT_HOUR = 20
DEFAULT_MINUTE = 0

TURF_CHANNEL_ID = 1373930711542923296
LOG_CHANNEL_ID = 1373936464152236062
ADMIN_PANEL_CHANNEL_ID = 1373961230682951811
LOA_LIST_CHANNEL_ID = 1373956925506588812

RESPONSE_WINDOW_MINUTES = 60
MIN_RESPONSES_FOR_LEADERBOARD = 5

settings = {
    "message": DEFAULT_MESSAGE,
    "hour": DEFAULT_HOUR,
    "minute": DEFAULT_MINUTE,
    "admin_roles": [],
    "announcement": DEFAULT_MESSAGE,
    "turf_channel": TURF_CHANNEL_ID,
    "log_channel": LOG_CHANNEL_ID,
    "admin_panel_channel": ADMIN_PANEL_CHANNEL_ID,
    "loa_list_channel": LOA_LIST_CHANNEL_ID
}

responses = {}
summary_message_id = None
loa_message_id = None
last_turf_message_id = None

if not os.path.exists(ARCHIVE_FOLDER):
    os.makedirs(ARCHIVE_FOLDER)


def load_json(filename, default):
    if not os.path.exists(filename):
        return default
    with open(filename, 'r') as f:
        return json.load(f)


def save_json(filename, data):
    with open(filename, 'w') as f:
        json.dump(data, f, indent=2)


def load_all():
    global settings, responses
    loaded = load_json(SETTINGS_FILE, settings)
    settings.update(loaded)
    responses.clear()


def archive_today():
    today_str = date.today().isoformat()
    save_json(f"{ARCHIVE_FOLDER}/{today_str}.json", responses)


def is_on_loa(user_id: str, check_date: date):
    loas = load_json(LOA_FILE, {})
    user_loas = loas.get(user_id, [])
    for entry in user_loas:
        start = datetime.strptime(entry["start"], "%Y-%m-%d").date()
        end = datetime.strptime(entry["end"], "%Y-%m-%d").date()
        if start <= check_date <= end:
            return True
    return False


class TurfModal(Modal, title="Turf Availability"):
    availability = TextInput(label="Availability (Yes, No, or Yes but later)",
                             placeholder="Yes / No / Yes but later",
                             max_length=20)
    reason = TextInput(label="Reason or time if No / Yes later",
                       style=discord.TextStyle.paragraph,
                       required=False,
                       max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        avail = self.availability.value.lower().strip()
        reason = self.reason.value.strip()
        if avail == "yes but later" and reason:
            reason_text = f"Will join later: {reason}"
            avail = "yes_later"   # changed here to differentiate "yes but later"
        elif avail == "no":
            reason_text = reason or "No reason given"
        else:
            reason_text = ""
        await record_response(interaction.user, avail, reason_text)
        await update_summary()
        await interaction.response.send_message(
            "✅ Your response has been recorded!", ephemeral=True)


class LOAModal(Modal, title="Log Leave of Absence"):
    start = TextInput(label="Start Date (dd/mm/yyyy)",
                      placeholder="e.g. 20/05/2025")
    end = TextInput(label="End Date (dd/mm/yyyy)",
                    placeholder="e.g. 22/05/2025")
    reason = TextInput(label="Reason", style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            start_date = datetime.strptime(self.start.value.strip(),
                                           "%d/%m/%Y").date()
            end_date = datetime.strptime(self.end.value.strip(),
                                         "%d/%m/%Y").date()
            reason_text = self.reason.value.strip()
            user_id = str(interaction.user.id)
            loas = load_json(LOA_FILE, {})
            if user_id not in loas:
                loas[user_id] = []
            loas[user_id].append({
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "reason": reason_text
            })
            save_json(LOA_FILE, loas)

            for day in range((end_date - start_date).days + 1):
                current_date = start_date + timedelta(days=day)
                if current_date == date.today():
                    await record_response(interaction.user, "no", reason_text)
                    await update_summary()

            await update_loa_list()
            await interaction.response.send_message(
                f"✅ LOA recorded from {self.start.value} to {self.end.value}.",
                ephemeral=True)
        except:
            await interaction.response.send_message(
                "❌ Invalid date format. Use dd/mm/yyyy.", ephemeral=True)


class TimeModal(Modal, title="Set Turf Time"):
    hour = TextInput(label="Hour (0-23)", placeholder="e.g. 20")
    minute = TextInput(label="Minute (0-59)", placeholder="e.g. 30")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            h = int(self.hour.value.strip())
            m = int(self.minute.value.strip())
            if 0 <= h <= 23 and 0 <= m <= 59:
                settings["hour"] = h
                settings["minute"] = m
                save_json(SETTINGS_FILE, settings)
                await interaction.response.send_message(
                    f"✅ Turf time updated to {h:02d}:{m:02d}.", ephemeral=True)
            else:
                await interaction.response.send_message(
                    "❌ Invalid hour or minute.", ephemeral=True)
        except:
            await interaction.response.send_message("❌ Invalid input.",
                                                    ephemeral=True)


class MessageModal(Modal, title="Set Turf Announcement"):
    mention_role = TextInput(label="Role to mention (@everyone or role ID)",
                             placeholder="@everyone or RoleID",
                             required=True)
    msg = TextInput(label="Message",
                    placeholder="Supports **bold**, *italics*, etc.",
                    style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        mention_text = self.mention_role.value.strip()
        msg_text = self.msg.value.strip()
        if mention_text == "@everyone":
            settings["announcement"] = f"@everyone {msg_text}"
        else:
            try:
                role_id = int(mention_text.strip("<@&>"))
                settings["announcement"] = f"<@&{role_id}> {msg_text}"
            except:
                settings["announcement"] = msg_text
        save_json(SETTINGS_FILE, settings)
        await interaction.response.send_message(
            "✅ Announcement message updated.", ephemeral=True)


class AdminPanel(View):

    @discord.ui.button(label="Send Turf Test",
                       style=discord.ButtonStyle.primary)
    async def test(self, interaction: discord.Interaction,
                   button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ Not permitted.",
                                                    ephemeral=True)
            return
        await send_turf_question()
        await interaction.response.send_message("✅ Turf test sent.",
                                                ephemeral=True)

    @discord.ui.button(label="Force Summary",
                       style=discord.ButtonStyle.secondary)
    async def summary(self, interaction: discord.Interaction,
                      button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ Not permitted.",
                                                    ephemeral=True)
            return
        await update_loa_list()
        await update_summary(force=True)
        await interaction.response.send_message("✅ Summary updated.",
                                                ephemeral=True)

    @discord.ui.button(label="Set Time", style=discord.ButtonStyle.secondary)
    async def settime(self, interaction: discord.Interaction,
                      button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ Not permitted.",
                                                    ephemeral=True)
            return
        await interaction.response.send_modal(TimeModal())

    @discord.ui.button(label="Set Message",
                       style=discord.ButtonStyle.secondary)
    async def setmsg(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ Not permitted.",
                                                    ephemeral=True)
            return
        await interaction.response.send_modal(MessageModal())


class RemoveLOASelect(Select):

    def __init__(self, user_id: str):
        self.user_id = user_id
        loas = load_json(LOA_FILE, {}).get(user_id, [])
        options = []
        for i, entry in enumerate(loas):
            start = datetime.strptime(entry["start"],
                                      "%Y-%m-%d").date().strftime("%d/%m/%Y")
            end = datetime.strptime(entry["end"],
                                    "%Y-%m-%d").date().strftime("%d/%m/%Y")
            label = f"{start} to {end}"
            options.append(discord.SelectOption(label=label, value=str(i)))
        super().__init__(placeholder="Select LOA to remove",
                         options=options,
                         min_values=1,
                         max_values=1)

    async def callback(self, interaction: discord.Interaction):
        loas = load_json(LOA_FILE, {})
        user_loas = loas.get(self.user_id, [])
        idx = int(self.values[0])
        if 0 <= idx < len(user_loas):
            removed = user_loas.pop(idx)
            loas[self.user_id] = user_loas
            save_json(LOA_FILE, loas)
            await update_loa_list()
            await interaction.response.send_message(
                f"✅ Removed LOA from {removed['start']} to {removed['end']}.",
                ephemeral=True)
        else:
            await interaction.response.send_message("❌ Invalid selection.",
                                                    ephemeral=True)


class RemoveLOAView(View):

    def __init__(self, user_id: str):
        super().__init__()
        self.add_item(RemoveLOASelect(user_id))


# NEW: Add this class to show Add LOA and Remove LOA buttons on LOA list message
class LOAMessageView(View):

    def __init__(self):
        super().__init__()
        self.add_item(
            discord.ui.Button(label="Add LOA",
                              style=discord.ButtonStyle.primary,
                              custom_id="add_loa"))
        self.add_item(
            discord.ui.Button(label="Remove LOA",
                              style=discord.ButtonStyle.danger,
                              custom_id="remove_loa"))


async def clear_bot_messages(channel):

    def is_bot(m):
        return m.author == bot.user

    try:
        deleted = await channel.purge(limit=100, check=is_bot)
        print(f"Deleted {len(deleted)} bot messages in {channel.name}")
    except Exception as e:
        print(f"Error deleting messages: {e}")


async def update_loa_list():
    global loa_message_id
    channel = bot.get_channel(settings.get("loa_list_channel"))
    if not channel:
        return
    await clear_bot_messages(channel
                             )  # clear previous bot messages before posting

    loas = load_json(LOA_FILE, {})
    today = date.today()
    output = []
    for uid, records in loas.items():
        member = channel.guild.get_member(int(uid))
        if not member:
            continue
        name = member.display_name
        for entry in records:
            start = datetime.strptime(entry["start"], "%Y-%m-%d").date()
            end = datetime.strptime(entry["end"], "%Y-%m-%d").date()
            if end >= today:
                start_str = start.strftime("%d/%m/%Y")
                end_str = end.strftime("%d/%m/%Y")
                output.append(
                    f"📅 **{name}** — {start_str} to {end_str} – {entry['reason']}"
                )
    content = "**📋 Current and Upcoming LOAs:**\n" + (
        "\n".join(output) if output else "✅ No active LOAs.")
    # Send message with Add LOA and Remove LOA buttons always
    msg = await channel.send(content, view=LOAMessageView())
    loa_message_id = msg.id


async def update_summary(force=False):
    global summary_message_id
    log_channel = bot.get_channel(settings.get("log_channel"))
    if not log_channel:
        return

    # Delete old summary message if forcing update to avoid clutter
    if force and summary_message_id:
        try:
            old_msg = await log_channel.fetch_message(summary_message_id)
            await old_msg.delete()
        except:
            pass

    yes_list = [
        r["name"] for r in responses.values() if r["available"] == "yes"
    ]
    yes_later_list = [
        f"⏰ **{r['name']}** – {r['reason']}" for r in responses.values() if r["available"] == "yes_later"
    ]
    no_list = [
        f"❌ **{r['name']}** – {r['reason']}" for r in responses.values()
        if r["available"] == "no"
    ]
    today_uk = date.today().strftime("%d/%m/%Y")
    summary = f"📋 **Turf Availability Summary** ({today_uk})\n\n"
    summary += f"✅ **Yes ({len(yes_list)}):**\n" + (
        ", ".join(yes_list) if yes_list else "None") + "\n\n"
    summary += f"⏰ **Yes but later ({len(yes_later_list)}):**\n" + (
        "\n".join(yes_later_list) if yes_later_list else "None") + "\n\n"
    summary += f"❌ **No ({len(no_list)}):**\n" + ("\n".join(no_list)
                                                  if no_list else "None")

    # Delete old summary message (if not forced)
    if not force and summary_message_id:
        try:
            old_msg = await log_channel.fetch_message(summary_message_id)
            await old_msg.delete()
        except:
            pass

    msg = await log_channel.send(summary)
    summary_message_id = msg.id



async def send_turf_question():
    global last_turf_message_id
    turf_channel = bot.get_channel(settings.get("turf_channel"))
    if not turf_channel:
        return
    await clear_bot_messages(turf_channel
                             )  # clear previous turf messages before sending

    ping_text = "@everyone"
    msg = await turf_channel.send(
        f"{ping_text} {settings.get('announcement', DEFAULT_MESSAGE)}",
        view=discord.ui.View().add_item(
            discord.ui.Button(label='Respond',
                              style=discord.ButtonStyle.primary,
                              custom_id="respond_button")))
    last_turf_message_id = msg.id


async def send_admin_panel():
    channel = bot.get_channel(settings.get("admin_panel_channel"))
    if channel:
        await clear_bot_messages(channel)
        await channel.send("🛠 **Turf Admin Panel**", view=AdminPanel())


async def record_response(user, availability, reason):
    uid = str(user.id)
    today = date.today()
    if is_on_loa(uid, today):
        # Auto set no if user on LOA today
        reason = "On Leave of Absence"
        availability = "no"
    else:
        availability = availability.lower().strip()
        reason = reason.strip() if availability == "no" else ""

    # --- NEW: Automatically add 1-day LOA if user responds 'no' ---
    if availability == "no" and reason:
        loas = load_json(LOA_FILE, {})
        user_loas = loas.get(uid, [])
        # Check if user already has a LOA today to avoid duplicates
        already_has_today = any(
            datetime.strptime(entry["start"], "%Y-%m-%d").date() <= today <=
            datetime.strptime(entry["end"], "%Y-%m-%d").date()
            for entry in user_loas)
        if not already_has_today:
            user_loas.append({
                "start": today.isoformat(),
                "end": today.isoformat(),
                "reason": reason
            })
            loas[uid] = user_loas
            save_json(LOA_FILE, loas)
            await update_loa_list()
    # -------------------------------------------------------------

    timestamp = datetime.now(ZoneInfo("Europe/London")).strftime("%H:%M:%S")
    previous = responses.get(uid, {}).get("available")
    change_note = f" (changed at {timestamp})" if previous == "yes" and availability == "no" else ""

    responses[uid] = {
        "name": user.display_name,
        "available": availability,
        "reason": reason + change_note if reason and change_note else reason
    }

    history = load_json(HISTORY_FILE, {})
    today_str = today.isoformat()
    if uid not in history:
        history[uid] = []
    history[uid].append({
        "date": today_str,
        "available": availability,
        "reason": reason,
        "time": timestamp
    })
    save_json(HISTORY_FILE, history)


@bot.event
async def on_interaction(interaction):
    if interaction.type == discord.InteractionType.component:
        cid = interaction.data.get("custom_id")
        if cid == "respond_button":
            await interaction.response.send_modal(TurfModal())
        elif cid == "add_loa":
            await interaction.response.send_modal(LOAModal())
        elif cid == "remove_loa":
            await interaction.response.send_message(
                "Select an LOA to remove:",
                view=RemoveLOAView(str(interaction.user.id)),
                ephemeral=True)


@tasks.loop(minutes=1)
async def turf_check():
    now = datetime.now(ZoneInfo("Europe/London"))
    if now.hour == settings.get("hour",
                                DEFAULT_HOUR) and now.minute == settings.get(
                                    "minute", DEFAULT_MINUTE):
        responses.clear()
        await send_turf_question()
        await update_summary()


@tasks.loop(minutes=1)
async def clear_daily():
    now = datetime.now(ZoneInfo("Europe/London"))
    if now.hour == 0 and now.minute == 1:
        log_channel = bot.get_channel(settings.get("log_channel"))
        global summary_message_id
        if log_channel and summary_message_id:
            try:
                msg = await log_channel.fetch_message(summary_message_id)
                await msg.delete()
            except:
                pass
        archive_today()
        summary_message_id = None
        responses.clear()
        await update_loa_list()


@bot.tree.command(name="loa",
                  description="Log a leave of absence",
                  guild=discord.Object(id=GUILD_ID))
async def loa(interaction: discord.Interaction):
    await interaction.response.send_modal(LOAModal())


@bot.tree.command(name="removeloa",
                  description="Remove your own LOAs",
                  guild=discord.Object(id=GUILD_ID))
async def removeloa(interaction: discord.Interaction):
    loas = load_json(LOA_FILE, {})
    user_id = str(interaction.user.id)
    if user_id in loas:
        del loas[user_id]
        save_json(LOA_FILE, loas)
        await update_loa_list()
        await interaction.response.send_message(
            "✅ Your LOAs have been removed.", ephemeral=True)
    else:
        await interaction.response.send_message(
            "❌ You have no LOAs to remove.", ephemeral=True)


@bot.tree.command(name="removeloauser",
                  description="Admin: Remove LOAs for a user",
                  guild=discord.Object(id=GUILD_ID))
async def removeloauser(interaction: discord.Interaction,
                        member: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Not permitted.",
                                                ephemeral=True)
        return
    loas = load_json(LOA_FILE, {})
    user_id = str(member.id)
    if user_id in loas:
        del loas[user_id]
        save_json(LOA_FILE, loas)
        await update_loa_list()
        await interaction.response.send_message(
            f"✅ LOAs for {member.display_name} removed.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"❌ No LOAs found for {member.display_name}.", ephemeral=True)


@bot.tree.command(name="loas",
                  description="View active or upcoming LOAs",
                  guild=discord.Object(id=GUILD_ID))
async def view_loas(interaction: discord.Interaction):
    loas = load_json(LOA_FILE, {})
    today = date.today()
    output = []
    for uid, records in loas.items():
        member = interaction.guild.get_member(int(uid))
        if not member:
            continue
        name = member.display_name
        for entry in records:
            start = datetime.strptime(entry["start"], "%Y-%m-%d").date()
            end = datetime.strptime(entry["end"], "%Y-%m-%d").date()
            if end >= today:
                start_str = start.strftime("%d/%m/%Y")
                end_str = end.strftime("%d/%m/%Y")
                output.append(
                    f"📅 **{name}** — {start_str} to {end_str} – {entry['reason']}"
                )
    if output:
        await interaction.response.send_message("\n".join(output),
                                                ephemeral=True)
    else:
        await interaction.response.send_message(
            "✅ No active or upcoming LOAs.", ephemeral=True)


@bot.tree.command(name="setmessage",
                  description="Set turf announcement message",
                  guild=discord.Object(id=GUILD_ID))
async def setmessage(interaction: discord.Interaction, text: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Not permitted.",
                                                ephemeral=True)
        return
    settings["announcement"] = text
    save_json(SETTINGS_FILE, settings)
    await interaction.response.send_message("✅ Announcement message updated.",
                                            ephemeral=True)


@bot.tree.command(name="settime",
                  description="Set turf announcement time (hour minute)",
                  guild=discord.Object(id=GUILD_ID))
async def settime(interaction: discord.Interaction, hour: int, minute: int):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Not permitted.",
                                                ephemeral=True)
        return
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await interaction.response.send_message(
            "❌ Invalid time. Hour 0-23, minute 0-59.", ephemeral=True)
        return
    settings["hour"] = hour
    settings["minute"] = minute
    save_json(SETTINGS_FILE, settings)
    await interaction.response.send_message(
        f"✅ Turf time set to {hour:02d}:{minute:02d}.", ephemeral=True)


@bot.tree.command(name="forcesummary",
                  description="Force update summary now",
                  guild=discord.Object(id=GUILD_ID))
async def forcesummary(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Not permitted.",
                                                ephemeral=True)
        return
    await update_loa_list()
    await update_summary(force=True)
    await interaction.response.send_message("✅ Summary updated.",
                                            ephemeral=True)


@bot.tree.command(name="clearhistory",
                  description="Admin: Clear history for user or all",
                  guild=discord.Object(id=GUILD_ID))
async def clearhistory(interaction: discord.Interaction,
                       member: discord.Member = None):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Not permitted.",
                                                ephemeral=True)
        return
    # Clear history + responses + LOAs for member or all
    if member is None:
        # Clear all
        for file in os.listdir(ARCHIVE_FOLDER):
            os.remove(os.path.join(ARCHIVE_FOLDER, file))
        for file in [HISTORY_FILE, LOA_FILE]:
            if os.path.exists(file):
                os.remove(file)
        responses.clear()
        await interaction.response.send_message(
            "✅ Cleared all history, LOAs and responses.", ephemeral=True)
    else:
        uid = str(member.id)
        history = load_json(HISTORY_FILE, {})
        loas = load_json(LOA_FILE, {})
        if uid in history:
            del history[uid]
        if uid in loas:
            del loas[uid]
        save_json(HISTORY_FILE, history)
        save_json(LOA_FILE, loas)
        if uid in responses:
            del responses[uid]
        await interaction.response.send_message(
            f"✅ Cleared history, LOAs and responses for {member.display_name}.",
            ephemeral=True)


@bot.tree.command(name="stats",
                  description="Check attendance stats",
                  guild=discord.Object(id=GUILD_ID))
async def stats(interaction: discord.Interaction,
                member: discord.Member = None):
    member = member or interaction.user
    uid = str(member.id)
    history = load_json(HISTORY_FILE, {})
    user_data = history.get(uid, [])
    total = len(user_data)
    yes = sum(1 for x in user_data if x["available"] == "yes")
    no = total - yes
    reasons = [x["reason"] for x in user_data if x["reason"]]
    common = max(set(reasons), key=reasons.count) if reasons else "N/A"
    percent = round((yes / total) * 100, 1) if total > 0 else 0

    await interaction.response.send_message(
        f"📊 **Stats for {member.display_name}**\n"
        f"Total responses: {total}\n"
        f"✅ Yes: {yes}\n"
        f"❌ No: {no}\n"
        f"📈 Attendance: {percent}%\n"
        f"📝 Most common reason: {common}",
        ephemeral=True)


@bot.tree.command(name="leaderboard",
                  description="Show attendance leaderboard",
                  guild=discord.Object(id=GUILD_ID))
async def leaderboard(interaction: discord.Interaction):
    history = load_json(HISTORY_FILE, {})
    scores = []
    for uid, entries in history.items():
        total = len(entries)
        if total < MIN_RESPONSES_FOR_LEADERBOARD:
            continue
        yes = sum(1 for x in entries if x["available"] == "yes")
        percent = (yes / total) * 100
        scores.append((uid, percent, total))
    scores.sort(key=lambda x: x[1], reverse=True)
    lines = []
    for i, (uid, percent, total) in enumerate(scores[:5], 1):
        member = bot.get_guild(GUILD_ID).get_member(int(uid))
        name = member.display_name if member else f"User ID {uid}"
        lines.append(
            f"**{i}. {name}** - {percent:.1f}% attendance ({total} responses)")
    if not lines:
        await interaction.response.send_message(
            "No sufficient data for leaderboard.", ephemeral=True)
        return
    await interaction.response.send_message("🏆 **Attendance Leaderboard:**\n" +
                                            "\n".join(lines),
                                            ephemeral=True)


def is_admin(interaction):
    return any(role.id in settings.get("admin_roles", [])
               for role in interaction.user.roles
               ) or interaction.user.guild_permissions.administrator


@bot.event
async def on_member_remove(member):
    # Remove LOA if user leaves server
    loas = load_json(LOA_FILE, {})
    user_id = str(member.id)
    if user_id in loas:
        del loas[user_id]
        save_json(LOA_FILE, loas)
        await update_loa_list()

async def handle(request):
    return web.Response(text="Bot is alive!")

app = web.Application()
app.add_routes([web.get('/', handle)])

async def start_webserver():
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))  # Use env PORT or fallback to 8080
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Webserver running on port {port}")



@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

    # Start webserver for Uptime Robot pings
    bot.loop.create_task(start_webserver())

    guild = discord.Object(id=GUILD_ID)
    try:
        await bot.tree.sync(guild=guild)
        print("Slash commands synced.")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    load_all()
    await update_loa_list()
    await send_admin_panel()
    turf_check.start()
    clear_daily.start()


bot.run(os.environ['TOKEN'])
