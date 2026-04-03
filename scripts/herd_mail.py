#!/usr/bin/env python3
"""
herd-mail: AI-to-AI communication facilitator for the herd.

A secure, user-friendly wrapper for waggle that handles the full lifecycle
of herd email communication: sending, reading, checking, and downloading.

Commands:
    herd_mail.py send --to recipient@example.com --subject "Hello" --body "Message"
    herd_mail.py list [--folder INBOX] [--limit 20] [--unread] [--human]
    herd_mail.py read <uid> [--folder INBOX] [--human]
    herd_mail.py check [--folder INBOX] [--human]
    herd_mail.py download <uid> [--folder INBOX] [--dest-dir .]
    herd_mail.py config

Environment Variables (see .envrc.template):
    WAGGLE_HOST         SMTP host
    WAGGLE_PORT         SMTP port (default: 465)
    WAGGLE_USER         SMTP/IMAP username
    WAGGLE_PASS         SMTP/IMAP password
    WAGGLE_FROM         From email address
    WAGGLE_NAME         Display name
    WAGGLE_TLS          Use TLS (default: true)
    WAGGLE_IMAP_HOST    IMAP host (required for list/read/check/download)
    WAGGLE_IMAP_PORT    IMAP port (default: 993)
    WAGGLE_IMAP_TLS     Use IMAP TLS (default: true)
    WAGGLE_TO           Default test recipient
    WAGGLE_SEND_LOG     Path to send log (for duplicate detection)
    WAGGLE_DEV_PATH     Optional: Path to local waggle for development

Author: O.C.
License: MIT
"""

import argparse
import imaplib
import json
import logging
import os
import re
import sys
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
from pathlib import Path
from typing import Any, Optional

# Constants
DEFAULT_SMTP_PORT = 465
DEFAULT_IMAP_PORT = 993
DEFAULT_DUPLICATE_CHECK_MINUTES = 5
DEFAULT_IMAP_FOLDER = "INBOX"
DEFAULT_NO_BODY_MESSAGE = "(No message body)"
DEFAULT_LIST_LIMIT = 20
SENT_FOLDER_CANDIDATES = ["[Gmail]/Sent Mail", "Sent", "Sent Items", "INBOX.Sent", "[Gmail]/All Mail"]

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Add local waggle development path ONLY if explicitly enabled via env var
# This prevents arbitrary code execution from hardcoded paths
WAGGLE_DEV_PATH = os.environ.get("WAGGLE_DEV_PATH")
if WAGGLE_DEV_PATH:
    dev_path = Path(WAGGLE_DEV_PATH)
    if dev_path.exists() and dev_path.is_dir():
        sys.path.insert(0, str(dev_path))
        logger.info(f"Using development waggle from: {dev_path}")
    else:
        logger.warning(f"WAGGLE_DEV_PATH set but path doesn't exist: {dev_path}")

# Try to import waggle, but don't exit at module level (allows tests to run)
WAGGLE_AVAILABLE = False
WAGGLE_IMPORT_ERROR = None

try:
    from waggle import (
        send_email, check_recently_sent, read_message,
        list_inbox, download_attachments,
    )
    WAGGLE_AVAILABLE = True
except ImportError as e:
    WAGGLE_IMPORT_ERROR = e
    # Don't exit here - allow module to be imported for testing
    # We'll check in main() before actually trying to use waggle

    # Create stub functions so tests can mock them
    def send_email(*args, **kwargs):
        raise RuntimeError("waggle not installed")

    def check_recently_sent(*args, **kwargs):
        raise RuntimeError("waggle not installed")

    def read_message(*args, **kwargs):
        raise RuntimeError("waggle not installed")

    def list_inbox(*args, **kwargs):
        raise RuntimeError("waggle not installed")

    def download_attachments(*args, **kwargs):
        raise RuntimeError("waggle not installed")


