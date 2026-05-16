import streamlit as st
import os
import zipfile
import re
import glob
import time
import http.cookiejar
import uuid
import tempfile
import logging
from io import BytesIO
from urllib.parse import urlparse, parse_qs

import requests
from tenacity import retry, stop_after_attempt, wait_exponential
from yt_dlp import YoutubeDL
from yt_dlp.utils import sanitize_filename

from youtube_transcript_api import (
    YouTubeTranscriptApi,
    NoTranscriptFound,
    CouldNotRetrieveTranscript,
)
from youtube_transcript_api._errors import (
    RequestBlocked,
    IpBlocked,
    VideoUnavailable,
    AgeRestricted,
    TranscriptsDisabled,
    PoTokenRequired,
    YouTubeRequestFailed,
)
from youtube_transcript_api.formatters import SRTFormatter, WebVTTFormatter

logging.basicConfig(level=logging.WARNING)

# ── Language map ──────────────────────────────────────────────────────────────
LANGUAGE_NAMES = {
    "en": "English",
    "tr": "Türkçe (Turkish)",
    "es": "Español (Spanish)",
    "fr": "Français (French)",
    "de": "Deutsch (German)",
    "it": "Italiano (Italian)",
    "pt": "Português (Portuguese)",
    "ru": "Русский (Russian)",
    "ja": "日本語 (Japanese)",
    "ko": "한국어 (Korean)",
    "zh-Hans": "中文简体 (Chinese Simplified)",
    "zh-Hant": "中文繁體 (Chinese Traditional)",
    "ar": "العربية (Arabic)",
    "hi": "हिन्दी (Hindi)",
    "nl": "Nederlands (Dutch)",
    "pl": "Polski (Polish)",
    "sv": "Svenska (Swedish)",
    "no": "Norsk (Norwegian)",
    "da": "Dansk (Danish)",
    "fi": "Suomi (Finnish)",
    "cs": "Čeština (Czech)",
    "el": "Ελληνικά (Greek)",
    "he": "עברית (Hebrew)",
    "id": "Bahasa Indonesia (Indonesian)",
    "th": "ไทย (Thai)",
    "vi": "Tiếng Việt (Vietnamese)",
    "uk": "Українська (Ukrainian)",
    "ro": "Română (Romanian)",
    "hu": "Magyar (Hungarian)",
    "bg": "Български (Bulgarian)",
    "sr": "Српски (Serbian)",
    "hr": "Hrvatski (Croatian)",
    "sk": "Slovenčina (Slovak)",
    "ca": "Català (Catalan)",
}

ALL_LANG_CODES = list(LANGUAGE_NAMES.keys())


def format_language_option(code: str) -> str:
    return LANGUAGE_NAMES.get(code, code.upper())


# ── Session builder ───────────────────────────────────────────────────────────
def _build_session(cookies_file: str | None = None) -> requests.Session:
    """
    Build a requests.Session with a browser-like User-Agent.
    If cookies_file is provided (Netscape/Mozilla format), load it into the session.

    NOTE: YouTubeTranscriptApi v1.2.4 disabled cookie_path= in the constructor,
    but it still accepts an http_client= Session, so we inject cookies this way.
    """
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    if cookies_file and os.path.isfile(cookies_file):
        jar = http.cookiejar.MozillaCookieJar(cookies_file)
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies = jar  # type: ignore[assignment]
        except Exception:
            pass  # malformed file — proceed without cookies
    return session


# ── URL helpers ───────────────────────────────────────────────────────────────
def is_channel_url(url: str) -> bool:
    """Return True if the URL points to a YouTube channel (not a video/playlist)."""
    try:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        query_params = parse_qs(parsed.query)
        if "v" in query_params or "list" in query_params:
            return False
        patterns = [
            r"^/@[\w\-\.]+$",
            r"^/c/[\w\-\.]+$",
            r"^/channel/[\w\-]+$",
            r"^/user/[\w\-]+$",
        ]
        return any(re.match(p, path) for p in patterns)
    except Exception:
        return False


