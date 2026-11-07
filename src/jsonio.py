"""
JSON file persistence shared by every store in the app (annotations, rules,
categories, budgets).

Writes are atomic -- temp file + os.replace -- with the previous version kept
as `.bak`: these files hold hand-built work the user can't regenerate, and the
old truncating `open(path, 'w')` left nothing behind if a dump failed midway.
"""
import json
import os
import shutil
import tempfile


def load_json(filepath, default=None):
    """The file's contents, or `default` if it's missing or unreadable."""
    if not os.path.exists(filepath):
        return default
    try:
        with open(filepath, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(filepath, data):
    directory = os.path.dirname(filepath) or "."
    os.makedirs(directory, exist_ok=True)

    if os.path.exists(filepath):
        try:
            shutil.copy2(filepath, filepath + ".bak")
        except OSError:
            pass  # a failed backup must never block the actual save

    fd, temp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, indent=2)
        os.replace(temp_path, filepath)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise
