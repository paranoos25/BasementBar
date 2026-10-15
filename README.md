# 🍻 Basement Bar POS & Jumbotron

A local-first, low-maintenance Point of Sale and Digital Signage system designed for home bars, tailgates, and sports caves. Built with Python (FastAPI), SQLite, and Tailwind CSS.

## 🌟 Features

*   **📱 Guest Mobile Ordering:** Guests simply scan a QR code or visit the local URL on their phones to view the menu, add custom drink requests, leave bartender notes, and track their personal order history.
*   **🍺 Bartender Control Tablet:** A dedicated interface to manage the active queue, edit the menu, cut off rowdy guests, and send rejection notes (e.g., "Out of stock").
*   **📺 Rotating Jumbotron Dashboard:** Designed for a vertical monitor, the dashboard automatically cycles every 15 seconds through three fullscreen views:
    *   *The Bar Board:* Shows the active queue, "Tonight's Tab," and all-time heavy hitters.
    *   *The Liquid Diet Tracker:* A visually filling pint glass that tracks the room's total calories (and calculates the equivalent in chicken wings).
    *   *Game Day Awards:* Dynamically calculates superlatives based on timestamp math (The Early Bird, The Marathoner, The Wildcard, etc.).
*   **🚨 Smart Light Webhook Integration:** Hit the "GOAL" or "TOUCHDOWN" buttons on the bartender tablet to flash a massive animated graphic on the jumbotron, play a stadium horn over the speakers, and fire outbound webhooks to flash your Philips Hue / Govee lights.
*   **🔉 Native Web Audio Synthesizer:** No MP3s required. Generates pure mathematical audio for order notification chimes, arena buzzers, and retro arcade sounds.

## 🚀 Quick Start

**1. Install Dependencies**
\`\`\`bash
pip install -r requirements.txt
\`\`\`

**2. Run the Server**
\`\`\`bash
uvicorn main:app --host 0.0.0.0 --port 80
\`\`\`

**3. Access the Interfaces**
*   **Guest Menu:** \`http://<YOUR_IP>/\`
*   **Bartender Control:** \`http://<YOUR_IP>/bartender\`
*   **Jumbotron Display:** \`http://<YOUR_IP>/dashboard\`

## 🛠️ Architecture
The system intentionally avoids external cloud dependencies. It relies on a single lightweight FastAPI backend and an auto-generating local SQLite database (`bar_pos.db`). The frontend is pure HTML/JS using Tailwind CDN for zero-build-step styling.
