from rest_framework import serializers

from .models import Trip


class TripSerializer(serializers.ModelSerializer):
    class Meta:
        model = Trip
        fields = [
            "id", "created_at",
            "current_location", "pickup_location", "dropoff_location",
            "cycle_used_hours", "start_time", "utc_offset_hours",
            "route", "plan",
        ]
        read_only_fields = ["id", "created_at", "route", "plan"]


class PlanRequestSerializer(serializers.Serializer):
    """Inputs for the full plan endpoint (geocode -> route -> HOS schedule)."""

    current_location = serializers.CharField(max_length=255)
    pickup_location = serializers.CharField(max_length=255)
    dropoff_location = serializers.CharField(max_length=255)
    cycle_used_hours = serializers.FloatField(min_value=0, max_value=69.99)
    start_time = serializers.DateTimeField(required=False, allow_null=True)
    utc_offset_hours = serializers.FloatField(required=False, default=-4.0,
                                              min_value=-12, max_value=14)
    avg_speed_mph = serializers.FloatField(required=False, allow_null=True,
                                           min_value=30, max_value=75)
    allow_34hr_restart = serializers.BooleanField(required=False, default=True)
    save = serializers.BooleanField(required=False, default=True)
