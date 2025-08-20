# defender_bot.py
# 需求：discord.py >= 2.3
# pip install -U "discord.py>=2.3.2"

import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import json
import os
from typing import Dict, Any, List
import functools
import discord
from discord.ext import commands

# ====== 基本設定 ======
BOT_TOKEN = "不會用自己的嗎"
DEFAULT_DELETE_DELAY = 120  # 新 webhook 在非白名單時，延遲刪除秒數
DATA_DIR = "guild_data"
os.makedirs(DATA_DIR, exist_ok=True)

intents = discord.Intents.default()
intents.message_content = True  # 允許讀取訊息
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree  # for slash commands


# ====== 多伺服器設定持久化 ======

def _config_path(guild_id: int) -> str:
    return os.path.join(DATA_DIR, f"{guild_id}.json")

def _default_config() -> Dict[str, Any]:
    return {
        "webhook_whitelist": [],     # 允許的 webhook URLs
        "blacklist": [],             # 進服即封的 user IDs
        "protected_roles": ["管理員", "Admin", "Administrator"],  # 被盜時自動撤回的高權限角色名
        "defense_users": [],         # 可使用防禦指令的人員 user IDs（除擁有者/管理員外）
        "delete_delay": DEFAULT_DELETE_DELAY,
        "log_channel_id": None       # 設定後，事件都會往這個頻道回報
    }

