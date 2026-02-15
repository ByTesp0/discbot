#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Discord бот для автоматического снятия роли через 24 часа после выдачи
"""

import os
import sys
import logging
import logging.handlers  # 🔑 КРИТИЧЕСКИ ВАЖНО: явный импорт модуля handlers
from pathlib import Path
from datetime import datetime, timedelta, timezone
import sqlite3
import discord
from discord.ext import commands, tasks

# ==================== 1. НАСТРОЙКА ЛОГИРОВАНИЯ ====================
Path("logs").mkdir(exist_ok=True)

logger = logging.getLogger("role_manager_bot")
logger.setLevel(logging.INFO)

formatter = logging.Formatter(
    '%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Консоль (для Render Logs)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# Файл с ротацией — ИСПОЛЬЗУЕМ ЯВНЫЙ ИМПОРТ
file_handler = logging.handlers.RotatingFileHandler(
    "logs/bot.log",
    maxBytes=5_000_000,
    backupCount=5,
    encoding="utf-8"
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Логирование самого discord.py
discord_logger = logging.getLogger('discord')
discord_logger.setLevel(logging.WARNING)
discord_logger.addHandler(console_handler)
discord_logger.addHandler(file_handler)

# ==================== 2. НАСТРОЙКИ ====================
ROLE_ID_TO_TRACK = int(os.getenv("ROLE_ID", "1470909799502712935"))  # ID роли, которую нужно отслеживать
CHECK_INTERVAL_MINUTES = 5  # Как часто проверять истёкшие роли
HOURS_UNTIL_REMOVAL = 24  # Через сколько часов снимать роль

if ROLE_ID_TO_TRACK == 0:
    logger.error("❌ Не указан ROLE_ID в переменных окружения! Остановка бота.")
    sys.exit(1)

# ==================== 3. БАЗА ДАННЫХ ====================
class Database:
    def __init__(self, path="roles.db"):
        self.path = path
        self.init_db()
    
    def init_db(self):
        """Создаём таблицу при первом запуске"""
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
        logger.info("✅ База данных инициализирована")
    
    def add_role(self, user_id: int, guild_id: int, role_id: int, assigned_by: str):
        """Добавляем запись о выданной роли"""
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
    
    def remove_role_record(self, user_id: int, guild_id: int, role_id: int):
        """Удаляем запись (роль снята вручную или истекла)"""
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
    
    def get_expired_roles(self, hours: int = 24):
        """Получаем все роли, которые нужно снять (старше N часов)"""
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
        return results
    
    def get_all_pending(self):
        """Получаем все активные записи для статуса"""
        conn = sqlite3.connect(self.path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*), MIN(assigned_at) FROM pending_roles")
        count, oldest = cursor.fetchone()
        conn.close()
        return count, oldest

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
        # Показываем статус в присутствии
        await self.bot.change_presence(
            activity=discord.Game(name=f"Слежу за ролью | !статус")
        )
    
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Отслеживаем выдачу/снятие отслеживаемой роли"""
        # Находим разницу в ролях
        before_roles = set(r.id for r in before.roles)
        after_roles = set(r.id for r in after.roles)
        
        # Роль добавлена
        if ROLE_ID_TO_TRACK in after_roles and ROLE_ID_TO_TRACK not in before_roles:
            # Определяем, кто выдал роль (если это бот — будет системное сообщение)
            assigner = "system/unknown"
            try:
                async for entry in after.guild.audit_logs(limit=5, action=discord.AuditLogAction.member_role_update):
                    if entry.target.id == after.id:
                        # Проверяем, была ли добавлена нужная роль
                        if hasattr(entry.after, 'roles'):
                            after_roles_audit = [r.id for r in entry.after.roles]
                            if ROLE_ID_TO_TRACK in after_roles_audit:
                                assigner = f"{entry.user.name}#{entry.user.discriminator} (ID: {entry.user.id})"
                                break
            except Exception as e:
                logger.warning(f"⚠️  Не удалось прочитать audit log: {e}")
            
            self.db.add_role(after.id, after.guild.id, ROLE_ID_TO_TRACK, assigner)
            logger.info(f"🎁 Роль выдана: {after} (ID: {after.id}) выдал: {assigner}")
        
        # Роль снята вручную (до истечения 24ч)
        elif ROLE_ID_TO_TRACK in before_roles and ROLE_ID_TO_TRACK not in after_roles:
            removed = self.db.remove_role_record(after.id, after.guild.id, ROLE_ID_TO_TRACK)
            if removed:
                logger.info(f"↩️  Роль снята вручную: {after} (ID: {after.id})")
    
    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def check_expired_roles(self):
        """Периодическая проверка и снятие истёкших ролей"""
        expired = self.db.get_expired_roles(hours=HOURS_UNTIL_REMOVAL)
        if not expired:
            return
        
        logger.info(f"🔍 Найдено {len(expired)} истёкших ролей для снятия")
        
        for user_id, guild_id, role_id, assigned_at, assigned_by in expired:
            guild = self.bot.get_guild(guild_id)
            if not guild:
                logger.warning(f"⚠️  Сервер {guild_id} не найден, пропускаем")
                continue
            
            member = guild.get_member(user_id)
            if not member:
                logger.warning(f"⚠️  Пользователь {user_id} не найден на сервере {guild.name}, пропускаем")
                self.db.remove_role_record(user_id, guild_id, role_id)  # Удаляем "мёртвую" запись
                continue
            
            role = guild.get_role(role_id)
            if not role:
                logger.warning(f"⚠️  Роль {role_id} не найдена на сервере {guild.name}")
                self.db.remove_role_record(user_id, guild_id, role_id)
                continue
            
            # Снимаем роль
            try:
                await member.remove_roles(role, reason=f"Авто-снятие через {HOURS_UNTIL_REMOVAL}ч (выдано: {assigned_by})")
                self.db.remove_role_record(user_id, guild_id, role_id)
                logger.info(f"⏰ Снята роль у {member} (ID: {member.id}) спустя {HOURS_UNTIL_REMOVAL}ч")
                
                # Отправляем уведомление в личку (опционально)
                try:
                    await member.send(
                        f"👋 Привет! На сервере **{guild.name}** у тебя автоматически снята роль <@&{role.id}>, "
                        f"так как прошло {HOURS_UNTIL_REMOVAL} часа с момента её получения."
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass  # Пользователь закрыл ЛС — не критично
                
            except discord.HTTPException as e:
                logger.error(f"❌ Ошибка при снятии роли у {member.id}: {e}")
    
    @check_expired_roles.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()
    
    # ==================== 5. КОМАНДЫ ДЛЯ АДМИНИСТРАТОРА ====================
    @commands.command(name="статус", aliases=["status", "info"])
    @commands.has_permissions(administrator=True)
    async def status(self, ctx: commands.Context):
        """Показать статистику бота"""
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
            assigned_dt = datetime.fromisoformat(oldest.replace("Z", "+00:00"))
            delta = datetime.now(timezone.utc) - assigned_dt
            hours = int(delta.total_seconds() // 3600)
            minutes = int((delta.total_seconds() % 3600) // 60)
            embed.add_field(
                name="Самая старая запись", 
                value=f"{hours}ч {minutes}м назад", 
                inline=False
            )
        
        embed.set_footer(text=f"Бот работает с {self.bot.user.name}")
        await ctx.send(embed=embed)
    
    @commands.command(name="очистить", aliases=["clear"])
    @commands.has_permissions(administrator=True)
    async def clear_db(self, ctx: commands.Context):
        """Очистить базу данных (осторожно!)"""
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
    # Проверка токена
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        print("❌ Не найдена переменная окружения DISCORD_TOKEN", file=sys.stderr)
        sys.exit(1)
    
    # Инициализация БД
    db = Database()
    
    # Настройка бота
    intents = discord.Intents.default()
    intents.members = True  # Обязательно для отслеживания ролей
    intents.message_content = True  # Для команд
    
    bot = commands.Bot(
        command_prefix="!",
        intents=intents,
        help_command=None,
        case_insensitive=True
    )
    
    # Регистрация кога
    @bot.event
    async def on_command_error(ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ У вас нет прав для использования этой команды")
        elif isinstance(error, commands.CommandNotFound):
            pass  # Игнорируем неизвестные команды
        else:
            logger.exception(f"Ошибка команды: {error}")
    
    @bot.command()
    async def ping(ctx):
        """Проверка работоспособности"""
        await ctx.send(f"🏓 Pong! Задержка: {round(bot.latency * 1000)}ms")
    
    # Загружаем ког при готовности
    @bot.event
    async def on_ready():
        await bot.add_cog(RoleManagerCog(bot, db))
    
    # Запуск с обработкой ошибок
    try:
        logger.info("🚀 Запуск бота...")
        bot.run(TOKEN, log_handler=None)  # log_handler=None чтобы не дублировать логи
    except discord.LoginFailure:
        logger.error("❌ Неверный токен бота. Проверьте DISCORD_TOKEN")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"❌ Критическая ошибка: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()