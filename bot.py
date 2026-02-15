#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Discord бот для автоматического снятия роли через 24 часа после выдачи
"""

import os
import sys
import logging
import logging.handlers
from pathlib import Path
from datetime import datetime, timedelta, timezone
import sqlite3
import discord
from discord.ext import commands, tasks

# ==================== 1. НАСТРОЙКА ЛОГИРОВАНИЯ ====================
Path("logs").mkdir(exist_ok=True)

logger = logging.getLogger("role_manager_bot")
logger.setLevel(logging.DEBUG)  # DEBUG для детального логирования при отладке

formatter = logging.Formatter(
    '%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

file_handler = logging.handlers.RotatingFileHandler(
    "logs/bot.log",
    maxBytes=5_000_000,
    backupCount=5,
    encoding="utf-8"
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

discord_logger = logging.getLogger('discord')
discord_logger.setLevel(logging.WARNING)
discord_logger.addHandler(console_handler)
discord_logger.addHandler(file_handler)

# ==================== 2. НАСТРОЙКИ ====================
ROLE_ID_TO_TRACK = int(os.getenv("ROLE_ID", "1470909799502712935"))
CHECK_INTERVAL_MINUTES = 5
HOURS_UNTIL_REMOVAL = 24

if ROLE_ID_TO_TRACK == 0:
    logger.error("❌ Не указан ROLE_ID в переменных окружения! Остановка бота.")
    sys.exit(1)

# ==================== 3. БАЗА ДАННЫХ ====================
class Database:
    def __init__(self, path="roles.db"):
        self.path = path
        self.init_db()
    
    def init_db(self):
        try:
            conn = sqlite3.connect(self.path)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pending_roles (
                    user_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    assigned_at TEXT NOT NULL,
                    assigned_by TEXT NOT NULL,
                    PRIMARY KEY (user_id, guild_id, role_id)
                )
            """)
            conn.commit()
            conn.close()
            logger.info(f"✅ База данных инициализирована: {self.path}")
        except Exception as e:
            logger.exception(f"❌ Ошибка инициализации БД: {e}")
            raise
    
    def add_role(self, user_id: int, guild_id: int, role_id: int, assigned_by: str):
        try:
            conn = sqlite3.connect(self.path)
            cursor = conn.cursor()
            now = datetime.now(timezone.utc).isoformat()
            cursor.execute("""
                INSERT OR REPLACE INTO pending_roles 
                (user_id, guild_id, role_id, assigned_at, assigned_by)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, guild_id, role_id, now, assigned_by))
            conn.commit()
            conn.close()
            logger.info(f"➕ Роль {role_id} добавлена для пользователя {user_id} (выдал: {assigned_by})")
        except Exception as e:
            logger.exception(f"❌ Ошибка добавления роли в БД: {e}")
    
    def remove_role_record(self, user_id: int, guild_id: int, role_id: int):
        try:
            conn = sqlite3.connect(self.path)
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM pending_roles 
                WHERE user_id = ? AND guild_id = ? AND role_id = ?
            """, (user_id, guild_id, role_id))
            changed = cursor.rowcount
            conn.commit()
            conn.close()
            if changed:
                logger.info(f"➖ Запись удалена для пользователя {user_id}, роль {role_id}")
            return changed
        except Exception as e:
            logger.exception(f"❌ Ошибка удаления записи из БД: {e}")
            return 0
    
    def get_expired_roles(self, hours: int = 24):
        try:
            conn = sqlite3.connect(self.path)
            cursor = conn.cursor()
            expiry_time = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            cursor.execute("""
                SELECT user_id, guild_id, role_id, assigned_at, assigned_by 
                FROM pending_roles 
                WHERE assigned_at < ?
            """, (expiry_time,))
            results = cursor.fetchall()
            conn.close()
            logger.debug(f"📊 Найдено {len(results)} истёкших ролей (порог: {hours}ч)")
            return results
        except Exception as e:
            logger.exception(f"❌ Ошибка получения истёкших ролей из БД: {e}")
            return []
    
    def get_all_pending(self):
        try:
            conn = sqlite3.connect(self.path)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*), MIN(assigned_at) FROM pending_roles")
            count, oldest = cursor.fetchone()
            conn.close()
            return count, oldest
        except Exception as e:
            logger.exception(f"❌ Ошибка получения статистики из БД: {e}")
            return 0, None

