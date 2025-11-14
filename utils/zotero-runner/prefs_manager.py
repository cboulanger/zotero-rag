import json
import logging
import re

logger = logging.getLogger(__name__)

class PrefsManager:
    def __init__(self, prefix: str = "user_pref"):
        self.prefix = prefix
        self.prefs = {}

    def set_prefs(self, prefs_to_set: dict):
        self.prefs.update(prefs_to_set)

    async def read(self, path: str):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Regex to find user_pref("key", value);
            pref_pattern = re.compile(r'user_pref\("([^"]+)",\s*(.*?)\);')
            for match in pref_pattern.finditer(content):
                key, value_str = match.groups()
                try:
                    # Use json.loads for robust parsing of strings, booleans, numbers
                    self.prefs[key] = json.loads(value_str)
                except json.JSONDecodeError:
                    # Fallback for unquoted strings or other non-json values
                    self.prefs[key] = value_str
        except FileNotFoundError:
            logger.warning(f"prefs.js not found at {path}, will create a new one.")
        except Exception as e:
            logger.error(f"Error reading prefs file {path}: {e}")

    async def write(self, path: str):
        lines = []
        for key, value in self.prefs.items():
            # json.dumps will correctly handle strings, bools, numbers
            value_str = json.dumps(value)
            lines.append(f'{self.prefix}("{key}", {value_str});')
        
        content = "\n".join(lines)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)