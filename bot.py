import os
import threading
from flask import Flask
import discord
from discord.ext import commands, tasks
import sqlite3
from datetime import datetime, timedelta

# ===== 1. Flask сервер для keep-alive =====
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Bot is alive!", 200  # ВАЖНО: явно возвращаем статус 200

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

# Запускаем Flask в отдельном потоке ДО бота
threading.Thread(target=run_flask, daemon=True).start()

# ===== 2. Discord бот =====
intents = discord.Intents.default()
intents.members = True  # Обязательно для работы с ролями!

bot = commands.Bot(command_prefix='!', intents=intents)

# ===== 3. База данных для отслеживания ролей =====
def init_db():
    conn = sqlite3.connect('roles.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS pending_roles (
            user_id INTEGER,
            role_id INTEGER,
            guild_id INTEGER,
            assigned_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

@bot.event
async def on_ready():
    print(f'✅ Бот {bot.user} запущен и готов к работе!')
    init_db()
    check_roles.start()  # Запускаем фоновую задачу

# ===== 4. Фоновая задача для снятия ролей через 3 часа =====
@tasks.loop(minutes=5)
async def check_roles():
    conn = sqlite3.connect('roles.db')
    c = conn.cursor()
    three_hours_ago = (datetime.utcnow() - timedelta(hours=3)).isoformat()
    
    c.execute(
        "SELECT user_id, role_id, guild_id FROM pending_roles WHERE assigned_at < ?",
        (three_hours_ago,)
    )
    
    for user_id, role_id, guild_id in c.fetchall():
        guild = bot.get_guild(guild_id)
        if guild:
            member = guild.get_member(user_id)
            role = guild.get_role(role_id)
            if member and role:
                try:
                    await member.remove_roles(role, reason="Авто-снятие через 3 часа")
                    print(f"✅ Снята роль {role.name} у {member.display_name}")
                except Exception as e:
                    print(f"❌ Ошибка при снятии роли: {e}")
        c.execute(
            "DELETE FROM pending_roles WHERE user_id = ? AND role_id = ?",
            (user_id, role_id)
        )
    
    conn.commit()
    conn.close()

# ===== 5. Запуск бота =====
if __name__ == '__main__':
    bot.run(os.getenv('DISCORD_TOKEN'))