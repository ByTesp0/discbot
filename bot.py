import discord
from discord.ext import commands, tasks
import sqlite3
import os
from datetime import datetime, timedelta
import logging

# === НАСТРОЙКИ ===
ROLE_NAME = "бан"          # Точное название роли (регистр важен)
ROLE_DURATION_HOURS = 24    # Через сколько часов снимать роль
CHECK_INTERVAL = 60        # Проверка каждые N секунд

# === ЛОГИРОВАНИЕ ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('role-bot')

# === БАЗА ДАННЫХ ===
DB_FILE = "roles.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS role_assignments (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            assigned_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id, role_id)
        )
    """)
    conn.commit()
    conn.close()
    logger.info("✓ База данных инициализирована")

def add_role_assignment(guild_id: int, user_id: int, role_id: int, expires_at: datetime):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO role_assignments 
        (guild_id, user_id, role_id, assigned_at, expires_at)
        VALUES (?, ?, ?, ?, ?)
    """, (guild_id, user_id, role_id, datetime.utcnow().isoformat(), expires_at.isoformat()))
    conn.commit()
    conn.close()
    logger.info(f"⏱️ Роль {role_id} выдана пользователю {user_id} на сервере {guild_id} | Истекает: {expires_at}")

def remove_role_assignment(guild_id: int, user_id: int, role_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM role_assignments 
        WHERE guild_id = ? AND user_id = ? AND role_id = ?
    """, (guild_id, user_id, role_id))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if deleted:
        logger.info(f"🧹 Запись удалена: роль {role_id} у пользователя {user_id} (сервер {guild_id})")
    return deleted

def get_expired_roles():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT guild_id, user_id, role_id, expires_at 
        FROM role_assignments 
        WHERE expires_at <= ?
    """, (datetime.utcnow().isoformat(),))
    results = cursor.fetchall()
    conn.close()
    return results

def get_all_assignments():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT guild_id, user_id, role_id, expires_at FROM role_assignments")
    results = cursor.fetchall()
    conn.close()
    return results

# === БОТ ===
intents = discord.Intents.default()
intents.members = True  # Обязательно включить в портале разработчика Discord!
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

@tasks.loop(seconds=CHECK_INTERVAL)
async def check_expired_roles():
    """Проверяет и снимает просроченные роли каждые CHECK_INTERVAL секунд"""
    expired = get_expired_roles()
    if not expired:
        return

    logger.info(f"🔍 Найдено {len(expired)} просроченных ролей для снятия")
    
    for guild_id, user_id, role_id, expires_at_str in expired:
        guild = bot.get_guild(guild_id)
        if not guild:
            logger.warning(f"⚠️ Сервер {guild_id} недоступен — пропускаем")
            remove_role_assignment(guild_id, user_id, role_id)
            continue

        member = guild.get_member(user_id)
        role = guild.get_role(role_id)

        if not member:
            logger.warning(f"⚠️ Пользователь {user_id} не найден на сервере {guild.name} — удаляем запись")
            remove_role_assignment(guild_id, user_id, role_id)
            continue

        if not role:
            logger.warning(f"⚠️ Роль {role_id} не найдена на сервере {guild.name} — удаляем запись")
            remove_role_assignment(guild_id, user_id, role_id)
            continue

        # Пытаемся снять роль
        try:
            await member.remove_roles(role, reason=f"Авто-снятие: прошло {ROLE_DURATION_HOURS} часа")
            logger.info(f"✅ Роль '{role.name}' снята с {member} ({member.id}) на сервере {guild.name}")
        except discord.Forbidden:
            logger.error(f"❌ Нет прав на снятие роли '{role.name}' у {member} на сервере {guild.name}. "
                         f"Проверьте иерархию ролей: роль бота должна быть ВЫШЕ роли '{role.name}'")
        except Exception as e:
            logger.error(f"❌ Ошибка при снятии роли у {member}: {e}")

        # Удаляем запись в любом случае
        remove_role_assignment(guild_id, user_id, role_id)

