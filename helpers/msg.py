import re
from pyrogram.utils import get_channel_id
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def parse_link(link: str):
    """
    Parse any Telegram post link and return (chat_id, message_id, topic_id).

    Supported formats:
      Public channel/group:
        https://t.me/username/123
        https://t.me/username/topic_id/123     ← group topic
      Private channel/group:
        https://t.me/c/1234567890/123
        https://t.me/c/1234567890/topic_id/123 ← private group topic
    """
    if not link:
        raise ValueError("Empty link.")

    link = link.strip().rstrip("/")
    if "?" in link:
        link = link.split("?")[0]

    parts = link.split("/")
    # parts[0] = 'https:', [1] = '', [2] = 't.me', [3+] = path segments
    path = parts[3:]  # everything after t.me/

    chat_id = None
    message_id = None
    topic_id = None

    try:
        if path[0] == "c":
            # Private: t.me/c/<channel_id>/[<topic_id>/]<msg_id>
            raw_channel = int(path[1])
            chat_id = get_channel_id(raw_channel)
            if len(path) == 4:
                # t.me/c/CID/TOPIC/MSGID
                topic_id = int(path[2])
                message_id = int(path[3])
            elif len(path) == 3:
                # t.me/c/CID/MSGID
                message_id = int(path[2])
            else:
                raise ValueError("Unrecognised private link format.")
        else:
            # Public: t.me/<username>/[<topic_id>/]<msg_id>
            chat_id = path[0]
            if len(path) == 3:
                # t.me/USERNAME/TOPIC/MSGID
                topic_id = int(path[1])
                message_id = int(path[2])
            elif len(path) == 2:
                # t.me/USERNAME/MSGID
                message_id = int(path[1])
            else:
                raise ValueError("Unrecognised public link format.")
    except (IndexError, ValueError, TypeError):
        raise ValueError(f"Could not parse link: {link}")

    if not chat_id or not message_id:
        raise ValueError("Link must end with a numeric message ID.")

    return chat_id, message_id, topic_id


def parse_range_input(text: str):
    """
    Parse user input that may be:
      - A single link
      - Two links separated by ' - ' or newline (range)

    Returns list of (chat_id, start_id, end_id, topic_id) tuples.
    Each item in the list = one job.
    """
    text = text.strip()

    # Try range split: "link1 - link2" or "link1\nlink2" when only 2 lines
    # We'll handle multiline playlist separately in the plugin.
    # Here we handle a single entry that may itself be a range.
    separators = [" - ", "\n"]
    parts = None
    for sep in separators:
        if sep in text:
            split = [x.strip() for x in text.split(sep, 1) if x.strip()]
            if len(split) == 2 and "t.me" in split[0] and "t.me" in split[1]:
                parts = split
                break

    if parts:
        link_a, link_b = parts
        chat_a, id_a, topic_a = parse_link(link_a)
        chat_b, id_b, topic_b = parse_link(link_b)

        if str(chat_a) != str(chat_b):
            raise ValueError("Both links must be from the same chat.")
        if topic_a != topic_b:
            raise ValueError("Both links must be from the same topic thread.")
        if id_a > id_b:
            raise ValueError("Start message ID must be less than end message ID.")

        return [(chat_a, id_a, id_b, topic_a)]
    else:
        # Single link
        chat_id, message_id, topic_id = parse_link(text)
        return [(chat_id, message_id, message_id, topic_id)]


def parse_playlist_input(text: str):
    """
    Parse a multiline playlist message.
    Each line is either:
      - a single link
      - a range: link1 - link2
    Returns list of (chat_id, start_id, end_id, topic_id).
    """
    jobs = []
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    errors = []

    for line in lines:
        try:
            jobs.extend(parse_range_input(line))
        except Exception as e:
            errors.append(f"⚠️ Skipped `{line[:50]}`: {e}")

    return jobs, errors


async def get_parsed_msg(chat_msg) -> str:
    if chat_msg.caption:
        return chat_msg.caption.html
    elif chat_msg.text:
        return chat_msg.text.html
    return ""


def clean_caption(caption: str) -> str:
    if not caption:
        return ""
    caption = re.sub(r'(?<!href=["\'])@([a-zA-Z0-9_]+)', r'(at)\1', caption)

    def defang(match):
        url = match.group(0)
        if re.search(r'(?:t\.me|telegram\.me)/.+/\d+', url, re.IGNORECASE):
            return url
        return url.replace('.', '(dot)')

    pattern = r'(?<!href=["\'])(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|chat\.whatsapp\.com)\S+'
    caption = re.sub(pattern, defang, caption, flags=re.IGNORECASE)
    return caption.strip()


def apply_caption_rules(caption: str, rules: list) -> str:
    if not caption:
        return ""
    for rule in rules:
        if rule == "keep":
            continue
        lines = caption.replace('\r', '').split('\n')
        if rule == "rm_last":
            for i in range(len(lines) - 1, -1, -1):
                if re.search(r'[a-zA-Z0-9]', re.sub(r'<[^>]+>', '', lines[i])):
                    caption = '\n'.join(lines[:i]).strip()
                    break
        elif rule.startswith("remove_text:"):
            txt = rule.split("remove_text:", 1)[1]
            caption = caption.replace(txt, "")
            caption = re.sub(r'[ \t]{2,}', ' ', caption).strip()
    return caption.strip()


def get_file_name(message_id: int, chat_message) -> str:
    if chat_message.document:
        return chat_message.document.file_name or str(message_id)
    elif chat_message.video:
        return chat_message.video.file_name or f"{message_id}.mp4"
    elif chat_message.audio:
        return chat_message.audio.file_name or f"{message_id}.mp3"
    elif chat_message.voice:
        return f"{message_id}.ogg"
    elif chat_message.video_note:
        return f"{message_id}.mp4"
    elif chat_message.animation:
        return chat_message.animation.file_name or f"{message_id}.gif"
    elif chat_message.sticker:
        return f"{message_id}.webp"
    elif chat_message.photo:
        return f"{message_id}.jpg"
    return str(message_id)
