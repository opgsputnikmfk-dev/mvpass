"""Reproducibility gate for future runners. Not an authorization/security boundary."""
import hashlib
import json
from pathlib import Path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify(lock, root, expected_lock_sha256):
    """Trust anchor must be recorded separately before any holdout access.

    Call before downloading/loading holdout data. All transitive research
    dependencies must appear in artifacts. This verifies reproducibility only;
    an operator controlling both the trust anchor and code can bypass it.
    """
    root = Path(root).resolve()
    lock = Path(lock)
    if digest(lock) != expected_lock_sha256:
        raise ValueError("Lock differs from registered digest")
    doc = json.loads(lock.read_text())
    if doc.get("schema") != 1 or doc.get("eligible") is not True:
        raise ValueError("Unqualified or unsupported lock")
    if doc.get("selected") is None:
        raise ValueError("No candidate")
    windows = doc.get("windows")
    if not isinstance(windows, list) or not windows:
        raise ValueError("Missing registered windows")
    from datetime import date
    parsed = []
    for w in windows:
        if not isinstance(w, list) or len(w) != 2:
            raise ValueError("Invalid window")
        a, b = map(date.fromisoformat, w)
        if a >= b:
            raise ValueError("Invalid interval")
        parsed.append((a, b))
    for a, b in zip(sorted(parsed), sorted(parsed)[1:]):
        if a[1] > b[0]:
            raise ValueError("Overlapping windows")
    artifacts = doc.get("artifacts", {})
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("No artifact hashes")
    roles = doc.get("roles", {})
    if set(roles) != {"protocol", "preliminary", "runner"}:
        raise ValueError("Missing required artifact roles")
    if any(p not in artifacts for p in roles.values()):
        raise ValueError("Unhashed required artifact")
    for name, expected in artifacts.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Invalid artifact path")
        if digest(path) != expected:
            raise ValueError("Artifact differs: " + name)
    result = json.loads((root / roles["preliminary"]).read_text())
    if result.get("eligible") is not True or result.get("selected") != doc["selected"]:
        raise ValueError("Preliminary selection differs")
    return doc
