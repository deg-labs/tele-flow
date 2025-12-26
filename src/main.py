import os
import re
import asyncio
import aiohttp # REFACTOR: Use aiohttp for async requests
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from dotenv import load_dotenv
from telethon import TelegramClient, events

# --- Setup Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Load Environment Variables & Config ---
load_dotenv()
API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
SESSION_NAME = os.getenv('SESSION_NAME', 'my_session')
CHANNEL_USERNAME = os.getenv('CHANNEL_USERNAME')
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL')

try:
    MESSAGE_HISTORY_LIMIT = int(os.getenv('MESSAGE_HISTORY_LIMIT', 50))
    BASE_THRESHOLD_USD_PER_SEC = float(os.getenv('BASE_THRESHOLD_USD_PER_SEC', 20000))
    ANALYSIS_WINDOW_SECONDS = int(os.getenv('ANALYSIS_WINDOW_SECONDS', 300))
    MONITORING_INTERVAL_SECONDS = int(os.getenv('MONITORING_INTERVAL_SECONDS', 10))
    ACCELERATION_THRESHOLD = float(os.getenv('ACCELERATION_THRESHOLD', 3.0))
    DOMINANCE_THRESHOLD = float(os.getenv('DOMINANCE_THRESHOLD', 0.75))
    BIAS_THRESHOLD = float(os.getenv('BIAS_THRESHOLD', 0.85))
    SUMMARY_COOLDOWN_SECONDS = int(os.getenv('SUMMARY_COOLDOWN_SECONDS', 60))
    ACTIVE_IDLE_TRANSITION_GRACE_PERIOD_SECONDS = int(os.getenv('ACTIVE_IDLE_TRANSITION_GRACE_PERIOD_SECONDS', 30))
    SINGLE_EVENT_NOTIFICATION_THRESHOLD = float(os.getenv('SINGLE_EVENT_NOTIFICATION_THRESHOLD', 0))
except (ValueError, TypeError) as e:
    logging.error(f"Invalid configuration value: {e}. Please check your .env file.")
    exit(1)

# --- Database Setup ---
DB_FILE = "bot_data.db"
LIQUIDATION_HISTORY_LIMIT = 200

