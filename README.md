# 🍻 Basement Bar POS System

A lightning-fast, highly responsive Point-of-Sale and digital menu board system built with **FastAPI**, **SQLite (WAL Mode)**, and **Tailwind CSS**, designed specifically for home bars, house parties, and game days.

---

## 🚀 Key Features

* **Multi-View Display Architecture**: Tailored interfaces for mobile guests, wall dashboards, vertical TV boards, and bartender administration.
* **Dynamic Team & Holiday Theming**: Instantly switch the entire bar's aesthetic between **Default Dark**, **🎄 Christmas (with falling snow)**, **🏈 Buffalo Bills**, **🏒 Buffalo Sabres**, and **⚾ Toronto Blue Jays**.
* **High-Concurrency SQLite WAL Mode**: Configured with Write-Ahead Logging (WAL) and busy timeouts to support simultaneous polling from guest phones, display boards, and the bartender tablet without lock errors.
* **Live Inventory & Keg Tracking**: Real-time pour counters, draft percentage progress bars, quick stock adjustments (`-1` / `+1`), and sold-out protection.
* **Automated Game Day Scheduler**: Background task integrating with ESPN APIs to automatically detect Buffalo Bills and Sabres game days, manage Cloudflare tunnels, and trigger team themes.
* **Party Stats & Superlatives**: Automated game-day leaderboards ("The Early Bird", "The Heavyweight", "The Wildcard", "Calorie Crusher", "The Marathoner") and a Liquid Diet calorie counter with party mode tank overflow.
* **Wall of Shame**: Live scrolling marquee tracking cut-off guests and rejected orders with custom reasons.

---

## 🖥️ Application Routes & Screens

| Route | Interface File | Description |
| :--- | :--- | :--- |
| `GET /` | `menu.html` | Mobile-optimized guest menu for ordering drinks, viewing ABV/calories, and entering daily passcodes. |
| `GET /dashboard` | `dashboard.html` | Main big-screen TV display with a 15-second rotating multi-view carousel (Orders queue, Tonight's tab, Leaderboards, Liquid Diet, Awards, and QR/Passcode screen). |
| `GET /board` | `board.html` | Vertical digital menu board with categorized drinks and real-time pour levels. |
| `GET /bartender` | `bartender.html` | Password-protected admin command center (HTTP Basic Auth) for order management, inventory control, theme selection, and passcode rotation. |

---

## 🛠️ Tech Stack

* **Backend**: Python 3, FastAPI, Uvicorn, SQLite
* **Frontend**: HTML5, Tailwind CSS (via CDN), Vanilla JavaScript
* **Networking**: Cloudflare Tunnels (`cloudflared`) for secure remote guest access
* **Deployment**: Debian Linux via Systemd service (`barpos.service`)

---

## ⚙️ Quick Setup & Running

1. **Clone & Navigate**:
   ```bash
   cd /home/rob/BasementBarApp
   
Install Dependencies:

pip install -r requirements.txt

Run the Server:

uvicorn main:app --host 0.0.0.0 --port 80

Systemd Service Management:

sudo systemctl restart barpos
sudo systemctl status barpos