def validate_email_address(email: str) -> bool:
    """
    Validate email address format.

    Args:
        email: Email address to validate

    Returns:
        True if valid, False otherwise
    """
    if not email or not isinstance(email, str):
        return False

    # Check for suspicious characters that could indicate header injection
    # (check BEFORE parseaddr, which might strip them)
    suspicious_chars = ['\n', '\r', '\0', '\t']
    if any(char in email for char in suspicious_chars):
        return False

    # Use email.utils.parseaddr for basic validation
    name, addr = parseaddr(email)

    # Check for basic email structure: has @ and domain
    if not addr or '@' not in addr:
        return False

    local, domain = addr.rsplit('@', 1)

    # Basic sanity checks
    if not local or not domain:
        return False

    if '.' not in domain:
        return False

    return True


def validate_email_list(emails: str) -> bool:
    """
    Validate comma-separated list of email addresses.

    Args:
        emails: Comma-separated email addresses

    Returns:
        True if all valid, False otherwise
    """
    if not emails:
        return True  # Empty is ok

    for email in emails.split(','):
        if not validate_email_address(email.strip()):
            return False

    return True


def sanitize_for_display(text: str, max_length: int = 200) -> str:
    """
    Sanitize text for terminal display to prevent escape sequence injection.

    Args:
        text: Text to sanitize
        max_length: Maximum length to display

    Returns:
        Sanitized text
    """
    if not text:
        return ""

    # Remove ANSI escape sequences (ESC followed by [ and control codes)
    # Pattern matches: ESC [ ... (any chars between @ and ~)
    ansi_escape = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
    sanitized = ansi_escape.sub('', text)

    # Remove other control characters except newline and tab
    sanitized = ''.join(
        char for char in sanitized
        if char.isprintable() or char in ('\n', '\t')
    )

    # Truncate if too long
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + "..."

    return sanitized


def validate_file_path(file_path: str, must_exist: bool = True) -> Optional[Path]:
    """
    Validate and resolve file path with security checks.

    Args:
        file_path: Path to validate
        must_exist: Whether file must exist

    Returns:
        Resolved Path object if valid, None otherwise
    """
    try:
        path = Path(file_path).resolve()

        # Check if path exists (if required)
        if must_exist and not path.exists():
            logger.error(f"File not found: {file_path}")
            return None

        # Check if it's a file (not directory)
        if must_exist and not path.is_file():
            logger.error(f"Path is not a file: {file_path}")
            return None

        # Prevent reading from sensitive system directories
        # Include both standard Linux paths and macOS equivalents
        sensitive_dirs = [
            '/etc', '/private/etc',  # System config (macOS: /etc -> /private/etc)
            '/sys',                   # System info (Linux)
            '/proc',                  # Process info (Linux)
            '/dev',                   # Device files
            '/var/log',               # System logs
        ]
        for sensitive in sensitive_dirs:
            if str(path).startswith(sensitive):
                logger.error(f"Access to sensitive path denied: {file_path}")
                return None

        return path

    except (ValueError, OSError) as e:
        logger.error(f"Invalid file path: {file_path} ({e})")
        return None


def decode_escape_sequences(text: str) -> str:
    """
    Decode common escape sequences from command line input.

    Args:
        text: Text with potential escape sequences

    Returns:
        Text with escape sequences decoded
    """
    # Handle common escape sequences
    replacements = {
        '\\n': '\n',
        '\\r': '\r',
        '\\t': '\t',
        '\\\\': '\\',
        '\\"': '"',
        "\\'": "'",
    }

    result = text
    for escaped, actual in replacements.items():
        result = result.replace(escaped, actual)

    return result


def parse_port(port_str: str, default: int, port_name: str = "port") -> int:
    """
    Parse port number from string with validation.

    Args:
        port_str: Port as string
        default: Default port if parsing fails
        port_name: Name for error messages

    Returns:
        Valid port number

    Raises:
        ValueError: If port is invalid
    """
    try:
        port = int(port_str)
        if not (1 <= port <= 65535):
            raise ValueError(f"{port_name} must be between 1 and 65535")
        return port
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid {port_name}: {port_str} ({e})")