def load_config(guild_id: int) -> Dict[str, Any]:
    path = _config_path(guild_id)
    if not os.path.exists(path):
        cfg = _default_config()
        save_config(guild_id, cfg)
        return cfg
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(guild_id: int, cfg: Dict[str, Any]) -> None:
    path = _config_path(guild_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

async def get_log_channel(guild: discord.Guild) -> discord.TextChannel | None:
    cfg = load_config(guild.id)
    cid = cfg.get("log_channel_id")
    if cid:
        ch = guild.get_channel(cid)
        if isinstance(ch, discord.TextChannel):
            return ch
    # fallback：嘗試系統頻道
    return guild.system_channel if isinstance(guild.system_channel, discord.TextChannel) else None


# ====== 權限控管：誰能用防禦指令 ======

def is_guild_owner_or_admin_or_defender(interaction: discord.Interaction) -> bool:
    """伺服器擁有者 or 具有管理員權限 or 在 defense_users 白名單"""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    member: discord.Member = interaction.user
    if interaction.guild.owner_id == member.id:
        return True
    if member.guild_permissions.administrator:
        return True
    cfg = load_config(interaction.guild.id)
    return member.id in cfg.get("defense_users", [])

def require_defense_perm():
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            interaction = args[0] if args else kwargs.get("interaction")
            if not is_guild_owner_or_admin_or_defender(interaction):
                await interaction.response.send_message("❌ 你沒有使用此防禦指令的權限。", ephemeral=True)
                return
            return await func(*args, **kwargs)
        return wrapper
    return decorator


# ====== Bot 事件 ======

@bot.event
async def on_ready():
    print(f"✅ 防炸機器人已上線：{bot.user}")
    # 同步全域 slash 指令（初次需數分鐘；之後會快很多）
    try:
        await tree.sync()
        print("✅ Slash 指令已同步")
    except Exception as e:
        print("❌ 同步 Slash 指令失敗：", e)


# ====== 防禦核心：Webhook 監控 ======

# 注意：Bot 需要 Manage Webhooks 權限，否則無法讀取/刪除
@bot.event
async def on_webhooks_update(channel: discord.abc.GuildChannel):
    if not isinstance(channel, discord.TextChannel):
        return
    guild = channel.guild
    cfg = load_config(guild.id)
    delay = int(cfg.get("delete_delay", DEFAULT_DELETE_DELAY))

    try:
        hooks: List[discord.Webhook] = await channel.webhooks()
    except discord.Forbidden:
        return
    except Exception:
        return

    for hook in hooks:
        url = hook.url
        if url in cfg.get("webhook_whitelist", []):
            continue

        # 發通知 + 延遲刪除
        log_ch = await get_log_channel(guild)
        if log_ch:
            await log_ch.send(
                f"⚠️ 偵測到新 Webhook（未在白名單）：\n`{url}`\n"
                f"請在 {delay} 秒內使用 `/addwebhook url:<此URL>` 加入白名單，否則將自動刪除。"
            )

        await asyncio.sleep(delay)

        # 重新讀設定（期間可能被加入白名單了）
        cfg = load_config(guild.id)
        if url not in cfg.get("webhook_whitelist", []):
            try:
                await hook.delete(reason="未在白名單，防炸機器人自動清除")
                if log_ch:
                    await log_ch.send(f"🚨 已刪除未授權 Webhook：`{url}`")
            except discord.Forbidden:
                if log_ch:
                    await log_ch.send("⚠️ 權限不足，無法刪除此 Webhook。")
            except Exception as e:
                if log_ch:
                    await log_ch.send(f"⚠️ 刪除 Webhook 失敗：{e}")


# ====== 防禦核心：黑名單入服即封 ======

@bot.event
async def on_member_join(member: discord.Member):
    cfg = load_config(member.guild.id)
    if member.id in cfg.get("blacklist", []):
        try:
            await member.ban(reason="黑名單自動封鎖")
            log_ch = await get_log_channel(member.guild)
            if log_ch:
                await log_ch.send(f"🚫 黑名單帳號 {member.mention} 已自動封鎖。")
        except Exception as e:
            log_ch = await get_log_channel(member.guild)
            if log_ch:
                await log_ch.send(f"⚠️ 無法封鎖黑名單帳號 {member.mention}：{e}")


# ====== 防禦核心：被盜權限（保護角色）自動撤回 ======

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if before.roles == after.roles:
        return
    cfg = load_config(after.guild.id)
    protected = set(cfg.get("protected_roles", []))
    if not protected:
        return

    # 若 after 擁有受保護角色，但本人不是擁有者/管理員/授權防禦者 => 立刻撤回
    if not after.guild_permissions.administrator and after.id != after.guild.owner_id:
        # 偵測 after 擁有的保護角色
        remove_roles = [r for r in after.roles if r.name in protected]
        if remove_roles:
            try:
                await after.remove_roles(*remove_roles, reason="防炸：未授權獲得保護角色，已撤回")
                log_ch = await get_log_channel(after.guild)
                roles_str = ", ".join(r.name for r in remove_roles)
                if log_ch:
                    await log_ch.send(f"🚨 {after.mention} 非授權取得保護角色 `{roles_str}`，已自動撤回。")
            except Exception as e:
                log_ch = await get_log_channel(after.guild)
                if log_ch:
                    await log_ch.send(f"⚠️ 撤回保護角色失敗：{e}")


# ====== Slash 指令群：設定與權限管理 ======

@tree.command(name="setlogchannel", description="設定防禦事件的紀錄頻道")
@require_defense_perm()
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = load_config(interaction.guild.id)
    cfg["log_channel_id"] = channel.id
    save_config(interaction.guild.id, cfg)
    await interaction.response.send_message(f"✅ 已設定紀錄頻道為 {channel.mention}", ephemeral=True)

@tree.command(name="setdelay", description="設定未授權 Webhook 的延遲刪除秒數")
@require_defense_perm()
async def set_delay(interaction: discord.Interaction, seconds: app_commands.Range[int, 5, 3600]):
    cfg = load_config(interaction.guild.id)
    cfg["delete_delay"] = int(seconds)
    save_config(interaction.guild.id, cfg)
    await interaction.response.send_message(f"✅ 已將延遲刪除時間設定為 {seconds} 秒。", ephemeral=True)

# --- webhook 白名單 ---

@tree.command(name="addwebhook", description="將 Webhook URL 加入白名單")
@require_defense_perm()
async def add_webhook(interaction: discord.Interaction, url: str):
    cfg = load_config(interaction.guild.id)
    wl = cfg.get("webhook_whitelist", [])
    if url not in wl:
        wl.append(url)
        cfg["webhook_whitelist"] = wl
        save_config(interaction.guild.id, cfg)
        await interaction.response.send_message(f"✅ 已加入白名單：`{url}`", ephemeral=True)
    else:
        await interaction.response.send_message("⚠️ 此 Webhook 已在白名單中。", ephemeral=True)

@tree.command(name="listwebhook", description="查看 Webhook 白名單")
@require_defense_perm()
async def list_webhook(interaction: discord.Interaction):
    cfg = load_config(interaction.guild.id)
    wl = cfg.get("webhook_whitelist", [])
    if not wl:
        await interaction.response.send_message("📭 白名單為空。", ephemeral=True)
    else:
        joined = "\n".join(wl)
        await interaction.response.send_message(f"📋 Webhook 白名單：\n{joined}", ephemeral=True)

@tree.command(name="delwebhook", description="從白名單移除 Webhook URL")
@require_defense_perm()
async def del_webhook(interaction: discord.Interaction, url: str):
    cfg = load_config(interaction.guild.id)
    wl = cfg.get("webhook_whitelist", [])
    if url in wl:
        wl.remove(url)
        cfg["webhook_whitelist"] = wl
        save_config(interaction.guild.id, cfg)
        await interaction.response.send_message(f"🗑️ 已移除白名單：`{url}`", ephemeral=True)
    else:
        await interaction.response.send_message("⚠️ 白名單中沒有此 URL。", ephemeral=True)

# --- 黑名單 ---

@tree.command(name="addblacklist", description="新增黑名單用戶（進服自動封鎖）")
@require_defense_perm()
async def add_blacklist(interaction: discord.Interaction, user_id: str):
    try:
        uid = int(user_id)
    except ValueError:
        await interaction.response.send_message("❌ user_id 必須是數字。", ephemeral=True)
        return
    cfg = load_config(interaction.guild.id)
    bl = set(cfg.get("blacklist", []))
    if uid in bl:
        await interaction.response.send_message("⚠️ 該用戶已在黑名單中。", ephemeral=True)
        return
    bl.add(uid)
    cfg["blacklist"] = list(bl)
    save_config(interaction.guild.id, cfg)
    await interaction.response.send_message(f"🚫 已將 `{uid}` 加入黑名單。", ephemeral=True)

@tree.command(name="listblacklist", description="查看黑名單")
@require_defense_perm()
async def list_blacklist(interaction: discord.Interaction):
    cfg = load_config(interaction.guild.id)
    bl = cfg.get("blacklist", [])
    if not bl:
        await interaction.response.send_message("📭 黑名單為空。", ephemeral=True)
    else:
        await interaction.response.send_message("📋 黑名單：\n" + "\n".join(map(str, bl)), ephemeral=True)

@tree.command(name="delblacklist", description="從黑名單移除用戶")
@require_defense_perm()
async def del_blacklist(interaction: discord.Interaction, user_id: str):
    try:
        uid = int(user_id)
    except ValueError:
        await interaction.response.send_message("❌ user_id 必須是數字。", ephemeral=True)
        return
    cfg = load_config(interaction.guild.id)
    bl = set(cfg.get("blacklist", []))
    if uid not in bl:
        await interaction.response.send_message("⚠️ 黑名單中沒有該用戶。", ephemeral=True)
        return
    bl.remove(uid)
    cfg["blacklist"] = list(bl)
    save_config(interaction.guild.id, cfg)
    await interaction.response.send_message(f"🗑️ 已移除 `{uid}`。", ephemeral=True)

# --- 保護角色（被盜自動撤回） ---

@tree.command(name="addprotected", description="新增保護角色（若未授權取得會自動撤回）")
@require_defense_perm()
async def add_protected(interaction: discord.Interaction, role: discord.Role):
    cfg = load_config(interaction.guild.id)
    pr = set(cfg.get("protected_roles", []))
    if role.name in pr:
        await interaction.response.send_message("⚠️ 已在保護角色清單。", ephemeral=True)
        return
    pr.add(role.name)
    cfg["protected_roles"] = list(pr)
    save_config(interaction.guild.id, cfg)
    await interaction.response.send_message(f"🛡️ 已加入保護角色：`{role.name}`", ephemeral=True)

@tree.command(name="listprotected", description="查看保護角色")
@require_defense_perm()
async def list_protected(interaction: discord.Interaction):
    cfg = load_config(interaction.guild.id)
    pr = cfg.get("protected_roles", [])
    if not pr:
        await interaction.response.send_message("📭 目前沒有保護角色。", ephemeral=True)
    else:
        await interaction.response.send_message("📋 保護角色：\n" + "\n".join(pr), ephemeral=True)

@tree.command(name="delprotected", description="移除保護角色")
@require_defense_perm()
async def del_protected(interaction: discord.Interaction, role: discord.Role):
    cfg = load_config(interaction.guild.id)
    pr = set(cfg.get("protected_roles", []))
    if role.name not in pr:
        await interaction.response.send_message("⚠️ 不在保護角色清單。", ephemeral=True)
        return
    pr.remove(role.name)
    cfg["protected_roles"] = list(pr)
    save_config(interaction.guild.id, cfg)
    await interaction.response.send_message(f"🗑️ 已移除保護角色：`{role.name}`", ephemeral=True)

# --- 防禦指令授權（伺服器主/管理員可授權其他成員使用防禦指令） ---

@tree.command(name="grantdefense", description="授予成員防禦指令使用權")
async def grant_defense(interaction: discord.Interaction, member: discord.Member):
    # 僅伺服器擁有者或管理員能授權
    if not interaction.user.guild_permissions.administrator and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ 你沒有授權他人的權限。", ephemeral=True)
        return
    cfg = load_config(interaction.guild.id)
    du = set(cfg.get("defense_users", []))
    if member.id in du:
        await interaction.response.send_message("⚠️ 該成員已擁有防禦權限。", ephemeral=True)
        return
    du.add(member.id)
    cfg["defense_users"] = list(du)
    save_config(interaction.guild.id, cfg)
    await interaction.response.send_message(f"✅ 已授予 {member.mention} 防禦權限。", ephemeral=True)

@tree.command(name="revokedefense", description="撤銷成員防禦指令使用權")
async def revoke_defense(interaction: discord.Interaction, member: discord.Member):
    if not interaction.user.guild_permissions.administrator and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ 你沒有撤銷他人權限的權限。", ephemeral=True)
        return
    cfg = load_config(interaction.guild.id)
    du = set(cfg.get("defense_users", []))
    if member.id not in du:
        await interaction.response.send_message("⚠️ 該成員不在防禦授權名單。", ephemeral=True)
        return
    du.remove(member.id)
    cfg["defense_users"] = list(du)
    save_config(interaction.guild.id, cfg)
    await interaction.response.send_message(f"✅ 已撤銷 {member.mention} 的防禦權限。", ephemeral=True)

@tree.command(name="listdefense", description="查看具有防禦權限的成員")
@require_defense_perm()
async def list_defense(interaction: discord.Interaction):
    cfg = load_config(interaction.guild.id)
    du = cfg.get("defense_users", [])
    if not du:
        await interaction.response.send_message("📭 目前沒有額外授權的防禦成員。", ephemeral=True)
        return
    mentions = [f"<@{uid}>" for uid in du]
    await interaction.response.send_message("🛡️ 防禦成員清單：\n" + "\n".join(mentions), ephemeral=True)


# ====== Ping 測試 ======

@tree.command(name="ping", description="測試 bot 是否存活")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("Pong! 🏓", ephemeral=True)


# ====== 啟動 ======

if __name__ == "__main__":
    bot.run(BOT_TOKEN)