def validate_url(url: str) -> tuple[str, str | None, str]:
    """
    Classify URL and normalise it.
    Returns (primary_url, secondary_url, url_type).
    url_type in {'video', 'playlist', 'both', 'channel'}
    """
    try:
        parsed = urlparse(url)

        if is_channel_url(url):
            return url, None, "channel"

        if parsed.netloc == "youtu.be":
            video_id = parsed.path.lstrip("/")
            query_params = parse_qs(parsed.query)
            playlist_id = query_params.get("list", [None])[0]
            if playlist_id and video_id:
                return (
                    f"https://www.youtube.com/playlist?list={playlist_id}",
                    f"https://www.youtube.com/watch?v={video_id}",
                    "both",
                )
            elif video_id:
                return f"https://www.youtube.com/watch?v={video_id}", None, "video"

        query_params = parse_qs(parsed.query)
        video_id = query_params.get("v", [None])[0]
        playlist_id = query_params.get("list", [None])[0]

        if playlist_id and video_id:
            return (
                f"https://www.youtube.com/playlist?list={playlist_id}",
                f"https://www.youtube.com/watch?v={video_id}",
                "both",
            )
        elif playlist_id:
            return f"https://www.youtube.com/playlist?list={playlist_id}", None, "playlist"
        elif video_id:
            return f"https://www.youtube.com/watch?v={video_id}", None, "video"
        else:
            raise ValueError("Invalid YouTube URL. Please provide a video, playlist, or channel URL.")
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Error parsing URL: {str(e)}")


def extract_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc == "youtu.be":
        return parsed.path.lstrip("/")
    return parse_qs(parsed.query).get("v", [None])[0]


# ── Transcript fetcher — primary path ─────────────────────────────────────────
def get_transcript_api(
    video_id: str,
    format_choice: str = "srt",
    target_lang: str = "en",
    cookies_file: str | None = None,
) -> tuple[str, str, bool]:
    """
    Fetch transcript via youtube-transcript-api (v1.2.4, instance-based API).

    Fixes vs original code
    ──────────────────────
    1. Injects cookies through http_client= (cookie_path= is disabled in v1.2.4).
    2. Calls api.list() BEFORE the try/except so transcript_list is always defined.
    3. Catches all known error subclasses (RequestBlocked, IpBlocked, AgeRestricted,
       TranscriptsDisabled, PoTokenRequired, VideoUnavailable) for clear messages.
    4. Returns (sub_text, lang_code, is_auto) consistently.
    """
    session = _build_session(cookies_file)
    api = YouTubeTranscriptApi(http_client=session)

    try:
        # Always fetch the full list first — fixes the NameError in the original
        transcript_list = api.list(video_id)

        if target_lang == "auto":
            # Prefer manually created, fall back to auto-generated
            try:
                transcript = transcript_list.find_manually_created_transcript(ALL_LANG_CODES)
            except NoTranscriptFound:
                transcript = transcript_list.find_generated_transcript(ALL_LANG_CODES)
        else:
            # Try manual in requested language → generated in requested language
            # → manual in any language → generated in any language
            try:
                transcript = transcript_list.find_manually_created_transcript([target_lang])
            except NoTranscriptFound:
                try:
                    transcript = transcript_list.find_generated_transcript([target_lang])
                except NoTranscriptFound:
                    try:
                        transcript = transcript_list.find_manually_created_transcript(ALL_LANG_CODES)
                    except NoTranscriptFound:
                        transcript = transcript_list.find_generated_transcript(ALL_LANG_CODES)

        fetched = transcript.fetch()
        lang_code: str = fetched.language_code
        is_auto: bool = fetched.is_generated

        dl_format = "srt" if format_choice == "txt" else format_choice
        formatter: SRTFormatter | WebVTTFormatter = (
            WebVTTFormatter() if dl_format == "vtt" else SRTFormatter()
        )
        sub_text: str = formatter.format_transcript(fetched)
        return sub_text, lang_code, is_auto

    # ── Specific, informative error messages ──────────────────────────────────
    except PoTokenRequired:
        raise ValueError("YouTube requires a PoToken — upload cookies.txt from a logged-in browser.")
    except (RequestBlocked, IpBlocked):
        raise ValueError("Your IP is blocked by YouTube. Upload valid cookies.txt or use a proxy.")
    except AgeRestricted:
        raise ValueError("Age-restricted video — upload cookies.txt from a logged-in adult account.")
    except TranscriptsDisabled:
        raise ValueError("Subtitles are disabled for this video.")
    except VideoUnavailable:
        raise ValueError("Video is unavailable (private, deleted, or region-locked).")
    except NoTranscriptFound:
        raise ValueError("No subtitles found in any language for this video.")
    except CouldNotRetrieveTranscript as e:
        raise ValueError(f"Could not retrieve transcript: {e}")
    except Exception as e:
        raise ValueError(f"API error: {e}")