# ==================== 4. КОГ С ЛОГИКОЙ БОТА ====================
class RoleManagerCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database):
        self.bot = bot
        self.db = db
        self.check_expired_roles.start()
        logger.info(f"⚙️  Отслеживаем роль ID: {ROLE_ID_TO_TRACK}")
        logger.info(f"⏰ Снятие через {HOURS_UNTIL_REMOVAL} часов")
        logger.info(f"🔄 Проверка каждые {CHECK_INTERVAL_MINUTES} минут")
    
    def cog_unload(self):
        self.check_expired_roles.cancel()
    
    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f"✅ Бот запущен как {self.bot.user} (ID: {self.bot.user.id})")
        logger.info(f"📊 Работает на {len(self.bot.guilds)} серверах")
        # Проверяем права бота на каждом сервере
        for guild in self.bot.guilds:
            bot_member = guild.get_member(self.bot.user.id)
            if bot_member and bot_member.guild_permissions.manage_roles:
                logger.info(f"✅ На сервере '{guild.name}' есть права Manage Roles")
            else:
                logger.warning(f"⚠️  На сервере '{guild.name}' НЕТ прав Manage Roles — бот не сможет снимать роли!")
        await self.bot.change_presence(
            activity=discord.Game(name=f"Слежу за ролью | !статус")
        )
    
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        before_roles = set(r.id for r in before.roles)
        after_roles = set(r.id for r in after.roles)
        
        # Роль добавлена
        if ROLE_ID_TO_TRACK in after_roles and ROLE_ID_TO_TRACK not in before_roles:
            assigner = "system/unknown"
            try:
                async for entry in after.guild.audit_logs(limit=10, action=discord.AuditLogAction.member_role_update):
                    if entry.target.id == after.id and hasattr(entry.after, 'roles'):
                        after_roles_audit = [r.id for r in entry.after.roles]
                        if ROLE_ID_TO_TRACK in after_roles_audit:
                            assigner = f"{entry.user} (ID: {entry.user.id})"
                            break
            except discord.Forbidden:
                logger.warning(f"⚠️  Нет прав на чтение Audit Log на сервере {after.guild.name}")
            except Exception as e:
                logger.warning(f"⚠️  Ошибка чтения Audit Log: {e}")
            
            self.db.add_role(after.id, after.guild.id, ROLE_ID_TO_TRACK, assigner)
            logger.info(f"🎁 Роль выдана: {after} (ID: {after.id}) выдал: {assigner}")
        
        # Роль снята вручную
        elif ROLE_ID_TO_TRACK in before_roles and ROLE_ID_TO_TRACK not in after_roles:
            removed = self.db.remove_role_record(after.id, after.guild.id, ROLE_ID_TO_TRACK)
            if removed:
                logger.info(f"↩️  Роль снята вручную: {after} (ID: {after.id})")
    
    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def check_expired_roles(self):
        """ПОЛНОСТЬЮ ЗАЩИЩЁННАЯ задача с обработкой ВСЕХ ошибок"""
        try:
            logger.debug("🔍 Запуск проверки истёкших ролей...")
            expired = self.db.get_expired_roles(hours=HOURS_UNTIL_REMOVAL)
            
            if not expired:
                logger.debug("✅ Нет ролей для снятия")
                return
            
            logger.info(f"⏰ Обнаружено {len(expired)} ролей для снятия (старше {HOURS_UNTIL_REMOVAL}ч)")
            processed = 0
            errors = 0
            
            for user_id, guild_id, role_id, assigned_at, assigned_by in expired:
                try:
                    guild = self.bot.get_guild(guild_id)
                    if not guild:
                        logger.warning(f"⚠️  Сервер {guild_id} не найден — удаляем запись")
                        self.db.remove_role_record(user_id, guild_id, role_id)
                        continue
                    
                    member = guild.get_member(user_id)
                    if not member:
                        logger.warning(f"⚠️  Пользователь {user_id} не на сервере {guild.name} — удаляем запись")
                        self.db.remove_role_record(user_id, guild_id, role_id)
                        continue
                    
                    role = guild.get_role(role_id)
                    if not role:
                        logger.warning(f"⚠️  Роль {role_id} не найдена на сервере {guild.name} — удаляем запись")
                        self.db.remove_role_record(user_id, guild_id, role_id)
                        continue
                    
                    # Проверка: может ли бот снять эту роль?
                    bot_member = guild.get_member(self.bot.user.id)
                    if bot_member and role >= bot_member.top_role:
                        logger.error(
                            f"❌ Невозможно снять роль {role.name} у {member} — роль бота ниже или равна. "
                            f"Роль бота: {bot_member.top_role}, роль цели: {role}"
                        )
                        continue
                    
                    # Снятие роли
                    await member.remove_roles(role, reason=f"Авто-снятие через {HOURS_UNTIL_REMOVAL}ч")
                    self.db.remove_role_record(user_id, guild_id, role_id)
                    processed += 1
                    logger.info(f"✅ Снята роль у {member} (ID: {member.id})")
                    
                    # Уведомление в ЛС
                    try:
                        await member.send(
                            f"👋 Роль `{role.name}` на сервере **{guild.name}** автоматически снята "
                            f"спустя {HOURS_UNTIL_REMOVAL} часов после получения."
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        logger.debug(f"✉️  Не удалось отправить ЛС пользователю {member.id}")
                
                except discord.Forbidden as e:
                    errors += 1
                    logger.error(f"❌ Нет прав для снятия роли у {user_id} на сервере {guild_id}: {e}")
                    # Не удаляем запись — возможно, права появятся позже
                except Exception as e:
                    errors += 1
                    logger.exception(f"❌ Ошибка при обработке записи (user={user_id}, guild={guild_id}): {e}")
            
            logger.info(f"✅ Завершена проверка: обработано {processed}, ошибок {errors} из {len(expired)} записей")
        
        except Exception as e:
            logger.exception(f"🔥 КРИТИЧЕСКАЯ ОШИБКА в задаче check_expired_roles: {e}")
    
    @check_expired_roles.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()
        logger.debug("✅ Фоновая задача готова к работе")
    
    # ==================== 5. КОМАНДЫ ДЛЯ АДМИНИСТРАТОРА ====================
    @commands.command(name="статус", aliases=["status", "info"])
    @commands.has_permissions(administrator=True)
    async def status(self, ctx: commands.Context):
        count, oldest = self.db.get_all_pending()
        
        embed = discord.Embed(
            title="📊 Статус бота управления ролями",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Отслеживаемая роль", value=f"<@&{ROLE_ID_TO_TRACK}> (ID: {ROLE_ID_TO_TRACK})", inline=False)
        embed.add_field(name="Активных записей", value=f"{count} пользователей", inline=True)
        embed.add_field(name="Снятие через", value=f"{HOURS_UNTIL_REMOVAL} часов", inline=True)
        embed.add_field(name="Проверка каждые", value=f"{CHECK_INTERVAL_MINUTES} минут", inline=True)
        
        if oldest and count > 0:
            try:
                # Надёжный парсинг даты с обработкой разных форматов
                clean_date = oldest.replace("Z", "+00:00") if "Z" in oldest else oldest
                assigned_dt = datetime.fromisoformat(clean_date)
                delta = datetime.now(timezone.utc) - assigned_dt
                hours = int(delta.total_seconds() // 3600)
                minutes = int((delta.total_seconds() % 3600) // 60)
                embed.add_field(
                    name="Самая старая запись", 
                    value=f"{hours}ч {minutes}м назад", 
                    inline=False
                )
            except Exception as e:
                logger.warning(f"⚠️  Ошибка парсинга даты '{oldest}': {e}")
                embed.add_field(name="Самая старая запись", value="ошибка парсинга", inline=False)
        
        # Проверка прав бота
        bot_member = ctx.guild.get_member(self.bot.user.id)
        if bot_member and bot_member.guild_permissions.manage_roles:
            perms_status = "✅ Есть"
        else:
            perms_status = "❌ Нет"
        embed.add_field(name="Права Manage Roles", value=perms_status, inline=True)
        
        embed.set_footer(text=f"Бот работает с {self.bot.user.name}")
        await ctx.send(embed=embed)
    
    @commands.command(name="очистить", aliases=["clear"])
    @commands.has_permissions(administrator=True)
    async def clear_db(self, ctx: commands.Context):
        conn = sqlite3.connect(self.db.path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM pending_roles")
        count = cursor.rowcount
        conn.commit()
        conn.close()
        await ctx.send(f"✅ Очищено {count} записей из базы данных")
        logger.warning(f"🧹 Администратор {ctx.author} очистил базу данных ({count} записей)")

# ==================== 6. ЗАПУСК БОТА ====================
def main():
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        print("❌ Не найдена переменная окружения DISCORD_TOKEN", file=sys.stderr)
        sys.exit(1)
    
    logger.info("🚀 Инициализация базы данных...")
    db = Database()
    
    intents = discord.Intents.default()
    intents.members = True
    intents.message_content = True
    
    bot = commands.Bot(
        command_prefix="!",
        intents=intents,
        help_command=None,
        case_insensitive=True
    )
    
    @bot.event
    async def on_command_error(ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ У вас нет прав для использования этой команды")
        elif isinstance(error, commands.CommandNotFound):
            pass
        else:
            logger.exception(f"Ошибка команды: {error}")
            await ctx.send("❌ Произошла внутренняя ошибка при выполнении команды")
    
    @bot.command()
    async def ping(ctx):
        await ctx.send(f"🏓 Pong! Задержка: {round(bot.latency * 1000)}ms")
    
    @bot.event
    async def on_ready():
        await bot.add_cog(RoleManagerCog(bot, db))
    
    try:
        logger.info("🚀 Запуск бота...")
        bot.run(TOKEN, log_handler=None)
    except discord.LoginFailure:
        logger.error("❌ Неверный токен бота. Проверьте DISCORD_TOKEN")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"❌ Критическая ошибка при запуске: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()