@bot.event
async def on_ready():
    logger.info(f"🟢 Бот запущен как {bot.user}")
    logger.info(f"📊 Отслеживаем роль: '{ROLE_NAME}' (снятие через {ROLE_DURATION_HOURS} часа)")
    logger.info(f"⏱️ Проверка каждые {CHECK_INTERVAL} секунд")
    
    # Проверяем каждый сервер
    for guild in bot.guilds:
        logger.info(f"🏠 Сервер: {guild.name} (ID: {guild.id})")
        
        # Ищем роль по точному названию
        target_role = discord.utils.get(guild.roles, name=ROLE_NAME)
        
        # Если не найдена — ищем без учёта регистра
        if not target_role:
            target_role = discord.utils.find(lambda r: r.name.lower() == ROLE_NAME.lower(), guild.roles)
            if target_role:
                logger.warning(f"⚠️ Роль найдена как '{target_role.name}' (регистр отличается от '{ROLE_NAME}')")
        
        if not target_role:
            logger.warning(f"❌ Роль '{ROLE_NAME}' не найдена на сервере {guild.name}")
            continue

        # Проверяем иерархию ролей
        bot_member = guild.me
        if target_role.position >= bot_member.top_role.position:
            logger.error(
                f"❌ КРИТИЧЕСКАЯ ОШИБКА: Роль '{target_role.name}' находится ВЫШЕ или на уровне роли бота!\n"
                f"   Решение: переместите роль бота ВЫШЕ в настройках сервера (Настройки сервера → Роли)"
            )
            continue

        logger.info(f"✓ Роль '{target_role.name}' найдена (ID: {target_role.id})")
        logger.info(f"✓ Иерархия ролей корректна (роль бота: {bot_member.top_role.name})")

        # Сканируем текущих пользователей с этой ролью
        count = 0
        for member in guild.members:
            if target_role in member.roles:
                # Проверяем, есть ли уже запись в БД
                assignments = get_all_assignments()
                exists = any(
                    gid == guild.id and uid == member.id and rid == target_role.id
                    for gid, uid, rid, _ in assignments
                )
                
                if not exists:
                    expires_at = datetime.utcnow() + timedelta(hours=ROLE_DURATION_HOURS)
                    add_role_assignment(guild.id, member.id, target_role.id, expires_at)
                    count += 1
        
        if count > 0:
            logger.info(f"🆕 Обнаружено {count} пользователей с ролью '{target_role.name}' — таймеры запущены")

    # Запускаем проверку
    check_expired_roles.start()
    logger.info("✅ Система мониторинга ролей запущена")

@bot.event
async def on_member_update(before, after):
    """Отслеживает выдачу/снятие роли"""
    # Ищем роль на сервере (точное совпадение или без учёта регистра)
    target_role = discord.utils.get(after.guild.roles, name=ROLE_NAME)
    if not target_role:
        target_role = discord.utils.find(lambda r: r.name.lower() == ROLE_NAME.lower(), after.guild.roles)
    if not target_role:
        return

    # Роль выдана
    if target_role in after.roles and target_role not in before.roles:
        expires_at = datetime.utcnow() + timedelta(hours=ROLE_DURATION_HOURS)
        add_role_assignment(after.guild.id, after.id, target_role.id, expires_at)
        logger.info(f"🆕 Роль '{target_role.name}' выдана {after} ({after.id}) | Снятие: {expires_at.strftime('%H:%M:%S')}")

    # Роль снята вручную
    elif target_role not in after.roles and target_role in before.roles:
        if remove_role_assignment(after.guild.id, after.id, target_role.id):
            logger.info(f"✋ Роль '{target_role.name}' снята вручную у {after} ({after.id}) — запись удалена")

@bot.command(name="debug")
@commands.has_permissions(administrator=True)
async def debug(ctx):
    """Отладочная команда: показывает все активные таймеры"""
    assignments = get_all_assignments()
    if not assignments:
        await ctx.send("📭 Нет активных таймеров снятия ролей")
        return

    embed = discord.Embed(title="Активные таймеры снятия ролей", color=discord.Color.blue())
    for guild_id, user_id, role_id, expires_at in assignments[:10]:  # Первые 10 записей
        guild = bot.get_guild(guild_id)
        role = guild.get_role(role_id) if guild else None
        expires_dt = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
        time_left = expires_dt - datetime.utcnow()
        
        embed.add_field(
            name=f"Пользователь {user_id}",
            value=(
                f"Сервер: {guild.name if guild else 'неизвестен'}\n"
                f"Роль: {role.name if role else f'ID {role_id}'}\n"
                f"Осталось: {max(0, int(time_left.total_seconds() // 60))} мин"
            ),
            inline=False
        )
    
    if len(assignments) > 10:
        embed.set_footer(text=f"Показано 10 из {len(assignments)} записей")
    
    await ctx.send(embed=embed)

@debug.error
async def debug_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ У вас нет прав администратора для использования этой команды")

# === ЗАПУСК ===
if __name__ == "__main__":
    init_db()
    
    token = os.getenv("TOKEN")
    if not token:
        logger.critical("❌ Переменная окружения TOKEN не установлена!")
        logger.critical("   Railway: Variables → New Variable → KEY=TOKEN, VALUE=ваш_токен")
        exit(1)
    
    try:
        bot.run(token)
    except discord.LoginFailure:
        logger.critical("❌ Неверный токен бота. Проверьте переменную окружения TOKEN")
        exit(1)
    except Exception as e:
        logger.critical(f"❌ Критическая ошибка: {e}")
        exit(1)