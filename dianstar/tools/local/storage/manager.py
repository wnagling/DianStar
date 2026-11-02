"""Compose managed local storage without coupling it to UI or model clients."""
from .base import StorageBase
from .scratch import ScratchMixin
from .trash import TrashMixin
from .maintenance import MaintenanceMixin


class StorageManager(ScratchMixin, TrashMixin, MaintenanceMixin, StorageBase):
    pass