def get_config() -> dict[str, Any]:
    """
    Build configuration from environment variables.

    Returns:
        Configuration dictionary

    Raises:
        ValueError: If port configuration is invalid
    """
    # Parse ports with validation
    try:
        smtp_port = parse_port(
            os.environ.get("WAGGLE_PORT", str(DEFAULT_SMTP_PORT)),
            DEFAULT_SMTP_PORT,
            "SMTP port"
        )
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        raise

    try:
        imap_port = parse_port(
            os.environ.get("WAGGLE_IMAP_PORT", str(DEFAULT_IMAP_PORT)),
            DEFAULT_IMAP_PORT,
            "IMAP port"
        )
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        raise

    return {
        "smtp_host": os.environ.get("WAGGLE_HOST"),
        "smtp_port": smtp_port,
        "smtp_user": os.environ.get("WAGGLE_USER"),
        "smtp_pass": os.environ.get("WAGGLE_PASS"),
        "from_addr": os.environ.get("WAGGLE_FROM"),
        "from_name": os.environ.get("WAGGLE_NAME", ""),
        "use_tls": os.environ.get("WAGGLE_TLS", "true").lower() == "true",
        "imap_host": os.environ.get("WAGGLE_IMAP_HOST"),
        "imap_port": imap_port,
        "imap_tls": os.environ.get("WAGGLE_IMAP_TLS", "true").lower() == "true",
        "send_log": os.environ.get("WAGGLE_SEND_LOG"),
    }


def validate_config(cfg: dict[str, Any], require_smtp: bool = True, require_imap: bool = False) -> bool:
    """
    Validate configuration has required values.

    Args:
        cfg: Configuration dictionary
        require_smtp: Whether to require SMTP settings
        require_imap: Whether to require IMAP settings

    Returns:
        True if valid, False otherwise
    """
    errors = []

    if require_smtp:
        required = ["smtp_host", "smtp_user", "smtp_pass", "from_addr"]
        for key in required:
            if not cfg.get(key):
                errors.append(f"Missing {key} (set WAGGLE_{key.upper()})")

        from_addr = cfg.get("from_addr")
        if from_addr and not validate_email_address(from_addr):
            errors.append(f"Invalid from_addr email format: {from_addr}")

    if require_imap:
        if not cfg.get("imap_host"):
            errors.append("Missing imap_host (set WAGGLE_IMAP_HOST)")

    if errors:
        logger.error("Configuration errors:")
        for error in errors:
            logger.error(f"  - {error}")
        logger.error("\nSee .envrc.template for required variables.")
        return False

    return True


def build_waggle_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """
    Convert our config to waggle's expected format.

    Args:
        cfg: Our configuration dictionary

    Returns:
        Configuration in waggle's format
    """
    return {
        "host": cfg["smtp_host"],
        "port": cfg["smtp_port"],
        "user": cfg["smtp_user"],
        "password": cfg["smtp_pass"],
        "from_addr": cfg["from_addr"],
        "from_name": cfg["from_name"],
        "tls": cfg["use_tls"],
        "imap_host": cfg.get("imap_host"),
        "imap_port": cfg.get("imap_port", DEFAULT_IMAP_PORT),
        "imap_tls": cfg.get("imap_tls", True),
    }


