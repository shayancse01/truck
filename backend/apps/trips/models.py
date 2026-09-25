import uuid

from django.db import models


class Trip(models.Model):
    """A planned HOS trip: inputs + generated route & schedule (persisted)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    # ---- inputs -------------------------------------------------------------
    current_location = models.CharField(max_length=255)
    pickup_location = models.CharField(max_length=255)
    dropoff_location = models.CharField(max_length=255)
    cycle_used_hours = models.FloatField(default=0.0)   # hours already used in cycle
    start_time = models.DateTimeField()                 # local departure time
    utc_offset_hours = models.FloatField(default=-4.0)  # driver's timezone offset

    # ---- derived route info (JSON blobs) -------------------------------------
    route = models.JSONField(default=dict, blank=True)      # geometry/distance/duration
    plan = models.JSONField(default=dict, blank=True)       # HOS schedule output

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.pickup_location} → {self.dropoff_location} ({self.cycle_used_hours}h used)"
