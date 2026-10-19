"""Google Calendar 'new event' link, prefilled — the user just clicks Save."""
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from .store import TZ


def gcal_url(task: dict, source_name: str | None = None) -> str:
    details = [task.get("details") or ""]
    if task.get("amount"):
        details.append(f"Amount: {task['amount']:g} {task.get('currency') or ''}".strip())
    if task.get("payment_url"):
        details.append(f"Pay: {task['payment_url']}")
    if source_name:
        details.append(f"From: {source_name}")
    params = {"action": "TEMPLATE", "text": task["title"], "details": "\n".join(d for d in details if d),
              "ctz": str(TZ)}
    if task.get("location"):
        params["location"] = task["location"]

    d = date.fromisoformat(task["due_date"]) if task.get("due_date") else datetime.now(TZ).date()
    if task.get("start_time"):
        h, m = map(int, task["start_time"].split(":"))
        start = datetime(d.year, d.month, d.day, h, m)
        if task.get("end_time"):
            eh, em = map(int, task["end_time"].split(":"))
            end = datetime(d.year, d.month, d.day, eh, em)
            if end <= start:
                end += timedelta(days=1)
        else:
            end = start + timedelta(hours=1)
        params["dates"] = f"{start:%Y%m%dT%H%M%S}/{end:%Y%m%dT%H%M%S}"
    else:
        params["dates"] = f"{d:%Y%m%d}/{d + timedelta(days=1):%Y%m%d}"
    return "https://calendar.google.com/calendar/render?" + urlencode(params)
