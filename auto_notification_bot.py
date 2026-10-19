import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from plyer import notification
except ImportError:  # pragma: no cover - fallback for environments without plyer
    notification = None


DEFAULT_CONFIG = {
    "notifications": [
        {"title": "Daily reminder", "message": "Check your tasks and email.", "time": "09:00"},
        {"title": "Break reminder", "message": "Take a short break and stretch.", "time": "15:00"},
    ],
    "check_interval_seconds": 30,
}


@dataclass
class Reminder:
    title: str
    message: str
    time: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Reminder":
        return cls(
            title=data.get("title", "Notification"),
            message=data.get("message", "You have a new reminder."),
            time=data.get("time", "09:00"),
        )

    def should_fire(self, current_time: datetime, tolerance_seconds: int = 30) -> bool:
        current_value = current_time.strftime("%H:%M")
        if current_value != self.time:
            return False

        second_value = current_time.second
        return second_value <= tolerance_seconds


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        return DEFAULT_CONFIG.copy()

    with config_path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError("Config file must contain a JSON object.")

    reminders = data.get("notifications", DEFAULT_CONFIG["notifications"])
    data["notifications"] = [Reminder.from_dict(item).__dict__ for item in reminders]

    data.setdefault("check_interval_seconds", DEFAULT_CONFIG["check_interval_seconds"])
    return data


def save_sent_state(state_path: Path, sent: set[str]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(sorted(sent)), encoding="utf-8")


def load_sent_state(state_path: Path) -> set[str]:
    if not state_path.exists():
        return set()

    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return set(map(str, data))
    except (ValueError, OSError):
        pass
    return set()


def send_desktop_notification(title: str, message: str) -> None:
    if notification is not None:
        notification.notify(
            title=title,
            message=message,
            app_name="Auto Notification Bot",
            timeout=10,
        )
        print(f"[NOTIFY] {title}: {message}")
        return

    print(f"[DESKTOP-NOTIFICATION-FALLBACK] {title}: {message}")


def run_loop(config_path: str | Path) -> None:
    config = load_config(config_path)
    reminders = [Reminder.from_dict(item) for item in config.get("notifications", [])]
    check_interval = int(config.get("check_interval_seconds", 30))
    sent_state_path = Path(config_path).with_name("sent_notifications.json")
    sent_signals = load_sent_state(sent_state_path)

    print("Auto Notification Bot started. Press Ctrl+C to stop.")

    while True:
        now = datetime.now()
        current_key = now.strftime("%Y-%m-%d")

        for reminder in reminders:
            signal_key = f"{current_key}|{reminder.time}|{reminder.title}"
            if reminder.should_fire(now, tolerance_seconds=check_interval) and signal_key not in sent_signals:
                send_desktop_notification(reminder.title, reminder.message)
                sent_signals.add(signal_key)
                save_sent_state(sent_state_path, sent_signals)

        # Clear the sent state when the day changes.
        if now.strftime("%H:%M:%S") == "00:00:00":
            sent_signals.clear()
            save_sent_state(sent_state_path, sent_signals)

        sys.stdout.flush()
        import time
        time.sleep(check_interval)


def send_one_time_notification(title: str, message: str) -> None:
    send_desktop_notification(title, message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Auto Notification Bot")
    parser.add_argument("--config", default="notifications.json", help="Path to the JSON config file")
    parser.add_argument("--title", help="Use when sending a one-time custom notification")
    parser.add_argument("--message", help="Message content for a one-time custom notification")
    parser.add_argument("--once", action="store_true", help="Send one notification and exit")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.once:
        if not args.title or not args.message:
            parser.error("--title and --message are required when using --once")
        send_one_time_notification(args.title, args.message)
        return

    run_loop(args.config)


if __name__ == "__main__":
    main()
