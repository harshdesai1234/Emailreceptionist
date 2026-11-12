from datetime import datetime, timedelta
from typing import List


class CalendarService:
    @staticmethod
    def get_suggested_demo_slots(count: int = 3) -> List[str]:
        """
        Calculates upcoming available demo meeting slots based on business days.
        """
        now = datetime.now()
        slots = []
        days_added = 0
        preferred_hours = ["02:30 PM", "11:00 AM", "04:00 PM", "10:30 AM", "03:30 PM"]

        while len(slots) < count:
            days_added += 1
            target_date = now + timedelta(days=days_added)
            # Skip weekends (5=Saturday, 6=Sunday)
            if target_date.weekday() >= 5:
                continue

            day_name = target_date.strftime("%a")
            hour_str = preferred_hours[(len(slots)) % len(preferred_hours)]
            slots.append(f"{day_name}, {hour_str} EST")

        return slots