# ── Transcript fetcher — fallback path ────────────────────────────────────────
def get_subtitles_yt_dlp(
    video_url: str,
    format_choice: str,
    cookies_file: str | None,
    temp_dir: str,
    target_lang: str = "en",
    debug: bool = False,
) -> tuple[str, str, bool]:
    """
    Fallback: download subtitles with yt-dlp.

    Fixes vs original code
    ──────────────────────
    1. Uses a per-video subdirectory (uuid) so glob never picks up stale files
       from a previous video in the same batch.
    2. Skips the duplicate glob patterns — picks any .srt/.vtt file cleanly.
    3. Passes cookies_file only when it exists and is not None.
    """
    dl_format = "srt" if format_choice == "txt" else format_choice

    vid_dir = os.path.join(temp_dir, uuid.uuid4().hex)
    os.makedirs(vid_dir, exist_ok=True)

    lang_codes = (
        [target_lang, f"{target_lang}-orig"] if target_lang != "auto" else ["en", "tr", "en-orig"]
    )
    auto_lang_codes = (
        [target_lang] if target_lang != "auto" else ["en", "tr"]
    )

    ydl_opts: dict = {
        "writesubtitles": True,
        "writeautomaticsub": True,
        "skip_download": True,
        "outtmpl": os.path.join(vid_dir, "%(title)s.%(ext)s"),
        "quiet": not debug,
        "no_warnings": not debug,
        "verbose": debug,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "restrict_filenames": True,
        "ignoreerrors": False,
        "subtitlesformat": dl_format,
        "subtitleslangs": lang_codes,
        "automaticsubslangs": auto_lang_codes,
    }
    if cookies_file and os.path.isfile(cookies_file):
        ydl_opts["cookiefile"] = cookies_file

    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])

    # Collect any subtitle file written to the per-video dir
    all_files = (
        glob.glob(os.path.join(vid_dir, "*.srt"))
        + glob.glob(os.path.join(vid_dir, "*.vtt"))
    )

    preferred = (
        [f for f in all_files if f".{target_lang}." in f]
        if target_lang != "auto"
        else []
    )
    files = preferred or all_files

    if not files:
        raise ValueError(
            "No subtitle files found via yt-dlp — "
            "the video may have no captions or requires authentication."
        )

    sub_path = files[0]
    with open(sub_path, "r", encoding="utf-8") as fh:
        sub_text = fh.read()
    try:
        os.remove(sub_path)
    except OSError:
        pass

    # Detect language code from filename, e.g. "Title.en.srt" → "en"
    fname = os.path.basename(sub_path)
    lang_match = re.search(r"\.([a-z]{2,5}(?:-[A-Za-z]{2,4})?)\.(?:srt|vtt)$", fname)
    lang_code = (
        lang_match.group(1)
        if lang_match
        else (target_lang if target_lang != "auto" else "unknown")
    )
    is_auto = "auto" in fname.lower() or sub_path.endswith(".vtt")
    return sub_text, lang_code, is_auto


