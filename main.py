import asyncio
from datetime import datetime, timedelta
import json
import os
import re
import secrets
import sqlite3
import subprocess
import time
from typing import Optional
import urllib.request
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "bar_pos.db")

load_dotenv(os.path.join(BASE_DIR, ".env"))

app = FastAPI()
security = HTTPBasic()

active_event = {"id": None, "type": None, "expires": 0}
tunnel_process = None
active_game_status = "Checking schedule..."
active_team = "none"


# --- Centralized Database Helper with WAL & Timeout ---
def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=10.0)
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def get_setting(cursor, key: str, default: str = "") -> str:
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    return row[0] if (row and row[0] is not None) else default


def set_setting(cursor, key: str, value: str):
    cursor.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    # Enable Write-Ahead Logging for fast concurrent reads & writes
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.execute("PRAGMA synchronous = NORMAL")

    cursor.execute(
        "CREATE TABLE IF NOT EXISTS guests ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, device_id TEXT UNIQUE)"
    )
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS menu ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, item_name TEXT, category TEXT)"
    )
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS orders ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, guest_id INTEGER, menu_id INTEGER, "
        "status TEXT DEFAULT 'pending', timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, "
        "FOREIGN KEY(guest_id) REFERENCES guests(id), FOREIGN KEY(menu_id) REFERENCES menu(id))"
    )
    cursor.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS banned_ips (ip TEXT PRIMARY KEY)")

    # Schema migrations
    migrations = [
        "ALTER TABLE orders ADD COLUMN custom_item TEXT",
        "ALTER TABLE orders ADD COLUMN reject_reason TEXT",
        "ALTER TABLE orders ADD COLUMN order_note TEXT",
        "ALTER TABLE orders ADD COLUMN custom_abv TEXT",
        "ALTER TABLE orders ADD COLUMN custom_calories TEXT",
        "ALTER TABLE guests ADD COLUMN is_cut_off INTEGER DEFAULT 0",
        "ALTER TABLE guests ADD COLUMN ip_address TEXT",
        "ALTER TABLE guests ADD COLUMN location TEXT",
        "ALTER TABLE guests ADD COLUMN user_agent TEXT",
        "ALTER TABLE guests ADD COLUMN aliases TEXT DEFAULT ''",
        "ALTER TABLE menu ADD COLUMN abv TEXT",
        "ALTER TABLE menu ADD COLUMN calories TEXT",
        "ALTER TABLE menu ADD COLUMN notes TEXT",
        "ALTER TABLE menu ADD COLUMN container TEXT",
        "ALTER TABLE menu ADD COLUMN max_pours INTEGER",
        "ALTER TABLE menu ADD COLUMN current_pours INTEGER",
        "ALTER TABLE menu ADD COLUMN is_special INTEGER DEFAULT 0",
    ]
    for sql in migrations:
        try:
            cursor.execute(sql)
        except sqlite3.OperationalError:
            pass

    # Performance indexes for frequent polling queries
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_guest_id ON orders(guest_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_guests_device_id ON guests(device_id)")

    # Migrate legacy .txt files into SQLite settings if present
    default_title = "🎄 Ugly Sweater Party Specials"
    title_file = os.path.join(BASE_DIR, "specials_title.txt")
    if os.path.exists(title_file):
        try:
            with open(title_file, "r") as f:
                default_title = f.read().strip() or default_title
            os.remove(title_file)
        except Exception:
            pass

    default_theme = "auto"
    theme_file = os.path.join(BASE_DIR, "theme_mode.txt")
    if os.path.exists(theme_file):
        try:
            with open(theme_file, "r") as f:
                default_theme = f.read().strip() or default_theme
            os.remove(theme_file)
        except Exception:
            pass

    defaults = {
        "daily_code": "",
        "bar_status": "open",
        "tunnel_mode": "auto",
        "specials_title": default_title,
        "theme_mode": default_theme,
    }
    for k, v in defaults.items():
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    cursor.execute("SELECT COUNT(*) FROM menu")
    if cursor.fetchone()[0] == 0:
        default_drinks = [
            ("Labatt Blue", "Beer"),
            ("Southern Tier IPA", "Beer"),
            ("Fattey Beer House Lager", "Beer"),
            ("Vodka Cranberry", "Cocktail"),
            ("Old Fashioned", "Cocktail"),
        ]
        cursor.executemany("INSERT INTO menu (item_name, category) VALUES (?, ?)", default_drinks)

    conn.commit()
    conn.close()


init_db()


# --- Background Game Schedule & Cloudflare Tunnel Manager ---
async def manage_tunnel_schedule():
    global tunnel_process, active_game_status, active_team
    EST = ZoneInfo("America/New_York")

    while True:
        try:
            now = datetime.now(EST)
            bills_game = False
            sabres_time = None

            try:
                req = urllib.request.Request(
                    "http://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/BUF/schedule",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                with urllib.request.urlopen(req, timeout=5) as response:
                    data = json.loads(response.read().decode())
                    for event in data.get("events", []):
                        game_time = (
                            datetime.strptime(event["date"], "%Y-%m-%dT%H:%MZ")
                            .replace(tzinfo=ZoneInfo("UTC"))
                            .astimezone(EST)
                        )
                        if game_time.date() == now.date():
                            bills_game = True
                            break
            except Exception as e:
                print("Bills API error:", e)

            try:
                req = urllib.request.Request(
                    "http://site.api.espn.com/apis/site/v2/sports/hockey/nhl/teams/BUF/schedule",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                with urllib.request.urlopen(req, timeout=5) as response:
                    data = json.loads(response.read().decode())
                    for event in data.get("events", []):
                        game_time = (
                            datetime.strptime(event["date"], "%Y-%m-%dT%H:%MZ")
                            .replace(tzinfo=ZoneInfo("UTC"))
                            .astimezone(EST)
                        )
                        if game_time.date() == now.date():
                            sabres_time = game_time
                            break
            except Exception as e:
                print("Sabres API error:", e)

            should_run = False
            status_msg = "No games scheduled today."
            current_team = "none"

            if bills_game:
                current_team = "bills"
                status_msg = "🏈 Bills Game Today"
                if now.hour >= 8:
                    should_run = True
            elif sabres_time:
                current_team = "sabres"
                puck_drop = sabres_time.strftime("%I:%M %p").lstrip("0")
                status_msg = f"🏒 Sabres Game Today ({puck_drop})"
                start_time = sabres_time - timedelta(hours=1)
                end_time = sabres_time + timedelta(hours=5)
                if start_time <= now <= end_time:
                    should_run = True

            active_game_status = status_msg
            active_team = current_team

            conn_db = get_db()
            tunnel_mode = get_setting(conn_db.cursor(), "tunnel_mode", "auto")
            conn_db.close()

            if tunnel_mode == "on":
                should_run = True
            elif tunnel_mode == "off":
                should_run = False

            if should_run:
                if tunnel_process is None or tunnel_process.poll() is not None:
                    print("Activating Cloudflare Tunnel...")
                    tunnel_process = subprocess.Popen(
                        [
                            "/usr/local/bin/cloudflared",
                            "tunnel",
                            "run",
                            "--url",
                            "http://localhost:80",
                            "mafiabasementbar",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            else:
                if tunnel_process is not None and tunnel_process.poll() is None:
                    print("Deactivating Cloudflare Tunnel...")
                    tunnel_process.terminate()
                    tunnel_process.wait()
                    tunnel_process = None

        except Exception as e:
            print("Scheduler loop error:", e)

        await asyncio.sleep(300)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(manage_tunnel_schedule())


@app.on_event("shutdown")
def shutdown_event():
    global tunnel_process
    if tunnel_process is not None and tunnel_process.poll() is None:
        tunnel_process.terminate()
        tunnel_process.wait()


# --- Authentication ---
def verify_bartender(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(
        credentials.username, os.getenv("BARTENDER_USER", "admin")
    )
    correct_password = secrets.compare_digest(
        credentials.password, os.getenv("BARTENDER_PASS", "password")
    )
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# --- Pydantic Models ---
class OrderRequest(BaseModel):
    guest_name: str
    device_id: str
    menu_id: Optional[int] = None
    custom_item: Optional[str] = None
    order_note: Optional[str] = None
    custom_abv: Optional[str] = None
    custom_calories: Optional[str] = None
    access_code: Optional[str] = None


class MenuItemRequest(BaseModel):
    item_name: str
    category: str
    abv: Optional[str] = None
    calories: Optional[str] = None
    notes: Optional[str] = None
    container: Optional[str] = None
    inventory: Optional[int] = None
    max_inventory: Optional[int] = None


class RejectRequest(BaseModel):
    reason: Optional[str] = None


class NameUpdateRequest(BaseModel):
    new_name: str


class CodeRequest(BaseModel):
    code: str


class StatusRequest(BaseModel):
    bar_status: Optional[str] = None
    tunnel_mode: Optional[str] = None


class AdjustInventoryRequest(BaseModel):
    delta: int


class SpecialsTitleRequest(BaseModel):
    title: str


class ThemeModeRequest(BaseModel):
    theme: str


# --- HTML & Static File Routes ---
@app.get("/")
def serve_phone_menu():
    return FileResponse(os.path.join(BASE_DIR, "menu.html"))


@app.get("/dashboard")
def serve_dashboard():
    return FileResponse(os.path.join(BASE_DIR, "dashboard.html"))


@app.get("/board")
def serve_board():
    return FileResponse(os.path.join(BASE_DIR, "board.html"))


@app.get("/bartender")
def serve_bartender(user: str = Depends(verify_bartender)):
    return FileResponse(os.path.join(BASE_DIR, "bartender.html"))


@app.get("/qrcode_mafiabasementbar.com.png")
def serve_qr():
    return FileResponse(os.path.join(BASE_DIR, "qrcode_mafiabasementbar.com.png"))


# --- Event, Theme & Settings Routes ---
@app.get("/api/event/current")
def get_current_event():
    conn = get_db()
    theme_override = get_setting(conn.cursor(), "theme_mode", "auto")
    conn.close()

    effective_team = active_team if theme_override == "auto" else theme_override
    resp = {"event": None, "id": None, "team": effective_team}
    if time.time() < active_event["expires"]:
        resp["event"] = active_event["type"]
        resp["id"] = active_event["id"]
    return resp


@app.get("/api/event/trigger/{event_type}")
def trigger_event(event_type: str, user: str = Depends(verify_bartender)):
    active_event["id"] = str(time.time())
    active_event["type"] = event_type
    active_event["expires"] = time.time() + 60
    try:
        url = None
        if event_type == "goal":
            url = os.getenv("SABRES_WEBHOOK_URL")
        elif event_type == "touchdown":
            url = os.getenv("BILLS_WEBHOOK_URL")
        if url:
            urllib.request.urlopen(url, timeout=3)
    except Exception as e:
        print("Webhook failed:", e)
    return {"status": "success", "event": event_type}


@app.get("/api/tunnel/status")
def get_tunnel_status(user: str = Depends(verify_bartender)):
    is_open = tunnel_process is not None and tunnel_process.poll() is None
    return {"is_open": is_open, "message": active_game_status}


@app.get("/api/settings/code")
def get_code():
    conn = get_db()
    code = get_setting(conn.cursor(), "daily_code", "")
    conn.close()
    return {"code": code}


@app.put("/api/settings/code")
def update_code(req: CodeRequest, user: str = Depends(verify_bartender)):
    conn = get_db()
    set_setting(conn.cursor(), "daily_code", req.code.strip())
    conn.commit()
    conn.close()
    return {"status": "success"}


@app.post("/api/verify_code")
def verify_code(req: CodeRequest):
    conn = get_db()
    real_code = get_setting(conn.cursor(), "daily_code", "")
    conn.close()
    if not real_code or req.code.strip().lower() == real_code.lower():
        return {"valid": True}
    return {"valid": False}


@app.get("/api/settings/status")
def get_status():
    conn = get_db()
    rows = (
        conn.cursor()
        .execute("SELECT key, value FROM settings WHERE key IN ('bar_status', 'tunnel_mode')")
        .fetchall()
    )
    conn.close()
    return {r[0]: r[1] for r in rows}


@app.put("/api/settings/status")
def update_status(req: StatusRequest, user: str = Depends(verify_bartender)):
    global tunnel_process
    conn = get_db()
    c = conn.cursor()
    if req.bar_status:
        set_setting(c, "bar_status", req.bar_status)
    if req.tunnel_mode:
        set_setting(c, "tunnel_mode", req.tunnel_mode)
        if req.tunnel_mode == "on" and (tunnel_process is None or tunnel_process.poll() is not None):
            tunnel_process = subprocess.Popen(
                [
                    "/usr/local/bin/cloudflared",
                    "tunnel",
                    "run",
                    "--url",
                    "http://localhost:80",
                    "mafiabasementbar",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif req.tunnel_mode == "off" and tunnel_process is not None and tunnel_process.poll() is None:
            tunnel_process.terminate()
            tunnel_process.wait()
            tunnel_process = None
    conn.commit()
    conn.close()
    return {"status": "success"}


@app.get("/api/settings/specials_title")
def get_specials_title():
    conn = get_db()
    title = get_setting(conn.cursor(), "specials_title", "🎄 Ugly Sweater Party Specials")
    conn.close()
    return {"title": title or "⭐ Bartender's Specials"}


@app.put("/api/settings/specials_title")
def set_specials_title(req: SpecialsTitleRequest, user: str = Depends(verify_bartender)):
    clean_title = req.title.strip() or "⭐ Bartender's Specials"
    conn = get_db()
    set_setting(conn.cursor(), "specials_title", clean_title)
    conn.commit()
    conn.close()
    return {"status": "success", "title": clean_title}


@app.get("/api/settings/theme")
def get_theme_setting():
    conn = get_db()
    theme = get_setting(conn.cursor(), "theme_mode", "auto")
    conn.close()
    return {"theme": theme}


@app.put("/api/settings/theme")
def set_theme_setting(req: ThemeModeRequest, user: str = Depends(verify_bartender)):
    clean = req.theme.strip().lower()
    if clean not in ("auto", "christmas", "bills", "sabres", "bluejays", "none"):
        clean = "auto"
    conn = get_db()
    set_setting(conn.cursor(), "theme_mode", clean)
    conn.commit()
    conn.close()
    return {"status": "success", "theme": clean}


# --- Menu & Inventory Routes ---
@app.get("/menu")
def get_menu():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, item_name, category, abv, calories, notes, container, "
        "current_pours, max_pours, is_special FROM menu"
    )
    drinks = [
        {
            "id": row[0],
            "item_name": row[1],
            "category": row[2],
            "abv": row[3],
            "calories": row[4],
            "notes": row[5],
            "container": row[6],
            "current_pours": row[7],
            "max_pours": row[8],
            "is_special": bool(row[9]) if row[9] else False,
        }
        for row in cursor.fetchall()
    ]
    conn.close()
    return {"menu": drinks}


@app.post("/api/menu")
def add_menu_item(item: MenuItemRequest, user: str = Depends(verify_bartender)):
    conn = get_db()
    cursor = conn.cursor()
    max_p = item.max_inventory if item.max_inventory is not None else item.inventory
    curr_p = item.inventory if item.inventory is not None else max_p
    cursor.execute(
        "INSERT INTO menu (item_name, category, abv, calories, notes, container, max_pours, current_pours) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (item.item_name, item.category, item.abv, item.calories, item.notes, item.container, max_p, curr_p),
    )
    conn.commit()
    conn.close()
    return {"message": "Menu item added"}


@app.put("/api/menu/{item_id}")
def edit_menu_item(item_id: int, item: MenuItemRequest, user: str = Depends(verify_bartender)):
    conn = get_db()
    cursor = conn.cursor()
    if item.inventory is not None or item.max_inventory is not None:
        cursor.execute("SELECT max_pours, current_pours FROM menu WHERE id=?", (item_id,))
        row = cursor.fetchone()
        existing_max = row[0] if row else None
        existing_curr = row[1] if row else None
        max_p = (
            item.max_inventory
            if item.max_inventory is not None
            else (existing_max if existing_max is not None else item.inventory)
        )
        curr_p = (
            item.inventory
            if item.inventory is not None
            else (existing_curr if existing_curr is not None else max_p)
        )
        if item.container in ("Can", "Bottle") and curr_p is not None and max_p is not None and curr_p > max_p:
            max_p = curr_p
        cursor.execute(
            "UPDATE menu SET item_name=?, category=?, abv=?, calories=?, notes=?, "
            "container=?, max_pours=?, current_pours=? WHERE id=?",
            (item.item_name, item.category, item.abv, item.calories, item.notes, item.container, max_p, curr_p, item_id),
        )
    else:
        cursor.execute(
            "UPDATE menu SET item_name=?, category=?, abv=?, calories=?, notes=?, container=? WHERE id=?",
            (item.item_name, item.category, item.abv, item.calories, item.notes, item.container, item_id),
        )
    conn.commit()
    conn.close()
    return {"message": "Menu item updated"}


@app.delete("/api/menu/{item_id}")
def delete_menu_item(item_id: int, user: str = Depends(verify_bartender)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT item_name FROM menu WHERE id = ?", (item_id,))
    row = cursor.fetchone()
    if row:
        cursor.execute("UPDATE orders SET custom_item = ? WHERE menu_id = ?", (row[0], item_id))
    cursor.execute("DELETE FROM menu WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return {"message": "Menu item deleted"}


@app.post("/api/menu/{item_id}/reset_keg")
def reset_keg(item_id: int, user: str = Depends(verify_bartender)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE menu SET current_pours = max_pours WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return {"message": "Keg reset"}


@app.post("/api/menu/{item_id}/adjust")
def adjust_inventory(item_id: int, req: AdjustInventoryRequest, user: str = Depends(verify_bartender)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT current_pours, max_pours, container, category FROM menu WHERE id = ?", (item_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return {"status": "error", "message": "Item not found"}
    curr_p, max_p, container, category = row
    curr_p = curr_p if curr_p is not None else 0
    new_curr = max(0, curr_p + req.delta)

    is_capped = container in ("Draft", "Liquor") or category in ("Wine", "Liquor", "Shot")
    if is_capped and max_p is not None and max_p > 0:
        new_curr = min(max_p, new_curr)
    elif max_p is not None and new_curr > max_p:
        max_p = new_curr

    cursor.execute("UPDATE menu SET current_pours = ?, max_pours = ? WHERE id = ?", (new_curr, max_p, item_id))
    conn.commit()
    conn.close()
    return {"status": "success", "current_pours": new_curr, "max_pours": max_p}


@app.post("/api/menu/{item_id}/toggle_special")
def toggle_special(item_id: int, user: str = Depends(verify_bartender)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT is_special FROM menu WHERE id = ?", (item_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return {"status": "error", "message": "Item not found"}
    new_val = 0 if (row[0] and int(row[0]) == 1) else 1
    cursor.execute("UPDATE menu SET is_special = ? WHERE id = ?", (new_val, item_id))
    conn.commit()
    conn.close()
    return {"status": "success", "is_special": bool(new_val)}


@app.get("/api/inventory/active")
def get_active_inventory():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT item_name, max_pours, current_pours FROM menu "
        "WHERE container = 'Draft' AND current_pours IS NOT NULL"
    )
    kegs = [
        {
            "name": r[0],
            "max": r[1],
            "current": r[2],
            "percent": int((r[2] / r[1]) * 100) if r[1] and r[1] > 0 else 0,
        }
        for r in cursor.fetchall()
    ]
    conn.close()
    return {"kegs": kegs}


# --- Order & Guest Routes ---
@app.post("/order")
def place_order(order: OrderRequest, request: Request):
    conn = get_db()
    cursor = conn.cursor()

    ip = request.headers.get("cf-connecting-ip", request.client.host if request.client else "Local")
    country = request.headers.get("cf-ipcountry", "Local")
    ua = request.headers.get("user-agent", "Unknown Device")
    clean_name = order.guest_name.strip()

    if get_setting(cursor, "bar_status", "open") == "closed":
        conn.close()
        return {"status": "error", "message": "The bar is currently closed!"}

    real_code = get_setting(cursor, "daily_code", "")
    if real_code and (not order.access_code or order.access_code.strip().lower() != real_code.lower()):
        conn.close()
        return {"status": "error", "message": "Invalid access code. Please refresh the page."}

    if ip not in ["Local", "127.0.0.1", "::1", "localhost"]:
        cursor.execute("SELECT ip FROM banned_ips WHERE ip = ?", (ip,))
        if cursor.fetchone():
            conn.close()
            return {"status": "error", "message": "Access denied. Your network has been permanently blocked."}

    # Check if a menu item is already out of stock before placing order
    if order.menu_id is not None:
        cursor.execute("SELECT item_name, current_pours FROM menu WHERE id = ?", (order.menu_id,))
        m_row = cursor.fetchone()
        if not m_row:
            conn.close()
            return {"status": "error", "message": "That drink is no longer on the menu."}
        if m_row[1] is not None and m_row[1] <= 0:
            conn.close()
            return {"status": "error", "message": f"Sorry, {m_row[0]} just sold out!"}

    cursor.execute("SELECT id, is_cut_off FROM guests WHERE device_id = ?", (order.device_id,))
    guest = cursor.fetchone()

    if not guest:
        cursor.execute("SELECT id, is_cut_off FROM guests WHERE LOWER(name) = LOWER(?)", (clean_name,))
        existing_guest = cursor.fetchone()
        if existing_guest:
            guest_id = existing_guest[0]
            cursor.execute(
                "UPDATE guests SET device_id = ?, ip_address = ?, location = ?, user_agent = ? WHERE id = ?",
                (order.device_id, ip, country, ua, guest_id),
            )
            if existing_guest[1]:
                conn.close()
                return {"status": "error", "message": "The bartender has cut you off for the evening. 🚰"}
        else:
            cursor.execute(
                "INSERT INTO guests (name, device_id, is_cut_off, ip_address, location, user_agent) "
                "VALUES (?, ?, 0, ?, ?, ?)",
                (clean_name, order.device_id, ip, country, ua),
            )
            guest_id = cursor.lastrowid
    else:
        guest_id = guest[0]
        cursor.execute(
            "UPDATE guests SET ip_address = ?, location = ?, user_agent = ? WHERE id = ?",
            (ip, country, ua, guest_id),
        )
        if guest[1]:
            conn.close()
            return {"status": "error", "message": "The bartender has cut you off for the evening. 🚰"}

        cursor.execute("SELECT id FROM orders WHERE guest_id = ? AND status = 'pending'", (guest_id,))
        if cursor.fetchone():
            conn.close()
            return {"status": "error", "message": "Slow down! The bartender is still making your last drink."}

    cursor.execute(
        "INSERT INTO orders (guest_id, menu_id, custom_item, order_note, custom_abv, custom_calories) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (guest_id, order.menu_id, order.custom_item, order.order_note, order.custom_abv, order.custom_calories),
    )
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Order received!"}


@app.get("/api/orders/active")
def get_active_orders():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT orders.id, guests.name, COALESCE(orders.custom_item, menu.item_name, 'Removed Drink'),
               strftime('%m/%d %H:%M', orders.timestamp, 'localtime'), orders.timestamp, orders.order_note
        FROM orders
        LEFT JOIN guests ON orders.guest_id = guests.id
        LEFT JOIN menu ON orders.menu_id = menu.id
        WHERE orders.status = 'pending'
        ORDER BY orders.timestamp ASC
        """
    )
    orders = [
        {
            "id": row[0],
            "guest_name": row[1],
            "item_name": row[2],
            "time": row[3],
            "raw_time": row[4].replace(" ", "T") + "Z",
            "note": row[5],
        }
        for row in cursor.fetchall()
    ]
    conn.close()
    return {"orders": orders}


@app.get("/api/orders/history")
def get_order_history(date: Optional[str] = None):
    conn = get_db()
    cursor = conn.cursor()
    base_query = """
        SELECT orders.id, guests.name, COALESCE(orders.custom_item, menu.item_name, 'Removed Drink'),
               strftime('%m/%d %H:%M', orders.timestamp, 'localtime')
        FROM orders
        LEFT JOIN guests ON orders.guest_id = guests.id
        LEFT JOIN menu ON orders.menu_id = menu.id
        WHERE orders.status = 'served'
    """
    if date:
        cursor.execute(
            base_query + " AND DATE(orders.timestamp, 'localtime') = ? ORDER BY orders.timestamp DESC",
            (date,),
        )
    else:
        cursor.execute(base_query + " ORDER BY orders.timestamp DESC LIMIT 15")
    orders = [
        {"id": row[0], "guest_name": row[1], "item_name": row[2], "time": row[3]}
        for row in cursor.fetchall()
    ]
    conn.close()
    return {"orders": orders}


@app.post("/api/orders/{order_id}/serve")
def serve_order(order_id: int, user: str = Depends(verify_bartender)):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT menu_id FROM orders WHERE id = ?", (order_id,))
    row = cursor.fetchone()
    if row and row[0]:
        cursor.execute(
            "UPDATE menu SET current_pours = current_pours - 1 "
            "WHERE id = ? AND current_pours IS NOT NULL AND current_pours > 0",
            (row[0],),
        )

    cursor.execute("UPDATE orders SET status = 'served' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()
    return {"message": "Order served!"}


@app.post("/api/orders/{order_id}/reject")
def reject_order(order_id: int, req: RejectRequest, user: str = Depends(verify_bartender)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE orders SET status = 'rejected', reject_reason = ? WHERE id = ?", (req.reason, order_id))
    conn.commit()
    conn.close()
    return {"message": "Order rejected!"}


@app.delete("/api/orders/{order_id}")
def delete_order(order_id: int, user: str = Depends(verify_bartender)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM orders WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()
    return {"message": "Order deleted"}


@app.get("/api/guests")
def get_guests():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name, device_id, is_cut_off, COALESCE(ip_address, 'Local'), "
        "COALESCE(location, 'Local'), COALESCE(user_agent, 'Unknown'), "
        "COALESCE(aliases, '') "
        "FROM guests ORDER BY name ASC"
    )
    guests = [
        {
            "name": row[0],
            "device_id": row[1],
            "is_cut_off": bool(row[2]),
            "ip": row[3],
            "loc": row[4],
            "ua": row[5],
            "aliases": row[6],
        }
        for row in cursor.fetchall()
    ]
    conn.close()
    return {"guests": guests}


@app.get("/api/guest/{device_id}/history")
def get_guest_history(device_id: str, guest_name: Optional[str] = None, date: Optional[str] = None):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT id, ip_address, location, user_agent FROM guests WHERE device_id = ?", (device_id,))
    guest = cursor.fetchone()

    if not guest:
        if guest_name:
            clean_name = guest_name.strip()
            cursor.execute(
                "SELECT id, ip_address, location, user_agent FROM guests WHERE LOWER(name) = LOWER(?)",
                (clean_name,),
            )
            existing_guest = cursor.fetchone()
            if existing_guest:
                cursor.execute("UPDATE guests SET device_id = ? WHERE id = ?", (device_id, existing_guest[0]))
                conn.commit()
                guest = existing_guest
            else:
                conn.close()
                return {"status": "deleted"}
        else:
            conn.close()
            return {"status": "deleted"}

    guest_id, ip, loc, ua = guest
    ip = ip or "Local"
    loc = loc or "Local"
    ua = ua or "Unknown Device"

    cursor.execute(
        "SELECT COUNT(*) FROM orders WHERE guest_id = ? AND status != 'rejected' "
        "AND DATE(timestamp, 'localtime') = DATE('now', 'localtime')",
        (guest_id,),
    )
    today_count = cursor.fetchone()[0]

    base_query = """
        SELECT orders.id, COALESCE(orders.custom_item, menu.item_name, 'Removed Drink'),
               strftime('%m/%d %H:%M', orders.timestamp, 'localtime'), orders.status, orders.reject_reason
        FROM orders
        JOIN guests ON orders.guest_id = guests.id
        LEFT JOIN menu ON orders.menu_id = menu.id
        WHERE guests.id = ?
    """
    if date:
        cursor.execute(
            base_query + " AND DATE(orders.timestamp, 'localtime') = ? ORDER BY orders.timestamp DESC",
            (guest_id, date),
        )
    else:
        cursor.execute(base_query + " ORDER BY orders.timestamp DESC", (guest_id,))

    history = [
        {"id": row[0], "item_name": row[1], "time": row[2], "status": row[3], "reject_reason": row[4]}
        for row in cursor.fetchall()
    ]
    conn.close()

    return {
        "status": "active",
        "history": history,
        "today_count": today_count,
        "ip": ip,
        "location": loc,
        "user_agent": ua,
    }


@app.put("/api/guest/{device_id}/name")
def update_guest_name(device_id: str, req: NameUpdateRequest):
    clean_name = req.new_name.strip()
    if not clean_name:
        return {"status": "error", "message": "Name cannot be empty"}
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT name, COALESCE(aliases, '') FROM guests WHERE device_id = ?", (device_id,))
    row = cursor.fetchone()
    if row:
        old_name, current_aliases = row[0], row[1]
        alias_list = [a.strip() for a in current_aliases.split(",") if a.strip()]
        if old_name and old_name.lower() != clean_name.lower():
            if old_name.lower() not in [a.lower() for a in alias_list]:
                alias_list.append(old_name)
        alias_list = [a for a in alias_list if a.lower() != clean_name.lower()]
        new_aliases = ", ".join(alias_list)
        cursor.execute("UPDATE guests SET name = ?, aliases = ? WHERE device_id = ?", (clean_name, new_aliases, device_id))
    conn.commit()
    conn.close()
    return {"message": "Guest name updated"}


@app.post("/api/guest/{device_id}/toggle_cutoff")
def toggle_cutoff(device_id: str, user: str = Depends(verify_bartender)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, is_cut_off, ip_address FROM guests WHERE device_id = ?", (device_id,))
    guest = cursor.fetchone()
    if guest:
        guest_id, is_cut, ip = guest
        new_status = 0 if is_cut else 1
        cursor.execute("UPDATE guests SET is_cut_off = ? WHERE id = ?", (new_status, guest_id))
        if ip and ip not in ["Local", "127.0.0.1", "::1", "localhost"]:
            if new_status == 1:
                cursor.execute("INSERT OR IGNORE INTO banned_ips (ip) VALUES (?)", (ip,))
            else:
                cursor.execute("DELETE FROM banned_ips WHERE ip = ?", (ip,))
        conn.commit()
    conn.close()
    return {"message": "Cutoff status updated"}


@app.delete("/api/guest/{device_id}")
def delete_guest(device_id: str, user: str = Depends(verify_bartender)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM guests WHERE device_id = ?", (device_id,))
    guest = cursor.fetchone()
    if guest:
        guest_id = guest[0]
        cursor.execute("DELETE FROM orders WHERE guest_id = ?", (guest_id,))
        cursor.execute("DELETE FROM guests WHERE id = ?", (guest_id,))
        conn.commit()
    conn.close()
    return {"message": "Guest deleted"}


# --- Stats & Leaderboards ---
@app.get("/api/stats")
def get_stats():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT guests.name, COUNT(orders.id) as count
        FROM orders LEFT JOIN guests ON orders.guest_id = guests.id
        WHERE orders.status != 'rejected' AND DATE(orders.timestamp, 'localtime') = DATE('now', 'localtime')
        GROUP BY guests.id ORDER BY count DESC LIMIT 5
        """
    )
    tonight = [{"name": row[0], "count": row[1]} for row in cursor.fetchall()]

    cursor.execute(
        """
        SELECT guests.name, COUNT(orders.id) as count
        FROM orders LEFT JOIN guests ON orders.guest_id = guests.id
        WHERE orders.status != 'rejected'
        GROUP BY guests.id ORDER BY count DESC LIMIT 5
        """
    )
    all_time = [{"name": row[0], "count": row[1]} for row in cursor.fetchall()]

    cursor.execute(
        """
        SELECT COALESCE(orders.custom_item, menu.item_name, 'Removed Drink') as drink_name,
               COUNT(orders.id) as count
        FROM orders LEFT JOIN menu ON orders.menu_id = menu.id
        WHERE orders.status != 'rejected'
        GROUP BY drink_name ORDER BY count DESC LIMIT 5
        """
    )
    popular = [{"name": row[0], "count": row[1]} for row in cursor.fetchall()]

    cursor.execute("SELECT name FROM guests WHERE is_cut_off = 1")
    cut_offs = [r[0] for r in cursor.fetchall()]

    cursor.execute(
        """
        SELECT guests.name, orders.reject_reason
        FROM orders JOIN guests ON orders.guest_id = guests.id
        WHERE orders.status = 'rejected' AND orders.reject_reason IS NOT NULL AND orders.reject_reason != ''
          AND DATE(orders.timestamp, 'localtime') = DATE('now', 'localtime')
        """
    )
    rejects = [{"name": r[0], "reason": r[1]} for r in cursor.fetchall()]

    cursor.execute(
        """
        SELECT guests.name, COALESCE(orders.custom_abv, menu.abv),
               COALESCE(orders.custom_calories, menu.calories),
               strftime('%Y-%m-%d %H:%M:%S', orders.timestamp, 'localtime'), orders.custom_item
        FROM orders JOIN guests ON orders.guest_id = guests.id
        LEFT JOIN menu ON orders.menu_id = menu.id
        WHERE orders.status != 'rejected' AND DATE(orders.timestamp, 'localtime') = DATE('now', 'localtime')
        ORDER BY orders.timestamp ASC
        """
    )
    tonight_orders = cursor.fetchall()

    total_cals = 0
    total_drinks = len(tonight_orders)
    guest_stats = {}

    for name, abv_str, cal_str, ts, custom in tonight_orders:
        if name not in guest_stats:
            guest_stats[name] = {
                "drinks": 0,
                "cals": 0,
                "abv_total": 0.0,
                "first_ts": ts,
                "last_ts": ts,
                "customs": 0,
            }

        guest_stats[name]["drinks"] += 1
        guest_stats[name]["last_ts"] = ts
        if custom:
            guest_stats[name]["customs"] += 1

        if cal_str:
            cal_match = re.search(r"\d+", str(cal_str))
            if cal_match:
                c = int(cal_match.group())
                total_cals += c
                guest_stats[name]["cals"] += c
        if abv_str:
            abv_match = re.search(r"[\d\.]+", str(abv_str))
            if abv_match:
                guest_stats[name]["abv_total"] += float(abv_match.group())

    superlatives = []
    if guest_stats:
        early_bird = list(guest_stats.keys())[0]
        superlatives.append(
            {"emoji": "🌅", "title": "The Early Bird", "name": early_bird, "desc": "First drink of the night"}
        )

        heavyweight = max(guest_stats.items(), key=lambda x: x[1]["abv_total"])
        if heavyweight[1]["abv_total"] > 0:
            superlatives.append(
                {
                    "emoji": "🥊",
                    "title": "The Heavyweight",
                    "name": heavyweight[0],
                    "desc": f"Highest ABV total ({heavyweight[1]['abv_total']:.1f}%)",
                }
            )

        wildcard = max(guest_stats.items(), key=lambda x: x[1]["customs"])
        if wildcard[1]["customs"] > 0:
            superlatives.append(
                {
                    "emoji": "🃏",
                    "title": "The Wildcard",
                    "name": wildcard[0],
                    "desc": f"Most 'Off-Menu' requests ({wildcard[1]['customs']})",
                }
            )

        cal_crusher = max(guest_stats.items(), key=lambda x: x[1]["cals"])
        if cal_crusher[1]["cals"] > 0:
            superlatives.append(
                {
                    "emoji": "🍔",
                    "title": "Calorie Crusher",
                    "name": cal_crusher[0],
                    "desc": f"Most liquid calories ({cal_crusher[1]['cals']} cal)",
                }
            )

        marathoner = None
        max_duration = -1
        for g, data in guest_stats.items():
            try:
                t1 = datetime.strptime(data["first_ts"], "%Y-%m-%d %H:%M:%S")
                t2 = datetime.strptime(data["last_ts"], "%Y-%m-%d %H:%M:%S")
                dur = (t2 - t1).total_seconds()
                if dur > max_duration and data["drinks"] > 1:
                    max_duration = dur
                    marathoner = g
            except Exception:
                pass

        if marathoner and max_duration > 900:
            superlatives.append(
                {"emoji": "🏃", "title": "The Marathoner", "name": marathoner, "desc": "Longest drinking duration"}
            )

        night_owl = list(guest_stats.keys())[-1]
        if len(guest_stats) > 1 and night_owl != early_bird:
            superlatives.append(
                {"emoji": "🦉", "title": "The Night Owl", "name": night_owl, "desc": "Most recent order"}
            )

    conn.close()
    return {
        "tonight": tonight,
        "all_time": all_time,
        "popular": popular,
        "liquid_diet": {"total_cals": total_cals, "total_drinks": total_drinks},
        "superlatives": superlatives,
        "wall_of_shame": {"cut_offs": cut_offs, "rejects": rejects},
    }