def save_to_sent(
    cfg: dict[str, Any],
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    reply_to: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> bool:
    """
    Save a copy of the sent message to the IMAP Sent folder.

    Returns True if saved, False on any failure. Failures are non-fatal.
    """
    wcfg = build_waggle_config(cfg)

    # Build RFC822 message
    msg = EmailMessage()
    from_display = f"{wcfg['from_name']} <{wcfg['from_addr']}>" if wcfg.get("from_name") else wcfg["from_addr"]
    msg["From"] = from_display
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    if cc:
        msg["Cc"] = cc
    if reply_to:
        msg["Reply-To"] = reply_to
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)

    msg_bytes = msg.as_bytes()

    try:
        # Connect to IMAP
        if wcfg.get("imap_tls", True):
            conn = imaplib.IMAP4_SSL(wcfg["imap_host"], wcfg.get("imap_port", DEFAULT_IMAP_PORT))
        else:
            conn = imaplib.IMAP4(wcfg["imap_host"], wcfg.get("imap_port", DEFAULT_IMAP_PORT))

        try:
            conn.login(wcfg["user"], wcfg["password"])

            # Find the Sent folder
            status, folder_data = conn.list()
            folder_names = []
            if status == "OK" and folder_data:
                for item in folder_data:
                    if isinstance(item, bytes):
                        decoded = item.decode("utf-8", errors="replace")
                        # Extract folder name from IMAP LIST response
                        # Format: (flags) "delimiter" "name"
                        parts = decoded.rsplit('"', 2)
                        if len(parts) >= 2:
                            folder_names.append(parts[-2])

            sent_folder = None
            for candidate in SENT_FOLDER_CANDIDATES:
                if candidate in folder_names:
                    sent_folder = candidate
                    break

            if not sent_folder:
                logger.warning(f"No Sent folder found (tried: {', '.join(SENT_FOLDER_CANDIDATES)})")
                return False

            # Append message
            status, _ = conn.append(f'"{sent_folder}"', "\\Seen", None, msg_bytes)
            if status != "OK":
                logger.warning(f"IMAP APPEND failed: {status}")
                return False

            return True

        finally:
            try:
                conn.logout()
            except Exception:
                pass

    except (OSError, imaplib.IMAP4.error) as e:
        logger.warning(f"Could not save to Sent folder: {e}")
        return False
    except Exception as e:
        logger.warning(f"Unexpected error saving to Sent folder: {e}")
        return False


def output_json(data: dict[str, Any]) -> None:
    """Write JSON to stdout. All logging must go to stderr."""
    print(json.dumps(data, indent=2, default=str))


def output_human_list(data: dict[str, Any]) -> None:
    """Write human-readable message list to stdout."""
    messages = data.get("messages", [])
    if not messages:
        print(f"No messages in {data['folder']}.")
        return

    print(f"{'UID':<8} {'From':<30} {'Subject':<40} {'Date':<20} {'Status'}")
    print("-" * 110)
    for msg in messages:
        status = "*" if msg.get("unread") else " "
        from_name = msg.get("from_name", "")
        from_addr = msg.get("from_addr", "")
        # Show name if available, otherwise email; prefer showing the email for clarity
        from_display = f"{from_name} ({from_addr})" if from_name and from_addr else (from_name or from_addr or "")
        subject = msg.get("subject", "(no subject)")
        from_display = from_display[:28] if len(from_display) > 28 else from_display
        subject = subject[:38] if len(subject) > 38 else subject
        date = msg.get("date", "")[:18]
        print(f"{msg['uid']:<8} {from_display:<30} {subject:<40} {date:<20} {status}")


def output_human_read(data: dict[str, Any]) -> None:
    """Write human-readable message to stdout."""
    from_name = data.get("from_name", "")
    from_addr = data.get("from_addr", "")
    from_display = f"{from_name} <{from_addr}>" if from_name else from_addr

    print(f"From: {from_display}")
    print(f"To: {data.get('to', '')}")
    print(f"Date: {data.get('date', '')}")
    print(f"Subject: {data.get('subject', '')}")

    attachments = data.get("attachments", [])
    if attachments:
        names = ", ".join(a.get("filename", "unknown") for a in attachments)
        print(f"Attachments: {names}")

    print("-" * 60)

    body = data.get("body_plain") or data.get("body_html") or "(no body)"
    print(body)


def output_human_check(data: dict[str, Any]) -> None:
    """Write human-readable check result to stdout."""
    count = data.get("unread_count", 0)
    folder = data.get("folder", "INBOX")
    if count == 0:
        print(f"No unread messages in {folder}.")
    else:
        print(f"{count} unread message(s) in {folder}.")


