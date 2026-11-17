from typing import Optional

MAX_CHAPTERS = 20
MIN_CHAPTERS = 2
MAX_TITLE_CHARS = 80
MIN_CHAPTER_GAP_SECONDS = 15.0

def normalize_chapters(raw: list[dict], duration: Optional[float]) -> list[dict]:
    """Clean chapter input into ``[{start, title}]``: sorted, inside the video,
    at least ``MIN_CHAPTER_GAP_SECONDS`` apart, first chapter at 0. Returns an
    empty list when fewer than ``MIN_CHAPTERS`` survive."""
    items: list[tuple[float, str]] = []
    for c in raw or []:
        start = c.get('start_seconds', c.get('start')) if isinstance(c, dict) else c.start_seconds
        title = c.get('title') if isinstance(c, dict) else c.title
        try:
            start = float(start)
        except (TypeError, ValueError):
            continue
        if not isinstance(title, str):
            continue
        title = ' '.join(title.split()).strip().strip('"').rstrip('.').strip()
        if not title or start != start or start < 0:
            continue
        if duration and start >= duration:
            continue
        items.append((start, title[:MAX_TITLE_CHARS]))
    items.sort(key=lambda x: x[0])
    out: list[dict] = []
    for start, title in items:
        if out and start - out[-1]['start'] < MIN_CHAPTER_GAP_SECONDS:
            continue
        out.append({'start': round(start, 3), 'title': title})
    if out:
        out[0]['start'] = 0.0
    out = out[:MAX_CHAPTERS]
    return out if len(out) >= MIN_CHAPTERS else []