# ── Info fetchers ─────────────────────────────────────────────────────────────
def _ydl_opts_base(cookies_file: str | None) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    if cookies_file and os.path.isfile(cookies_file):
        opts["cookiefile"] = cookies_file
    return opts


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_channel_info(
    channel_url: str, cookies_file: str | None = None
) -> tuple[list[tuple[str, str]], str]:
    """Return ([(video_id, title), ...], channel_title) for a channel URL."""
    videos_url = channel_url.rstrip("/")
    if not videos_url.endswith("/videos"):
        videos_url += "/videos"

    opts = {**_ydl_opts_base(cookies_file), "extract_flat": True}
    with YoutubeDL(opts) as ydl:
        result = ydl.extract_info(videos_url, download=False)

    entries = result.get("entries", [])
    channel_title = result.get("channel", result.get("title", "channel_subtitles"))
    video_pairs = [
        (entry["id"], entry.get("title", f"video_{i + 1}"))
        for i, entry in enumerate(entries)
        if entry and entry.get("id")
    ]
    return video_pairs, channel_title


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_info(
    url: str, is_playlist: bool = False, cookies_file: str | None = None
) -> tuple[list[tuple[str, str]], str]:
    """Return ([(video_id, title), ...], collection_title) for a video or playlist."""
    if is_playlist:
        opts = {**_ydl_opts_base(cookies_file), "extract_flat": True}
        with YoutubeDL(opts) as ydl:
            result = ydl.extract_info(url, download=False)
        entries = result.get("entries", [])
        pairs = [
            (e.get("id"), e.get("title", f"video_{i + 1}"))
            for i, e in enumerate(entries)
            if e and e.get("id")
        ]
        return pairs, result.get("title", "playlist_subtitles")
    else:
        video_id = extract_video_id(url)
        if not video_id:
            raise ValueError("Invalid video URL")
        opts = _ydl_opts_base(cookies_file)
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return [(video_id, info.get("title", "video_subtitles"))], info.get("title", "video_subtitles")


# ── Text processing ───────────────────────────────────────────────────────────
def deduplicate_lines(lines: list[str]) -> list[str]:
    """Remove consecutive duplicate lines — common in auto-generated captions."""
    seen = None
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped != seen:
            out.append(line)
            seen = stripped
    return out


def convert_srt_to_txt(srt_text: str) -> str:
    """
    Strip sequence numbers, timestamps, and inline tags.
    Handles both SRT and VTT input.
    Deduplicates repeated auto-caption lines.
    """
    lines = srt_text.split("\n")
    txt_lines: list[str] = []
    i = 0

    # Skip VTT header block
    if lines and lines[0].startswith("WEBVTT"):
        while i < len(lines) and lines[i].strip():
            i += 1

    while i < len(lines):
        line = lines[i].strip()

        # Cue sequence number
        if re.match(r"^\d+$", line):
            i += 1
            continue
        # SRT timestamp line
        if re.match(r"^\d{2}:\d{2}:\d{2}[,\.]\d{3} --> ", line):
            i += 1
            continue
        # VTT timestamp line (may include cue settings)
        if re.match(r"^\d{2}:\d{2}:\d{2}\.\d{3} --> ", line):
            i += 1
            continue
        # VTT metadata blocks and blank lines
        if line.startswith(("NOTE", "STYLE", "REGION")) or not line:
            i += 1
            continue

        # Strip all inline HTML/VTT tags  e.g. <c>, <b>, <00:00:01.000>
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line:
            txt_lines.append(line)
        i += 1

    txt_lines = deduplicate_lines(txt_lines)
    return "\n".join(txt_lines) + "\n"


