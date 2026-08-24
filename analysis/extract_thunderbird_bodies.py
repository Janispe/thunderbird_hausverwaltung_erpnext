from __future__ import annotations

import argparse
import gzip
import html
import json
import mailbox
import re
import sys
from email.message import Message
from pathlib import Path


HTML_TAG_RE = re.compile(r"<[^>]+>")


def normalize_message_id(value: object) -> str:
	return str(value or "").strip().removeprefix("<").removesuffix(">").casefold()


def decode_part(part: Message) -> str:
	try:
		content = part.get_content()
		if isinstance(content, str):
			return content
	except (AttributeError, LookupError, UnicodeError):
		pass
	payload = part.get_payload(decode=True)
	if not isinstance(payload, bytes):
		return ""
	charset = part.get_content_charset() or "utf-8"
	try:
		return payload.decode(charset, errors="replace")
	except LookupError:
		return payload.decode("utf-8", errors="replace")


def message_body(message: Message) -> str:
	plain: list[str] = []
	html_parts: list[str] = []
	parts = message.walk() if message.is_multipart() else (message,)
	for part in parts:
		if part.is_multipart() or part.get_content_disposition() == "attachment":
			continue
		content_type = part.get_content_type()
		if content_type == "text/plain":
			plain.append(decode_part(part))
		elif content_type == "text/html":
			html_parts.append(decode_part(part))
	if plain:
		return "\n\n".join(filter(None, plain)).strip()
	if html_parts:
		text = HTML_TAG_RE.sub(" ", "\n".join(html_parts))
		return " ".join(html.unescape(text).split())
	return ""


def is_mbox(path: Path) -> bool:
	if not path.is_file() or path.suffix in {".msf", ".dat", ".json", ".sqlite", ".sqlite-wal"}:
		return False
	try:
		with path.open("rb") as handle:
			return handle.read(5) == b"From "
	except OSError:
		return False


def extract(profile: Path, ids_path: Path, output: Path) -> dict[str, int]:
	wanted = {
		normalize_message_id(line)
		for line in ids_path.read_text(encoding="utf-8").splitlines()
		if normalize_message_id(line)
	}
	found: dict[str, str] = {}
	mailboxes = [path for path in profile.rglob("*") if is_mbox(path)]
	for index, path in enumerate(mailboxes, 1):
		for message in mailbox.mbox(path, create=False):
			message_id = normalize_message_id(message.get("Message-ID"))
			if message_id in wanted and message_id not in found:
				found[message_id] = message_body(message)
		if index % 50 == 0 or index == len(mailboxes):
			print(
				f"Postfächer {index}/{len(mailboxes)}, Treffer {len(found)}/{len(wanted)}",
				file=sys.stderr,
				flush=True,
			)
		if len(found) == len(wanted):
			break
	with gzip.open(output, "wt", encoding="utf-8", compresslevel=6) as handle:
		json.dump(found, handle, ensure_ascii=False, separators=(",", ":"))
	return {"requested": len(wanted), "matched": len(found), "mailboxes": len(mailboxes)}


def main() -> None:
	parser = argparse.ArgumentParser(description="Exportiert Mailtexte aus einem Thunderbird-mbox-Cache.")
	parser.add_argument("profile", type=Path)
	parser.add_argument("message_ids", type=Path)
	parser.add_argument("output", type=Path)
	args = parser.parse_args()
	print(json.dumps(extract(args.profile, args.message_ids, args.output)), flush=True)


if __name__ == "__main__":
	main()
