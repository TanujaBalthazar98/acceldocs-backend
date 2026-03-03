"""Environment variable management — read/write .env files safely."""

import re
from pathlib import Path
from typing import Any


class EnvManager:
    """Manages .env file reading and writing with validation."""

    def __init__(self, env_path: str | Path = ".env"):
        self.env_path = Path(env_path)
        if not self.env_path.exists():
            raise FileNotFoundError(f".env file not found at {self.env_path}")

    def read_all(self, redact_secrets: bool = True) -> dict[str, str]:
        """Read all environment variables from .env file.

        Args:
            redact_secrets: If True, replace secret values with '***'

        Returns:
            Dictionary of key-value pairs
        """
        env_vars = {}
        content = self.env_path.read_text()

        # Parse .env file (simple KEY=VALUE format, ignoring comments)
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            match = re.match(r'^([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$', line)
            if match:
                key, value = match.groups()
                # Remove quotes if present
                value = value.strip('"').strip("'")

                if redact_secrets and self._is_secret(key):
                    env_vars[key] = "***" if value else ""
                else:
                    env_vars[key] = value

        return env_vars

    def update(self, updates: dict[str, str]) -> None:
        """Update environment variables in .env file.

        Args:
            updates: Dictionary of key-value pairs to update

        Raises:
            ValueError: If any key contains invalid characters
        """
        # Validate keys
        for key in updates.keys():
            if not re.match(r'^[A-Z_][A-Z0-9_]*$', key):
                raise ValueError(f"Invalid environment variable name: {key}")

        content = self.env_path.read_text()
        lines = content.splitlines()
        updated_keys = set()

        # Update existing keys
        new_lines = []
        for line in lines:
            stripped = line.strip()

            # Keep comments and empty lines
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue

            match = re.match(r'^([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$', stripped)
            if match:
                key, _ = match.groups()
                if key in updates:
                    # Update this key
                    new_value = updates[key]
                    # Quote value if it contains spaces or special chars
                    if ' ' in new_value or any(c in new_value for c in ['#', '$', '\\']):
                        new_value = f'"{new_value}"'
                    new_lines.append(f"{key}={new_value}")
                    updated_keys.add(key)
                else:
                    # Keep unchanged
                    new_lines.append(line)
            else:
                # Keep malformed lines as-is
                new_lines.append(line)

        # Add new keys at the end
        for key, value in updates.items():
            if key not in updated_keys:
                if ' ' in value or any(c in value for c in ['#', '$', '\\']):
                    value = f'"{value}"'
                new_lines.append(f"{key}={value}")

        # Write back
        self.env_path.write_text("\n".join(new_lines) + "\n")

    def _is_secret(self, key: str) -> bool:
        """Check if an environment variable should be treated as a secret."""
        secret_keywords = [
            'SECRET', 'KEY', 'TOKEN', 'PASSWORD', 'CLIENT_SECRET',
            'PRIVATE', 'CREDENTIALS', 'AUTH'
        ]
        return any(keyword in key.upper() for keyword in secret_keywords)