def cmd_send(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    """Handle the send subcommand."""
    # Handle --dry-run as alias for config command (before email validation)
    if args.dry_run:
        return cmd_config(args, cfg)

    # Validate email addresses early
    if not validate_email_address(args.to):
        logger.error(f"Invalid recipient email address: {sanitize_for_display(args.to)}")
        return 1

    if args.cc and not validate_email_list(args.cc):
        logger.error(f"Invalid CC email address(es): {sanitize_for_display(args.cc)}")
        return 1

    if args.reply_to and not validate_email_address(args.reply_to):
        logger.error(f"Invalid Reply-To email address: {sanitize_for_display(args.reply_to)}")
        return 1

    # Validate SMTP configuration
    if not validate_config(cfg, require_smtp=True):
        return 1

    # Get body content
    body: Optional[str] = None
    if args.body_file:
        validated_path = validate_file_path(args.body_file, must_exist=True)
        if not validated_path:
            return 1

        try:
            with open(validated_path, "r", encoding="utf-8") as f:
                body = f.read()
        except UnicodeDecodeError as e:
            logger.error(f"Error: Body file must be UTF-8: {e}")
            return 1
        except OSError as e:
            logger.error(f"Error reading body file: {e}")
            return 1
    elif args.body:
        body = decode_escape_sequences(args.body)
    else:
        if not sys.stdin.isatty():
            try:
                body = sys.stdin.read()
            except (OSError, UnicodeDecodeError) as e:
                logger.error(f"Error reading from stdin: {e}")
                return 1
        else:
            body = DEFAULT_NO_BODY_MESSAGE

    # Check for duplicates (unless skipped)
    if not args.skip_duplicate_check:
        try:
            if check_recently_sent(
                args.to,
                args.subject,
                within_minutes=DEFAULT_DUPLICATE_CHECK_MINUTES,
                config=build_waggle_config(cfg)
            ):
                logger.warning(
                    f"Duplicate detected: recently sent to "
                    f"{sanitize_for_display(args.to)} with similar subject"
                )
                logger.info("Use --skip-duplicate-check to override")
                return 0
        except (OSError, ValueError) as e:
            logger.warning(f"Could not check for duplicates: {e}")
            logger.info("Continuing anyway...")

    # Handle reply with threading
    in_reply_to: Optional[str] = None
    references: Optional[str] = None

    if args.message_id:
        try:
            logger.info(f"Fetching original message {args.message_id} for threading...")
            original = read_message(
                args.message_id,
                folder=DEFAULT_IMAP_FOLDER,
                config=build_waggle_config(cfg)
            )
            in_reply_to = original.get("message_id")
            references = original.get("reply_references")
            subject = sanitize_for_display(original.get('subject', 'Unknown'))
            logger.info(f"  Found: {subject}")
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.warning(f"Warning: Could not fetch original message: {e}")
            logger.info("  Continuing without threading headers...")
        except Exception as e:
            logger.warning(f"Unexpected error fetching message: {e}")
            logger.info("  Continuing without threading headers...")

    # Send the email
    try:
        to_display = sanitize_for_display(args.to, max_length=50)
        logger.info(f"Sending email to {to_display}...")

        send_email(
            to=args.to,
            subject=args.subject,
            body_md=body,
            cc=args.cc,
            reply_to=args.reply_to,
            in_reply_to=in_reply_to,
            references=references,
            attachments=args.attachment,
            rich=args.rich,
            config=build_waggle_config(cfg),
        )

        saved = False
        if cfg.get("imap_host"):
            saved = save_to_sent(
                cfg, args.to, args.subject, body,
                cc=args.cc, reply_to=args.reply_to,
                in_reply_to=in_reply_to, references=references,
            )

        logger.info("Email sent successfully!")
        if saved:
            logger.info("  (Saved to Sent folder via IMAP)")
        elif cfg.get("imap_host"):
            logger.warning("  (Could not save to Sent folder)")

        return 0

    except ConnectionError as e:
        logger.error(f"Connection error: {e}")
        return 1
    except TimeoutError as e:
        logger.error(f"Timeout error: {e}")
        return 1
    except ValueError as e:
        logger.error(f"Invalid input: {e}")
        return 1
    except OSError as e:
        logger.error(f"I/O error: {e}")
        return 1
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        logger.debug("", exc_info=True)
        return 1


def cmd_config(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    """Handle the config subcommand. Validates SMTP and IMAP settings."""
    smtp_valid = validate_config(cfg, require_smtp=True, require_imap=False)

    if smtp_valid:
        logger.info("SMTP configuration valid.")
        smtp_user_display = cfg['smtp_user'][:3] + "***" if cfg['smtp_user'] else "***"
        logger.info(f"  SMTP: {smtp_user_display}@{cfg['smtp_host']}:{cfg['smtp_port']}")
        logger.info(f"  From: {cfg['from_name']} <{cfg['from_addr']}>")
    else:
        logger.error("SMTP configuration invalid.")

    if cfg.get("imap_host"):
        logger.info(f"  IMAP: {cfg['imap_host']}:{cfg['imap_port']} (configured)")
    else:
        logger.warning("  IMAP: not configured (set WAGGLE_IMAP_HOST for read commands)")

    return 0 if smtp_valid else 1


def cmd_list(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    """Handle the list subcommand."""
    if not validate_config(cfg, require_smtp=False, require_imap=True):
        return 1

    try:
        messages = list_inbox(
            folder=args.folder,
            limit=args.limit,
            config=build_waggle_config(cfg),
        )
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"Failed to list messages: {e}")
        return 1
    except Exception as e:
        logger.error(f"Unexpected error listing messages: {e}")
        return 1

    if args.unread:
        messages = [m for m in messages if m.get("unread")]

    data = {
        "folder": args.folder,
        "count": len(messages),
        "messages": messages,
    }

    if args.human:
        output_human_list(data)
    else:
        output_json(data)

    return 0


def cmd_read(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    """Handle the read subcommand."""
    if not validate_config(cfg, require_smtp=False, require_imap=True):
        return 1

    try:
        message = read_message(
            args.uid,
            folder=args.folder,
            config=build_waggle_config(cfg),
        )
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"Failed to read message {args.uid}: {e}")
        return 1
    except Exception as e:
        logger.error(f"Unexpected error reading message: {e}")
        return 1

    if args.human:
        output_human_read(message)
    else:
        output_json(message)

    return 0


def cmd_check(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    """
    Handle the check subcommand.

    Exit codes: 0=has unread, 1=no unread, 2=error.
    """
    if not validate_config(cfg, require_smtp=False, require_imap=True):
        return 2

    try:
        messages = list_inbox(
            folder=args.folder,
            config=build_waggle_config(cfg),
        )
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"Failed to check messages: {e}")
        return 2
    except Exception as e:
        logger.error(f"Unexpected error checking messages: {e}")
        return 2

    unread = [m for m in messages if m.get("unread")]

    data = {
        "folder": args.folder,
        "unread_count": len(unread),
        "messages": unread,
    }

    if args.human:
        output_human_check(data)
    else:
        output_json(data)

    return 0 if unread else 1


def cmd_download(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    """Handle the download subcommand."""
    if not validate_config(cfg, require_smtp=False, require_imap=True):
        return 1

    # Ensure dest_dir exists
    dest_dir = Path(args.dest_dir)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(f"Cannot create destination directory: {e}")
        return 1

    try:
        files = download_attachments(
            args.uid,
            folder=args.folder,
            dest_dir=str(dest_dir),
            config=build_waggle_config(cfg),
        )
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"Failed to download attachments: {e}")
        return 1
    except Exception as e:
        logger.error(f"Unexpected error downloading attachments: {e}")
        return 1

    data = {
        "uid": args.uid,
        "folder": args.folder,
        "files": files,
    }

    output_json(data)
    return 0


def main() -> int:
    """Main entry point with subcommand dispatch."""
    if not WAGGLE_AVAILABLE:
        logger.error("Error: waggle not installed. Run: pip install waggle-mail")
        if WAGGLE_IMPORT_ERROR:
            logger.error(f"Details: {WAGGLE_IMPORT_ERROR}")
        return 1

    # Backward compat: detect old-style invocation (no subcommand, but --to present)
    # Work on a copy to avoid mutating the global sys.argv
    argv = sys.argv[:]
    if len(argv) > 1 and argv[1].startswith('--'):
        if '--to' in argv:
            logger.warning("Deprecation warning: use 'herd_mail.py send --to ...' instead")
            argv.insert(1, 'send')
        elif '--dry-run' in argv:
            logger.warning("Deprecation warning: use 'herd_mail.py config' instead")
            argv.insert(1, 'send')

    parser = argparse.ArgumentParser(
        description="herd-mail: AI-to-AI email communication via waggle",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # send subcommand
    send_parser = subparsers.add_parser("send", help="Send an email")
    send_parser.add_argument("--to", required=True, help="Recipient email address")
    send_parser.add_argument("--subject", required=True, help="Email subject line")
    send_parser.add_argument("--body", help="Email body (Markdown supported)")
    send_parser.add_argument("--body-file", help="Read body from file (UTF-8)")
    send_parser.add_argument("--attachment", nargs="+", help="File(s) to attach")
    send_parser.add_argument("--cc", help="CC recipients (comma-separated)")
    send_parser.add_argument("--reply-to", help="Reply-To address")
    send_parser.add_argument("--message-id", help="IMAP message ID to reply to (enables threading)")
    send_parser.add_argument("--rich", action="store_true", help="Enable rich HTML formatting")
    send_parser.add_argument("--skip-duplicate-check", action="store_true", help="Skip duplicate detection")
    send_parser.add_argument("--dry-run", action="store_true", help="Validate config without sending")

    # list subcommand
    list_parser = subparsers.add_parser("list", help="List messages in a folder")
    list_parser.add_argument("--folder", default=DEFAULT_IMAP_FOLDER, help="IMAP folder (default: INBOX)")
    list_parser.add_argument("--limit", type=int, default=DEFAULT_LIST_LIMIT, help="Max messages (default: 20)")
    list_parser.add_argument("--unread", action="store_true", help="Only show unread messages")
    list_parser.add_argument("--human", action="store_true", help="Human-readable output")

    # read subcommand
    read_parser = subparsers.add_parser("read", help="Read a full message by UID")
    read_parser.add_argument("uid", help="IMAP message UID")
    read_parser.add_argument("--folder", default=DEFAULT_IMAP_FOLDER, help="IMAP folder (default: INBOX)")
    read_parser.add_argument("--human", action="store_true", help="Human-readable output")

    # check subcommand
    check_parser = subparsers.add_parser("check", help="Check for unread messages")
    check_parser.add_argument("--folder", default=DEFAULT_IMAP_FOLDER, help="IMAP folder (default: INBOX)")
    check_parser.add_argument("--human", action="store_true", help="Human-readable output")

    # download subcommand
    dl_parser = subparsers.add_parser("download", help="Download attachments from a message")
    dl_parser.add_argument("uid", help="IMAP message UID")
    dl_parser.add_argument("--folder", default=DEFAULT_IMAP_FOLDER, help="IMAP folder (default: INBOX)")
    dl_parser.add_argument("--dest-dir", default=".", help="Destination directory (default: .)")

    # config subcommand
    subparsers.add_parser("config", help="Validate configuration")

    args = parser.parse_args(argv[1:])

    if not args.command:
        parser.print_help()
        return 1

    # Load configuration
    try:
        cfg = get_config()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return 1

    # Dispatch to command handler
    commands = {
        "send": cmd_send,
        "list": cmd_list,
        "read": cmd_read,
        "check": cmd_check,
        "download": cmd_download,
        "config": cmd_config,
    }

    handler = commands.get(args.command)
    if handler:
        return handler(args, cfg)

    logger.error(f"Unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
