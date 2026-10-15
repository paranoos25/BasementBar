from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import sqlite3
import re
import time
from datetime import datetime
import urllib.request
import os
from dotenv import load_dotenv

# Load the hidden environment variables
load_dotenv()

app = FastAPI()

active_event = {"id": None, "type": None, "expires": 0}

class OrderRequest(BaseModel):
    guest_name: str
    device_id: str
    menu_id: Optional[int] = None
    custom_item: Optional[str] = None
    order_note: Optional[str] = None
    custom_abv: Optional[str] = None
    custom_calories: Optional[str] = None

class MenuItemRequest(BaseModel):
    item_name: str
    category: str
    abv: Optional[str] = None
    calories: Optional[str] = None
    notes: Optional[str] = None

class RejectRequest(BaseModel):
    reason: Optional[str] = None

class NameUpdateRequest(BaseModel):
    new_name: str

def init_db():
    conn = sqlite3.connect("bar_pos.db")
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS guests (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, device_id TEXT UNIQUE)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS menu (id INTEGER PRIMARY KEY AUTOINCREMENT, item_name TEXT, category TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, guest_id INTEGER, menu_id INTEGER, status TEXT DEFAULT 'pending', timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(guest_id) REFERENCES guests(id), FOREIGN KEY(menu_id) REFERENCES menu(id))''')
    
    try: cursor.execute("ALTER TABLE orders ADD COLUMN custom_item TEXT")
    except sqlite3.OperationalError: pass 
    try: cursor.execute("ALTER TABLE orders ADD COLUMN reject_reason TEXT")
    except sqlite3.OperationalError: pass 
    try: cursor.execute("ALTER TABLE guests ADD COLUMN is_cut_off INTEGER DEFAULT 0")
    except sqlite3.OperationalError: pass 
    try: cursor.execute("ALTER TABLE menu ADD COLUMN abv TEXT")
    except sqlite3.OperationalError: pass 
    try: cursor.execute("ALTER TABLE menu ADD COLUMN calories TEXT")
    except sqlite3.OperationalError: pass 
    try: cursor.execute("ALTER TABLE menu ADD COLUMN notes TEXT")
    except sqlite3.OperationalError: pass 
    try: cursor.execute("ALTER TABLE orders ADD COLUMN order_note TEXT")
    except sqlite3.OperationalError: pass 
    try: cursor.execute("ALTER TABLE orders ADD COLUMN custom_abv TEXT")
    except sqlite3.OperationalError: pass 
    try: cursor.execute("ALTER TABLE orders ADD COLUMN custom_calories TEXT")
    except sqlite3.OperationalError: pass 
        
    cursor.execute("SELECT COUNT(*) FROM menu")
    if cursor.fetchone()[0] == 0:
        default_drinks = [("Labatt Blue", "Beer"), ("Southern Tier IPA", "Beer"), ("Fattey Beer House Lager", "Beer"), ("Vodka Cranberry", "Cocktail"), ("Old Fashioned", "Cocktail")]
        cursor.executemany("INSERT INTO menu (item_name, category) VALUES (?, ?)", default_drinks)
        
    conn.commit()
    conn.close()

init_db()

@app.get("/")
def serve_phone_menu(): return FileResponse("menu.html")

@app.get("/dashboard")
def serve_dashboard(): return FileResponse("dashboard.html")

@app.get("/bartender")
def serve_bartender(): return FileResponse("bartender.html")

@app.get("/api/event/trigger/{event_type}")
def trigger_event(event_type: str):
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

@app.get("/api/event/current")
def get_current_event():
    if time.time() < active_event["expires"]:
        return {"event": active_event["type"], "id": active_event["id"]}
    return {"event": None, "id": None}

@app.get("/menu")
def get_menu():
    conn = sqlite3.connect("bar_pos.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, item_name, category, abv, calories, notes FROM menu")
    drinks = [{"id": row[0], "item_name": row[1], "category": row[2], "abv": row[3], "calories": row[4], "notes": row[5]} for row in cursor.fetchall()]
    conn.close()
    return {"menu": drinks}

@app.post("/api/menu")
def add_menu_item(item: MenuItemRequest):
    conn = sqlite3.connect("bar_pos.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO menu (item_name, category, abv, calories, notes) VALUES (?, ?, ?, ?, ?)", 
                   (item.item_name, item.category, item.abv, item.calories, item.notes))
    conn.commit()
    conn.close()
    return {"message": "Menu item added"}

@app.put("/api/menu/{item_id}")
def edit_menu_item(item_id: int, item: MenuItemRequest):
    conn = sqlite3.connect("bar_pos.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE menu SET item_name=?, category=?, abv=?, calories=?, notes=? WHERE id=?", 
                   (item.item_name, item.category, item.abv, item.calories, item.notes, item_id))
    conn.commit()
    conn.close()
    return {"message": "Menu item updated"}

@app.delete("/api/menu/{item_id}")
def delete_menu_item(item_id: int):
    conn = sqlite3.connect("bar_pos.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM menu WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return {"message": "Menu item deleted"}

@app.post("/order")
def place_order(order: OrderRequest):
    conn = sqlite3.connect("bar_pos.db")
    cursor = conn.cursor()
    
    cursor.execute("SELECT id, is_cut_off FROM guests WHERE device_id = ?", (order.device_id,))
    guest = cursor.fetchone()
    
    if not guest:
        cursor.execute("INSERT INTO guests (name, device_id, is_cut_off) VALUES (?, ?, 0)", (order.guest_name, order.device_id))
        guest_id = cursor.lastrowid
    else:
        guest_id = guest[0]
        if guest[1]:
            conn.close()
            return {"status": "error", "message": "The bartender has cut you off for the evening. 🚰"}
            
        cursor.execute("SELECT id FROM orders WHERE guest_id = ? AND status = 'pending'", (guest_id,))
        if cursor.fetchone():
            conn.close()
            return {"status": "error", "message": "Slow down! The bartender is still making your last drink."}
            
    cursor.execute("INSERT INTO orders (guest_id, menu_id, custom_item, order_note, custom_abv, custom_calories) VALUES (?, ?, ?, ?, ?, ?)", 
                   (guest_id, order.menu_id, order.custom_item, order.order_note, order.custom_abv, order.custom_calories))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Order received!"}

@app.get("/api/orders/active")
def get_active_orders():
    conn = sqlite3.connect("bar_pos.db")
    cursor = conn.cursor()
    cursor.execute('''
        SELECT orders.id, guests.name, COALESCE(orders.custom_item, menu.item_name, 'Removed Drink'), strftime('%m/%d %H:%M', orders.timestamp, 'localtime'), orders.timestamp, orders.order_note
        FROM orders LEFT JOIN guests ON orders.guest_id = guests.id LEFT JOIN menu ON orders.menu_id = menu.id 
        WHERE orders.status = 'pending' ORDER BY orders.timestamp ASC
    ''')
    orders = [{"id": row[0], "guest_name": row[1], "item_name": row[2], "time": row[3], "raw_time": row[4].replace(" ", "T") + "Z", "note": row[5]} for row in cursor.fetchall()]
    conn.close()
    return {"orders": orders}

@app.post("/api/orders/{order_id}/serve")
def serve_order(order_id: int):
    conn = sqlite3.connect("bar_pos.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE orders SET status = 'served' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()
    return {"message": "Order served!"}

@app.post("/api/orders/{order_id}/reject")
def reject_order(order_id: int, req: RejectRequest):
    conn = sqlite3.connect("bar_pos.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE orders SET status = 'rejected', reject_reason = ? WHERE id = ?", (req.reason, order_id))
    conn.commit()
    conn.close()
    return {"message": "Order rejected!"}

@app.delete("/api/orders/{order_id}")
def delete_order(order_id: int):
    conn = sqlite3.connect("bar_pos.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM orders WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()
    return {"message": "Order deleted"}

@app.get("/api/orders/history")
def get_order_history(date: Optional[str] = None):
    conn = sqlite3.connect("bar_pos.db")
    cursor = conn.cursor()
    base_query = '''
        SELECT orders.id, guests.name, COALESCE(orders.custom_item, menu.item_name, 'Removed Drink'), strftime('%m/%d %H:%M', orders.timestamp, 'localtime') 
        FROM orders LEFT JOIN guests ON orders.guest_id = guests.id LEFT JOIN menu ON orders.menu_id = menu.id 
        WHERE orders.status = 'served'
    '''
    if date:
        cursor.execute(base_query + " AND DATE(orders.timestamp, 'localtime') = ? ORDER BY orders.timestamp DESC", (date,))
    else:
        cursor.execute(base_query + " ORDER BY orders.timestamp DESC LIMIT 15")
    orders = [{"id": row[0], "guest_name": row[1], "item_name": row[2], "time": row[3]} for row in cursor.fetchall()]
    conn.close()
    return {"orders": orders}

@app.get("/api/guests")
def get_guests():
    conn = sqlite3.connect("bar_pos.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name, device_id, is_cut_off FROM guests ORDER BY name ASC")
    guests = [{"name": row[0], "device_id": row[1], "is_cut_off": bool(row[2])} for row in cursor.fetchall()]
    conn.close()
    return {"guests": guests}

@app.put("/api/guest/{device_id}/name")
def update_guest_name(device_id: str, req: NameUpdateRequest):
    conn = sqlite3.connect("bar_pos.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE guests SET name = ? WHERE device_id = ?", (req.new_name, device_id))
    conn.commit()
    conn.close()
    return {"message": "Guest name updated"}

@app.post("/api/guest/{device_id}/toggle_cutoff")
def toggle_cutoff(device_id: str):
    conn = sqlite3.connect("bar_pos.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_cut_off FROM guests WHERE device_id = ?", (device_id,))
    guest = cursor.fetchone()
    if guest:
        new_status = 0 if guest[0] else 1
        cursor.execute("UPDATE guests SET is_cut_off = ? WHERE device_id = ?", (new_status, device_id))
        conn.commit()
    conn.close()
    return {"message": "Cutoff status updated"}

@app.get("/api/guest/{device_id}/history")
def get_guest_history(device_id: str, date: Optional[str] = None):
    conn = sqlite3.connect("bar_pos.db")
    cursor = conn.cursor()
    
    cursor.execute("SELECT id FROM guests WHERE device_id = ?", (device_id,))
    guest = cursor.fetchone()
    if not guest:
        conn.close()
        return {"status": "deleted"}
        
    guest_id = guest[0]
    
    cursor.execute("SELECT COUNT(*) FROM orders WHERE guest_id = ? AND status != 'rejected' AND DATE(timestamp, 'localtime') = DATE('now', 'localtime')", (guest_id,))
    today_count = cursor.fetchone()[0]
        
    base_query = '''
        SELECT orders.id, COALESCE(orders.custom_item, menu.item_name, 'Removed Drink'), strftime('%m/%d %H:%M', orders.timestamp, 'localtime'), orders.status, orders.reject_reason
        FROM orders JOIN guests ON orders.guest_id = guests.id LEFT JOIN menu ON orders.menu_id = menu.id 
        WHERE guests.device_id = ?
    '''
    if date:
        cursor.execute(base_query + " AND DATE(orders.timestamp, 'localtime') = ? ORDER BY orders.timestamp DESC", (device_id, date))
    else:
        cursor.execute(base_query + " ORDER BY orders.timestamp DESC", (device_id,))
        
    history = [{"id": row[0], "item_name": row[1], "time": row[2], "status": row[3], "reject_reason": row[4]} for row in cursor.fetchall()]
    conn.close()
    return {"status": "active", "history": history, "today_count": today_count}

@app.delete("/api/guest/{device_id}")
def delete_guest(device_id: str):
    conn = sqlite3.connect("bar_pos.db")
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

@app.get("/api/stats")
def get_stats():
    conn = sqlite3.connect("bar_pos.db")
    cursor = conn.cursor()
    
    cursor.execute('''SELECT guests.name, COUNT(orders.id) as count FROM orders LEFT JOIN guests ON orders.guest_id = guests.id WHERE orders.status != 'rejected' AND DATE(orders.timestamp, 'localtime') = DATE('now', 'localtime') GROUP BY guests.id ORDER BY count DESC LIMIT 5''')
    tonight = [{"name": row[0], "count": row[1]} for row in cursor.fetchall()]

    cursor.execute('''SELECT guests.name, COUNT(orders.id) as count FROM orders LEFT JOIN guests ON orders.guest_id = guests.id WHERE orders.status != 'rejected' GROUP BY guests.id ORDER BY count DESC LIMIT 5''')
    all_time = [{"name": row[0], "count": row[1]} for row in cursor.fetchall()]
    
    cursor.execute('''SELECT COALESCE(orders.custom_item, menu.item_name, 'Removed Drink') as drink_name, COUNT(orders.id) as count FROM orders LEFT JOIN menu ON orders.menu_id = menu.id WHERE orders.status != 'rejected' GROUP BY drink_name ORDER BY count DESC LIMIT 5''')
    popular = [{"name": row[0], "count": row[1]} for row in cursor.fetchall()]
    
    cursor.execute("SELECT name FROM guests WHERE is_cut_off = 1")
    cut_offs = [r[0] for r in cursor.fetchall()]
    
    cursor.execute("SELECT guests.name, orders.reject_reason FROM orders JOIN guests ON orders.guest_id = guests.id WHERE orders.status = 'rejected' AND orders.reject_reason IS NOT NULL AND orders.reject_reason != '' AND DATE(orders.timestamp, 'localtime') = DATE('now', 'localtime')")
    rejects = [{"name": r[0], "reason": r[1]} for r in cursor.fetchall()]
    
    cursor.execute('''
        SELECT guests.name, COALESCE(orders.custom_abv, menu.abv), COALESCE(orders.custom_calories, menu.calories), strftime('%Y-%m-%d %H:%M:%S', orders.timestamp, 'localtime'), orders.custom_item 
        FROM orders JOIN guests ON orders.guest_id = guests.id LEFT JOIN menu ON orders.menu_id = menu.id 
        WHERE orders.status != 'rejected' AND DATE(orders.timestamp, 'localtime') = DATE('now', 'localtime')
        ORDER BY orders.timestamp ASC
    ''')
    tonight_orders = cursor.fetchall()
    
    total_cals = 0
    total_drinks = len(tonight_orders)
    guest_stats = {}
    
    for name, abv_str, cal_str, ts, custom in tonight_orders:
        if name not in guest_stats:
            guest_stats[name] = {'drinks': 0, 'cals': 0, 'abv_total': 0.0, 'first_ts': ts, 'last_ts': ts, 'customs': 0}
        
        guest_stats[name]['drinks'] += 1
        guest_stats[name]['last_ts'] = ts
        if custom: guest_stats[name]['customs'] += 1
            
        if cal_str:
            cal_match = re.search(r'\d+', str(cal_str))
            if cal_match:
                c = int(cal_match.group())
                total_cals += c
                guest_stats[name]['cals'] += c
        if abv_str:
            abv_match = re.search(r'[\d\.]+', str(abv_str))
            if abv_match:
                guest_stats[name]['abv_total'] += float(abv_match.group())
                
    superlatives = []
    if guest_stats:
        early_bird = list(guest_stats.keys())[0]
        superlatives.append({"emoji": "🌅", "title": "The Early Bird", "name": early_bird, "desc": "First drink of the night"})
        
        heavyweight = max(guest_stats.items(), key=lambda x: x[1]['abv_total'])
        if heavyweight[1]['abv_total'] > 0:
            superlatives.append({"emoji": "🥊", "title": "The Heavyweight", "name": heavyweight[0], "desc": f"Highest ABV total ({heavyweight[1]['abv_total']:.1f}%)"})
        
        wildcard = max(guest_stats.items(), key=lambda x: x[1]['customs'])
        if wildcard[1]['customs'] > 0:
            superlatives.append({"emoji": "🃏", "title": "The Wildcard", "name": wildcard[0], "desc": f"Most 'Off-Menu' requests ({wildcard[1]['customs']})"})
            
        cal_crusher = max(guest_stats.items(), key=lambda x: x[1]['cals'])
        if cal_crusher[1]['cals'] > 0:
            superlatives.append({"emoji": "🍔", "title": "Calorie Crusher", "name": cal_crusher[0], "desc": f"Most liquid calories ({cal_crusher[1]['cals']} cal)"})
        
        marathoner = None
        max_duration = -1
        for g, data in guest_stats.items():
            try:
                t1 = datetime.strptime(data['first_ts'], "%Y-%m-%d %H:%M:%S")
                t2 = datetime.strptime(data['last_ts'], "%Y-%m-%d %H:%M:%S")
                dur = (t2 - t1).total_seconds()
                if dur > max_duration and data['drinks'] > 1:
                    max_duration = dur
                    marathoner = g
            except: pass
        
        if marathoner and max_duration > 900:
            superlatives.append({"emoji": "🏃", "title": "The Marathoner", "name": marathoner, "desc": "Longest drinking duration"})
            
        night_owl = list(guest_stats.keys())[-1]
        if len(guest_stats) > 1 and night_owl != early_bird:
            superlatives.append({"emoji": "🦉", "title": "The Night Owl", "name": night_owl, "desc": "Most recent order"})

    conn.close()
    return {
        "tonight": tonight, "all_time": all_time, "popular": popular,
        "liquid_diet": {"total_cals": total_cals, "total_drinks": total_drinks},
        "superlatives": superlatives,
        "wall_of_shame": {"cut_offs": cut_offs, "rejects": rejects}
    }
