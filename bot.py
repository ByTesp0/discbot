import os
import asyncio
import sqlite3
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread
import discord
from discord.ext import commands, tasks

# Инициализация базы данных
def init_db():
    db = sqlite3.connect('roles.db')
    cursor = db.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pending_roles (
            user_id INTEGER,
            role_id INTEGER,
            guild_id INTEGER,
            assigned_at TEXT
        )
    ''')
    db.commit()
    db.close()

init_db()

# Discord бот
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Flask для эндпоинта здоровья
app = Flask(__name__)

@app.route('/health')
def health():
    return "Bot is running!", 200

# Фоновая задача для проверки ролей
@tasks.loop(minutes=5)
async def check_roles():
    db = sqlite3.connect('roles.db')
    cursor = db.cursor()
    three_hours_ago = datetime.utcnow() - timedelta(hours=24)
    
    cursor.execute(
        "SELECT user_id, role_id, guild_id FROM pending_roles WHERE assigned_at < ?",
        (three_hours_ago.strftime('%Y-%m-%d %H:%M:%S'),)
    )
    
    to_remove = cursor.fetchall()
    
    for user_id, role_id, guild_id in to_remove:
        guild = bot.get_guild(guild_id)
        if guild:
            member = guild.get_member(user_id)
            role = guild.get_role(role_id)
            if member and role:
                try:
                    await member.remove_roles(role)
                    print(f"Removed role {role.name} from {member.name}")
                except Exception as e:
                    print(f"Error removing role: {e}")
        cursor.execute(
            "DELETE FROM pending_roles WHERE user_id = ? AND role_id = ?",
            (user_id, role_id)
        )
    
    db.commit()
    db.close()

@bot.event
async def on_ready():
    print(f'✅ Бот {bot.user} запущен!')
    if not check_roles.is_running():
        check_roles.start()

# Команда для ручного добавления записи (для тестирования)
@bot.command()
@commands.has_permissions(administrator=True)
async def testrole(ctx, member: discord.Member, role: discord.Role):
    db = sqlite3.connect('roles.db')
    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO pending_roles (user_id, role_id, guild_id, assigned_at) VALUES (?, ?, ?, ?)",
        (member.id, role.id, ctx.guild.id, datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'))
    )
    db.commit()
    db.close()
    await ctx.send(f"✅ Роль {role.mention} будет снята с {member.mention} через 3 часа")

# Запуск бота в отдельном потоке
def run_bot():
    bot.run(os.getenv('DISCORD_TOKEN'))

# Запуск Flask в основном потоке (для Render)
def run_flask():
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    # Запускаем бота в фоновом потоке
    bot_thread = Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    # Flask остаётся в основном потоке
    run_flask()