def clean_subtitle_text(text: str) -> str:
    text = re.sub(r"\[Advertisement\].*?\n", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── Output builders ───────────────────────────────────────────────────────────
def combine_subtitles(
    subtitle_files: list[tuple[str, str]],
    output_dir: str,
    title: str,
    format_choice: str,
) -> str:
    """
    Merge all subtitles into one file with per-video section headers.
    VTT: single WEBVTT header at the top; per-chunk headers are stripped.
    SRT: cue numbers are renumbered sequentially across all chunks.
    """
    safe_title = sanitize_filename(title)[:150]
    combined_path = os.path.join(output_dir, f"{safe_title}_combined.{format_choice}")
    cue_index = 1

    with open(combined_path, "w", encoding="utf-8") as out:
        if format_choice == "vtt":
            out.write("WEBVTT\n\n")

        for video_title, sub_text in subtitle_files:
            sep = (
                f"\n\n### {video_title} ###\n\n"
                if format_choice == "txt"
                else f"\n\n=== {video_title} ===\n\n"
            )
            out.write(sep)

            if format_choice in ("srt", "vtt"):
                lines = sub_text.split("\n")
                start = 0
                # Strip embedded WEBVTT header from each chunk
                if format_choice == "vtt" and lines and lines[0].startswith("WEBVTT"):
                    while start < len(lines) and lines[start].strip():
                        start += 1
                i = start
                while i < len(lines):
                    line = lines[i].strip()
                    if re.match(r"^\d+$", line):
                        out.write(f"{cue_index}\n")
                        cue_index += 1
                    else:
                        out.write(lines[i] + "\n")
                    i += 1
            else:
                out.write(sub_text + "\n")

    return combined_path


def create_zip(
    subtitle_files: list[tuple[str, str]], title: str, format_choice: str
) -> tuple[BytesIO, str]:
    zip_buffer = BytesIO()
    safe_title = sanitize_filename(title)[:150]
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for video_title, sub_text in subtitle_files:
            filename = f"{sanitize_filename(video_title)[:150]}.{format_choice}"
            zf.writestr(filename, sub_text.encode("utf-8"))
    zip_buffer.seek(0)
    return zip_buffer, f"{safe_title}_subtitles.zip"


def get_mime_type(format_choice: str) -> str:
    return {"srt": "text/plain", "vtt": "text/vtt", "txt": "text/plain"}.get(
        format_choice, "text/plain"
    )


# ── Core download loop ────────────────────────────────────────────────────────
def download_subtitles(
    entries: list[tuple[str, str]],
    format_choice: str,
    temp_dir: str,
    progress_bar,
    status_text,
    clean_transcript: bool,
    cookies_file: str | None = None,
    target_lang: str = "en",
    rate_limit_delay: float = 1.0,
    debug_mode: bool = False,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """
    Download subtitles for a list of (video_id, video_title) entries.

    Strategy
    ────────
    1. Try youtube-transcript-api (fast, no yt-dlp overhead).
       Cookies are injected via a custom requests.Session.
    2. If that fails, fall back to yt-dlp (handles more edge-cases,
       uses cookies_file directly).

    Returns
    ───────
    subtitle_files : [(video_title, sub_text), ...]
    failed_videos  : [(video_title, reason), ...]
    """
    subtitle_files: list[tuple[str, str]] = []
    failed_videos: list[tuple[str, str]] = []
    total = len(entries)

    for i, (video_id, video_title) in enumerate(entries):
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        status_text.text(f"⏳ {i + 1}/{total} — {video_title[:70]}…")

        sub_text: str | None = None
        lang_code = "unknown"
        is_auto = False
        fallback_used = False
        api_error = ""

        # ── Step 1: youtube-transcript-api ───────────────────────────────────
        try:
            sub_text, lang_code, is_auto = get_transcript_api(
                video_id, format_choice, target_lang, cookies_file=cookies_file
            )
        except Exception as e:
            api_error = str(e)
            if debug_mode:
                st.caption(f"🔍 API failed for `{video_id}`: {api_error[:150]}")

        # ── Step 2: yt-dlp fallback ──────────────────────────────────────────
        if sub_text is None:
            fallback_used = True
            if debug_mode:
                st.caption(f"🔄 Trying yt-dlp for `{video_title[:50]}`…")
            try:
                sub_text, lang_code, is_auto = get_subtitles_yt_dlp(
                    video_url,
                    format_choice,
                    cookies_file,
                    temp_dir,
                    target_lang,
                    debug=debug_mode,
                )
            except Exception as e:
                ytdlp_error = str(e)
                combined_err = (api_error + " " + ytdlp_error).lower()

                if any(k in combined_err for k in ("sign in", "bot", "blocked", "ip")):
                    reason = "Bot-detection block — upload valid cookies.txt to bypass"
                elif any(k in combined_err for k in ("age", "restricted")):
                    reason = "Age-restricted — upload cookies from a logged-in adult account"
                elif "private" in combined_err:
                    reason = "Private video — cannot access"
                elif "poken" in combined_err:
                    reason = "PoToken required — upload cookies.txt from a logged-in browser"
                elif any(
                    k in combined_err
                    for k in ("no captions", "no subtitles", "no transcript", "disabled")
                ):
                    reason = "No subtitles available for this video"
                else:
                    reason = (
                        f"API: {api_error[:80]} | yt-dlp: {ytdlp_error[:80]}"
                    )
                failed_videos.append((video_title, reason))
                progress_bar.progress((i + 1) / total)
                if total > 3:
                    time.sleep(rate_limit_delay)
                continue

        # ── Step 3: post-process ─────────────────────────────────────────────
        if clean_transcript:
            sub_text = clean_subtitle_text(sub_text)
        if format_choice == "txt":
            sub_text = convert_srt_to_txt(sub_text)

        subtitle_files.append((video_title, sub_text))

        lang_name = format_language_option(lang_code)
        notes = []
        if is_auto:
            notes.append("auto-generated")
        if fallback_used:
            notes.append("yt-dlp")
        note_str = f" · {', '.join(notes)}" if notes else ""
        st.toast(f"✅ {video_title[:55]} — {lang_name}{note_str}")

        progress_bar.progress((i + 1) / total)
        if total > 3:
            time.sleep(rate_limit_delay)

    status_text.empty()
    return subtitle_files, failed_videos


# ── Result renderer ───────────────────────────────────────────────────────────
def render_results(
    subtitle_files: list[tuple[str, str]],
    failed_videos: list[tuple[str, str]],
    title: str,
    format_choice: str,
    combine_choice: str,
    temp_dir: str,
) -> None:
    """Display summary metrics, failure details, and the download button."""
    st.divider()
    c1, c2 = st.columns(2)
    c1.metric("✅ Succeeded", len(subtitle_files))
    c2.metric("❌ Failed", len(failed_videos))

    if failed_videos:
        with st.expander(f"⚠️ {len(failed_videos)} video(s) failed — click for details"):
            for vid_title, reason in failed_videos:
                st.markdown(f"- **{vid_title}**  \n  `{reason}`")

    if not subtitle_files:
        st.error("No subtitles were downloaded successfully.")
        return

    mime = get_mime_type(format_choice)

    if combine_choice == "single":
        vid_title, sub_text = subtitle_files[0]
        safe_name = f"{sanitize_filename(vid_title)[:150]}.{format_choice}"
        st.download_button(
            "📥 Download subtitle file",
            sub_text.encode("utf-8"),
            safe_name,
            mime,
        )
    elif combine_choice == "combined":
        combined_path = combine_subtitles(subtitle_files, temp_dir, title, format_choice)
        with open(combined_path, "rb") as fh:
            st.download_button(
                "📥 Download combined file",
                fh.read(),
                os.path.basename(combined_path),
                mime,
            )
    else:  # "separate" → ZIP
        zip_buffer, zip_name = create_zip(subtitle_files, title, format_choice)
        st.download_button("📥 Download ZIP", zip_buffer, zip_name, "application/zip")


# ── Streamlit UI ──────────────────────────────────────────────────────────────
def main() -> None:
    st.set_page_config(
        page_title="YouTube Subtitle Downloader",
        page_icon="🎥",
        layout="wide",
    )
    st.title("YouTube Subtitle Downloader 🎥")
    st.caption("Download subtitles from YouTube videos, playlists, or entire channels.")

    # ── Sidebar settings ──────────────────────────────────────────────────────
    with st.sidebar:
        st.header("⚙️ Settings")
        url = st.text_input(
            "YouTube URL",
            placeholder="Paste a video, playlist, or channel URL…",
        )

        with st.expander("🍪 Cookies (bypass bot-detection / age restrictions)"):
            st.markdown(
                "**When to use:** Upload cookies if you see:\n"
                "- *'Sign in to confirm you're not a bot'*\n"
                "- *'Age-restricted'*\n"
                "- *'PoToken required'*\n\n"
                "**How to export:**\n"
                "1. Install **Get cookies.txt LOCALLY** (Chrome/Firefox extension).\n"
                "2. Log in to YouTube in the same browser.\n"
                "3. Visit youtube.com, click the extension → export `cookies.txt`.\n"
                "4. Upload below.\n\n"
                "⚠️ Cookies expire — re-export if failures persist after uploading."
            )
            uploaded_file = st.file_uploader("Upload cookies.txt", type=["txt"])

        format_choice = st.selectbox("Subtitle format", ["srt", "vtt", "txt"])
        clean_transcript = st.checkbox("Clean transcript (remove ad markers)", value=True)

        target_display = st.radio(
            "Target language", ["English", "Turkish", "Auto"], horizontal=True
        )
        target_lang = {"English": "en", "Turkish": "tr", "Auto": "auto"}[target_display]

        rate_limit_delay = st.slider(
            "Delay between videos (s)",
            min_value=0.5,
            max_value=5.0,
            value=1.0,
            step=0.5,
            help="Increase to avoid YouTube rate limits on large playlists/channels.",
        )
        debug_mode = st.checkbox(
            "🐛 Debug mode",
            value=False,
            help="Shows per-video API errors and yt-dlp verbose output.",
        )

    # ── Handle cookies upload ─────────────────────────────────────────────────
    cookies_path: str | None = None
    if uploaded_file:
        try:
            cookies_bytes = uploaded_file.getvalue()
            if b"youtube.com" not in cookies_bytes and b"NETSCAPE" not in cookies_bytes:
                st.sidebar.warning(
                    "⚠️ This doesn't look like a valid YouTube cookies.txt file. "
                    "Make sure you exported from youtube.com while logged in."
                )
            else:
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
                tmp.write(cookies_bytes)
                tmp.flush()
                tmp.close()
                cookies_path = tmp.name
                st.sidebar.success(f"✅ Cookies loaded ({max(1, len(cookies_bytes) // 1024)} KB)")
        except Exception as e:
            st.sidebar.error(f"Could not save cookies: {e}")

    # ── URL classification ────────────────────────────────────────────────────
    url_type: str | None = None
    combine_choice = "separate"
    download_scope = "Entire Playlist"
    playlist_url: str = ""
    video_url_clean: str | None = None

    if url:
        try:
            playlist_url, video_url_clean, url_type = validate_url(url)
        except ValueError as ve:
            st.error(str(ve))
            url_type = None

    # ── Channel: two-step UI (fetch list → download) ──────────────────────────
    if url_type == "channel":
        st.info("📺 **Channel URL detected.**")

        # Clear stale cache when URL changes
        if st.session_state.get("_last_channel_url") != url:
            st.session_state.pop("channel_entries", None)
            st.session_state.pop("channel_title", None)
            st.session_state["_last_channel_url"] = url

        if st.button("🔍 Fetch video list"):
            with st.spinner("Fetching channel video list…"):
                try:
                    fetched_entries, fetched_title = get_channel_info(url, cookies_path)
                    st.session_state["channel_entries"] = fetched_entries
                    st.session_state["channel_title"] = fetched_title
                except Exception as e:
                    st.error(f"Could not fetch channel: {e}")

        channel_entries: list[tuple[str, str]] | None = st.session_state.get("channel_entries")
        channel_title: str = st.session_state.get("channel_title", "channel")

        if channel_entries:
            st.success(f"Found **{len(channel_entries)} videos** in *{channel_title}*")
            combine_choice = st.radio(
                "📦 Output format",
                options=["separate", "combined"],
                format_func=lambda x: (
                    f"📁 Separate — one .{format_choice} per video, bundled as ZIP"
                    if x == "separate"
                    else "📄 Combined — all subtitles in one file"
                ),
            )

    # ── Playlist / video sidebar options ──────────────────────────────────────
    elif url_type in ("playlist", "both"):
        with st.sidebar:
            if url_type == "both":
                download_scope = st.selectbox(
                    "Download scope", ["Entire Playlist", "Single Video"]
                )
            if url_type == "playlist" or (
                url_type == "both" and download_scope == "Entire Playlist"
            ):
                combine_choice = st.selectbox("Output", ["separate", "combined"])

    # ── Download button ───────────────────────────────────────────────────────
    st.divider()
    btn_disabled = not url or url_type is None
    if st.button("⬇️ Download Subtitles", type="primary", disabled=btn_disabled):
        try:
            with tempfile.TemporaryDirectory() as temp_dir:

                # Channel ─────────────────────────────────────────────────────
                if url_type == "channel":
                    if not st.session_state.get("channel_entries"):
                        st.error("Please click **Fetch video list** first.")
                    else:
                        ch_entries: list[tuple[str, str]] = st.session_state["channel_entries"]
                        ch_title: str = st.session_state["channel_title"]
                        progress_bar = st.progress(0.0)
                        status_text = st.empty()

                        sub_files, fails = download_subtitles(
                            ch_entries,
                            format_choice,
                            temp_dir,
                            progress_bar,
                            status_text,
                            clean_transcript,
                            cookies_path,
                            target_lang,
                            rate_limit_delay,
                            debug_mode=debug_mode,
                        )
                        render_results(
                            sub_files, fails, ch_title, format_choice, combine_choice, temp_dir
                        )

                # Playlist / single video ─────────────────────────────────────
                else:
                    if url_type == "both" and download_scope == "Single Video":
                        selected_url = video_url_clean
                        is_playlist = False
                    elif url_type in ("playlist", "both"):
                        selected_url = playlist_url
                        is_playlist = True
                    else:
                        selected_url = playlist_url  # video_url stored in playlist_url for 'video' type
                        is_playlist = False

                    with st.spinner("Fetching video info…"):
                        entries, collection_title = get_info(
                            selected_url, is_playlist, cookies_path
                        )

                    if not entries:
                        st.error("No videos found.")
                    else:
                        progress_bar = st.progress(0.0)
                        status_text = st.empty()

                        sub_files, fails = download_subtitles(
                            entries,
                            format_choice,
                            temp_dir,
                            progress_bar,
                            status_text,
                            clean_transcript,
                            cookies_path,
                            target_lang,
                            rate_limit_delay,
                            debug_mode=debug_mode,
                        )
                        effective_combine = combine_choice if is_playlist else "single"
                        render_results(
                            sub_files,
                            fails,
                            collection_title,
                            format_choice,
                            effective_combine,
                            temp_dir,
                        )

        except Exception as e:
            st.error(f"Unexpected error: {e}")
        finally:
            if cookies_path and os.path.exists(cookies_path):
                try:
                    os.unlink(cookies_path)
                except OSError:
                    pass


if __name__ == "__main__":
    main()
