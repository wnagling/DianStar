"""Read the device location through Windows location services."""

import asyncio
import sys
from dataclasses import dataclass


class LocationUnavailable(Exception):
    """Windows location services could not provide a position."""


@dataclass(frozen=True)
class DeviceLocation:
    latitude: float
    longitude: float
    accuracy_meters: float

    def as_tool_result(self) -> dict[str, float]:
        return {
            "latitude": round(self.latitude, 4),
            "longitude": round(self.longitude, 4),
            "accuracy_meters": round(self.accuracy_meters, 1),
        }


async def get_location() -> DeviceLocation:
    """Request permission, then obtain one position from Windows."""
    if sys.platform != "win32":
        raise LocationUnavailable("Device location is available only on Windows.")
    try:
        from winrt.windows.devices.geolocation import Geolocator, GeolocationAccessStatus
    except ImportError as error:
        raise LocationUnavailable("Windows location dependency is not installed.") from error

    try:
        access = await asyncio.wait_for(Geolocator.request_access_async(), timeout=15)
        if access != GeolocationAccessStatus.ALLOWED:
            raise LocationUnavailable("Windows location access was denied or is unavailable.")
        position = await asyncio.wait_for(Geolocator().get_geoposition_async(), timeout=20)
    except TimeoutError as error:
        raise LocationUnavailable("Windows location request timed out.") from error
    except LocationUnavailable:
        raise
    except Exception as error:
        raise LocationUnavailable("Windows could not provide a location.") from error

    coordinate = position.coordinate
    return DeviceLocation(coordinate.latitude, coordinate.longitude, coordinate.accuracy)