# REFACTOR: Use a single connection passed around, enable WAL mode
def init_db(conn: sqlite3.Connection):
    """Initializes the database and creates tables if they don't exist."""
    with conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS liquidations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP NOT NULL,
                ticker TEXT NOT NULL,
                direction TEXT NOT NULL,
                amount REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notification_cooldowns (
                key TEXT PRIMARY KEY,
                notified_at TIMESTAMP NOT NULL
            )
        """)
    logging.info("Database initialized successfully with WAL mode.")

# --- DB Helper Functions (modified for persistent connection) ---
def _to_datetime(ts_val):
    if isinstance(ts_val, str):
        try:
            return datetime.fromisoformat(ts_val.replace(" ", "T"))
        except ValueError:
            return None
    return ts_val

def add_liquidation(conn: sqlite3.Connection, timestamp, ticker, direction, amount):
    """Adds a liquidation event to the DB and trims old records."""
    with conn:
        conn.execute(
            "INSERT INTO liquidations (timestamp, ticker, direction, amount) VALUES (?, ?, ?, ?)",
            (timestamp, ticker, direction, amount)
        )
        conn.execute(f"""
            DELETE FROM liquidations
            WHERE id NOT IN (
                SELECT id FROM liquidations
                ORDER BY timestamp DESC, id DESC
                LIMIT {LIQUIDATION_HISTORY_LIMIT}
            )
        """)

def get_liquidations_in_timeframe(conn: sqlite3.Connection, start_time, end_time=None):
    """Fetches liquidations from the DB within a given timeframe."""
    if end_time is None:
        end_time = datetime.now(timezone.utc)
    
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    cursor.execute(
        "SELECT timestamp, ticker, direction, amount FROM liquidations WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp ASC",
        (start_time, end_time)
    )
    rows = cursor.fetchall()
    
    results = []
    for r in rows:
        ts = _to_datetime(r["timestamp"])
        if ts:
            results.append({"timestamp": ts, "ticker": r["ticker"], "direction": r["direction"], "amount": r["amount"]})
    return results

# --- Liquidation Analysis Functions ---
def _parse_amount(amount_str):
    if not amount_str: return 0.0
    amount_str = amount_str.replace('$', '').replace(',', '')
    multiplier = 1.0
    if amount_str.endswith('k'):
        multiplier = 1_000.0
        amount_str = amount_str[:-1]
    elif amount_str.endswith('M'):
        multiplier = 1_000_000.0
        amount_str = amount_str[:-1]
    try:
        return float(amount_str) * multiplier
    except ValueError:
        return 0.0

def parse_liquidation_message(message_text):
    if not message_text: return None, None, None
    match = re.search(r"#(?:\w+:)?(\w+)\s+(Long|Short)\s+Liquidation:\s*(\$[\d,]+\.?\d*[kM]?)", message_text, re.IGNORECASE)
    if match:
        ticker, direction, amount_str = match.groups()
        return ticker.upper(), direction.capitalize(), _parse_amount(amount_str)
    return None, None, None

def calculate_liquidation_metrics(events, window_seconds):
    total_amount = sum(e['amount'] for e in events)
    total_count = len(events)
    speed_usd_per_sec = total_amount / window_seconds if window_seconds > 0 else 0
    ticker_amounts = defaultdict(float)
    for event in events:
        ticker_amounts[event['ticker']] += event['amount']
    dominance_info = {t: a / total_amount for t, a in ticker_amounts.items()} if total_amount > 0 else {}
    long_amount = sum(e['amount'] for e in events if e['direction'] == 'Long')
    total_directional_amount = sum(e['amount'] for e in events if e['direction'] in ['Long', 'Short'])
    long_bias = long_amount / total_directional_amount if total_directional_amount > 0 else 0
    return {
        "speed_usd_per_sec": speed_usd_per_sec,
        "total_amount": total_amount,
        "total_count": total_count,
        "avg_event_amount": total_amount / total_count if total_count > 0 else 0,
        "dominance_info": dominance_info,
        "long_bias": long_bias,
        "short_bias": 1.0 - long_bias if total_directional_amount > 0 else 0,
    }

def calculate_acceleration(current_speed, prev_speed):
    if prev_speed > 500: # Avoid extreme acceleration on low baseline
        return current_speed / prev_speed
    elif current_speed > 0:
        return ACCELERATION_THRESHOLD # Cap acceleration for display
    return 1.0

# --- Bot State Manager ---
class LiquidationMonitor:
    def __init__(self):
        self.state = "IDLE"
        self.active_since = None
        self.last_summary_sent = None
        self.last_known_speed = 0.0
        self.prev_known_speed = 0.0
        self.last_above_threshold_time = None # REFACTOR: Renamed for clarity
        self.active_period_events = []

    async def _send_single_event_notification(self, event):
        if not DISCORD_WEBHOOK_URL: return
        direction_emoji = "🟢" if event["direction"] == "Short" else "🔴"
        title = f"{direction_emoji} Large {event['direction']} Liquidation"
        description_lines = [
            f"**Ticker:** `{event['ticker']}`",
            f"**Amount:** `${event['amount']:,.2f}`",
            f"**Time:** `{event['timestamp'].isoformat()}`",
        ]
        embed = {"title": title, "description": "\n".join(description_lines), "color": 5763719 if event["direction"] == "Short" else 15548997}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}) as response:
                    response.raise_for_status()
                    logging.info("Successfully sent single-event notification to Discord.")
            except aiohttp.ClientError as e:
                logging.error(f"Failed to send Discord single-event notification: {e}")

    async def _send_summary_notification(self, metrics, acceleration, prev_speed):
        if not DISCORD_WEBHOOK_URL: return
        title = "⚠ High Liquidation Activity ⚠"
        if acceleration >= ACCELERATION_THRESHOLD and metrics["speed_usd_per_sec"] >= BASE_THRESHOLD_USD_PER_SEC:
            title = "🚨 CRITICAL LIQUIDATION SPIKE 🚨"

        description_lines = [
            f"**Speed:** `{metrics['speed_usd_per_sec']:.2f} USD/sec`",
            f"**Acceleration:** `x{acceleration:.2f}` (Prev Window: {prev_speed:.2f} USD/sec)",
            f"**Count:** `{metrics['total_count']} events`",
            f"**Total Amount:** `${metrics['total_amount']:,.2f}`",
        ]
        if metrics['total_count'] > 0:
            description_lines.append(f"**Avg per event:** `${metrics['avg_event_amount']:,.2f}`")

        if metrics["dominance_info"]:
            sorted_dominance = sorted(metrics["dominance_info"].items(), key=lambda item: item[1], reverse=True)[:3]
            dominance_str = ", ".join([f"{t}: {r:.1%}" for t, r in sorted_dominance])
            if dominance_str: description_lines.append(f"**Dominance:** {dominance_str}")
        
        if metrics["long_bias"] >= BIAS_THRESHOLD:
            description_lines.append(f"🔴 **Long Flush:** `{metrics['long_bias']:.1%}` Longs")
        elif metrics["short_bias"] >= BIAS_THRESHOLD:
            description_lines.append(f"🟢 **Short Squeeze:** `{metrics['short_bias']:.1%}` Shorts")

        embed = {"title": title, "description": "\n".join(description_lines), "color": 15844367 if "CRITICAL" in title else 16776960}
        
        # REFACTOR: Use aiohttp for non-blocking post
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}) as response:
                    response.raise_for_status()
                    logging.info("Successfully sent summary notification to Discord.")
                    self.last_summary_sent = datetime.now(timezone.utc)
            except aiohttp.ClientError as e:
                logging.error(f"Failed to send Discord summary notification: {e}")

    async def check_and_transition(self, conn: sqlite3.Connection):
        now = datetime.now(timezone.utc)
        
        # REFACTOR: Speed calculation logic based on state
        if self.state == "ACTIVE":
            # Use in-memory events for ongoing speed calculation
            events = self.active_period_events
            window_duration = (now - self.active_since).total_seconds() if self.active_since else ANALYSIS_WINDOW_SECONDS
        else: # IDLE state
            # Use DB query for initial detection
            time_window_start = now - timedelta(seconds=ANALYSIS_WINDOW_SECONDS)
            events = get_liquidations_in_timeframe(conn, time_window_start, now)
            window_duration = ANALYSIS_WINDOW_SECONDS

        current_metrics = calculate_liquidation_metrics(events, window_duration)
        current_speed = current_metrics["speed_usd_per_sec"]

        logging.debug(f"State: {self.state}, Speed: {current_speed:.2f}, Events: {len(events)}")

        if self.state == "IDLE":
            if current_speed >= BASE_THRESHOLD_USD_PER_SEC:
                if self.last_summary_sent and (now - self.last_summary_sent).total_seconds() < SUMMARY_COOLDOWN_SECONDS:
                    return

                self.state = "ACTIVE"
                self.active_since = now
                self.active_period_events = list(events)
                self.prev_known_speed = self.last_known_speed
                self.last_above_threshold_time = now # Start grace period timer
                logging.info(f"Transitioned to ACTIVE with {len(events)} events. Prev speed: {self.prev_known_speed:.2f}")
            self.last_known_speed = current_speed

        elif self.state == "ACTIVE":
            if current_speed < BASE_THRESHOLD_USD_PER_SEC:
                grace_period_elapsed = (now - self.last_above_threshold_time).total_seconds()
                if grace_period_elapsed >= ACTIVE_IDLE_TRANSITION_GRACE_PERIOD_SECONDS:
                    logging.info("Speed dropped below threshold for grace period. Preparing SUMMARY.")
                    summary_metrics = calculate_liquidation_metrics(self.active_period_events, (now - self.active_since).total_seconds())
                    acceleration = calculate_acceleration(summary_metrics['speed_usd_per_sec'], self.prev_known_speed)
                    
                    await self._send_summary_notification(summary_metrics, acceleration, self.prev_known_speed)

                    self.state = "IDLE"
                    self.active_since = None
                    self.active_period_events = []
                    self.last_known_speed = 0.0
                    self.prev_known_speed = 0.0
                    logging.info("Transitioned to IDLE after sending SUMMARY.")
            else:
                self.last_above_threshold_time = now # Reset grace period timer
            self.last_known_speed = current_speed

# REFACTOR: Make async and accept connection
async def process_message(conn: sqlite3.Connection, message, monitor: LiquidationMonitor):
    try:
        ticker, direction, amount = parse_liquidation_message(message.text)
        if not all([ticker, direction, amount]): return

        now = message.date.astimezone(timezone.utc)
        logging.info(f"Detected liquidation: {ticker}-{direction}, Amount: {amount:.2f}")
        
        add_liquidation(conn, now, ticker, direction, amount)
        
        if monitor.state == "ACTIVE":
            new_event = {"timestamp": now, "ticker": ticker, "direction": direction, "amount": amount}
            monitor.active_period_events.append(new_event)
            logging.info(f"Appended event to active session. Total events: {len(monitor.active_period_events)}")

        if SINGLE_EVENT_NOTIFICATION_THRESHOLD > 0 and amount >= SINGLE_EVENT_NOTIFICATION_THRESHOLD:
            single_event = {"timestamp": now, "ticker": ticker, "direction": direction, "amount": amount}
            await monitor._send_single_event_notification(single_event)
        
    except Exception as e:
        logging.error(f"Error processing message (ID: {message.id}): {e}", exc_info=True)

async def monitor_loop(monitor: LiquidationMonitor, conn: sqlite3.Connection):
    while True:
        try:
            await monitor.check_and_transition(conn)
            await asyncio.sleep(MONITORING_INTERVAL_SECONDS)
        except Exception as e:
            logging.error(f"Error in monitoring loop: {e}", exc_info=True)
            await asyncio.sleep(MONITORING_INTERVAL_SECONDS * 2) # Longer sleep on error

async def main():
    if not all([API_ID, API_HASH, CHANNEL_USERNAME]):
        logging.error("API_ID, API_HASH, and CHANNEL_USERNAME must be set.")
        return

    # REFACTOR: Persistent DB connection
    db_conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    init_db(db_conn)
    
    client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
    monitor = LiquidationMonitor()

    @client.on(events.NewMessage(chats=CHANNEL_USERNAME))
    async def new_message_handler(event):
        # REFACTOR: Pass connection to handler
        await process_message(db_conn, event.message, monitor)

    try:
        async with client:
            logging.info("Client starting...")
            
            channel_entity = await client.get_entity(CHANNEL_USERNAME)
            logging.info(f"Fetching last {MESSAGE_HISTORY_LIMIT} messages...")
            try:
                history = await client.get_messages(channel_entity, limit=MESSAGE_HISTORY_LIMIT)
                for message in reversed(history):
                    if message and message.text:
                        await process_message(db_conn, message, monitor)
            except Exception as e:
                logging.error(f"Error fetching historical messages: {e}")
            
            logging.info("Initial state built. Starting monitoring loop...")
            monitor_task = asyncio.create_task(monitor_loop(monitor, db_conn))

            await client.run_until_disconnected()
    finally:
        # Ensure tasks are cancelled and connections closed
        if 'monitor_task' in locals() and not monitor_task.done():
            monitor_task.cancel()
        db_conn.close()
        logging.info("Database connection closed.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Shutting down gracefully.")
    except Exception as e:
        logging.error(f"An unexpected error occurred in main execution: {e}", exc_info=True)
