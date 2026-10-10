"""The receiver's replica of the sender's state. It never regresses."""

from src.header import version_newer


class StateTable:
    def __init__(self):
        self.version = {}    # field id -> latest version applied (absent = never seen)
        self.value = {}

    def apply(self, field_id, version, value):
        """Store the value only if it is newer. Returns True if it was applied."""
        have = self.version.get(field_id)
        if have is not None and not version_newer(version, have):
            return False     # old or duplicate: a delayed packet must not clobber newer state
        self.version[field_id] = version
        self.value[field_id] = value
        return True

    def missing_from(self, digest):
        """Heartbeat diff: field ids the sender has newer than we do, or we never saw.

        digest is a list of (field_id, version) pairs from the sender.
        """
        wanted = []
        for field_id, version in digest:
            have = self.version.get(field_id)
            if have is None or version_newer(version, have):
                wanted.append(field_id)
        return wanted
