"""
YouTube Subtitle Downloader — Streamlit app
============================================
Bot-bypass strategy (in order of attempt):
  1. youtube-transcript-api with injected cookies/proxy
  2. yt-dlp with android_vr client (no cookies needed, bypasses bot checks)
  3. yt-dlp with tv_downgraded client + cookies (authenticated fallback)
  4. yt-dlp with web_safari client + cookies (last resort)

Run:
    pip install streamlit tenacity youtube-transcript-api yt-dlp requests
    streamlit run app.py
"""

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
from youtube_transcript_api.proxies import GenericProxyConfig

logging.basicConfig(level=logging.WARNING)

# ── Language map ───────────────────────────────────────────────────────────────
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

CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600&display=swap');

:root {
    --bg: #0d0d0f;
    --surface: #141416;
    --surface2: #1c1c20;
    --border: #2a2a30;
    --border-bright: #3d3d48;
    --accent: #e8ff47;
    --accent-dim: rgba(232, 255, 71, 0.12);
    --accent-dim2: rgba(232, 255, 71, 0.06);
    --text: #f0f0f0;
    --text-muted: #888;
    --text-faint: #555;
    --red: #ff5c5c;
    --green: #5cffa0;
    --mono: 'DM Mono', monospace;
    --sans: 'DM Sans', sans-serif;
    --radius: 10px;
    --radius-lg: 16px;
}

/* Base */
html, body, [class*="css"] {
    font-family: var(--sans) !important;
    background-color: var(--bg) !important;
    color: var(--text) !important;
}

/* Hide Streamlit chrome */
#MainMenu, footer, header { visibility: hidden; }
.stDeployButton { display: none !important; }

/* Main content area */
.main .block-container {
    padding: 2.5rem 2.5rem 4rem !important;
    max-width: 900px !important;
}

/* ─── HERO HEADER ─── */
.yt-hero {
    display: flex;
    align-items: center;
    gap: 18px;
    margin-bottom: 2.5rem;
    padding-bottom: 2rem;
    border-bottom: 1px solid var(--border);
}
.yt-hero-icon {
    width: 52px;
    height: 52px;
    background: var(--accent);
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 26px;
    flex-shrink: 0;
}
.yt-hero-title {
    font-size: 1.7rem;
    font-weight: 600;
    color: var(--text);
    line-height: 1.1;
    letter-spacing: -0.03em;
    margin: 0;
}
.yt-hero-sub {
    font-size: 0.85rem;
    color: var(--text-muted);
    margin: 3px 0 0;
    font-weight: 300;
}

/* ─── URL INPUT CARD ─── */
.url-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: 1.5rem 1.5rem 1.2rem;
    margin-bottom: 1.5rem;
    transition: border-color 0.2s;
}
.url-card:focus-within {
    border-color: var(--accent);
}

/* ─── SECTION LABELS ─── */
.section-label {
    font-family: var(--mono);
    font-size: 0.7rem;
    font-weight: 500;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--text-faint);
    margin-bottom: 0.6rem;
}

/* ─── BADGE ─── */
.url-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: var(--accent-dim);
    border: 1px solid var(--accent);
    color: var(--accent);
    font-family: var(--mono);
    font-size: 0.72rem;
    padding: 3px 10px;
    border-radius: 20px;
    margin-top: 0.5rem;
    font-weight: 500;
}

/* ─── OPTIONS GRID ─── */
.options-grid {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 1rem;
    margin-bottom: 1.5rem;
}
.option-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1rem 1.1rem 0.9rem;
}
.option-card-label {
    font-family: var(--mono);
    font-size: 0.68rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--text-faint);
    margin-bottom: 0.4rem;
}

/* ─── STREAMLIT INPUT OVERRIDES ─── */
.stTextInput > div > div > input {
    background: var(--surface2) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--radius) !important;
    color: var(--text) !important;
    font-family: var(--mono) !important;
    font-size: 0.9rem !important;
    padding: 0.65rem 1rem !important;
    transition: border-color 0.15s !important;
}
.stTextInput > div > div > input:focus {
    border-color: var(--accent) !important;
    box-shadow: 0 0 0 3px var(--accent-dim) !important;
}
.stTextInput > label {
    font-family: var(--mono) !important;
    font-size: 0.7rem !important;
    letter-spacing: 0.1em !important;
    text-transform: uppercase !important;
    color: var(--text-faint) !important;
}

.stSelectbox > div > div {
    background: var(--surface2) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--radius) !important;
    color: var(--text) !important;
}
.stSelectbox > label {
    font-family: var(--mono) !important;
    font-size: 0.7rem !important;
    letter-spacing: 0.1em !important;
    text-transform: uppercase !important;
    color: var(--text-faint) !important;
}

/* Radio buttons */
.stRadio > label {
    font-family: var(--mono) !important;
    font-size: 0.7rem !important;
    letter-spacing: 0.1em !important;
    text-transform: uppercase !important;
    color: var(--text-faint) !important;
}
.stRadio [data-testid="stMarkdownContainer"] p {
    font-size: 0.85rem !important;
    font-family: var(--sans) !important;
}

/* Checkbox */
.stCheckbox > label > div {
    font-size: 0.85rem !important;
}

/* Slider */
.stSlider > label {
    font-family: var(--mono) !important;
    font-size: 0.7rem !important;
    letter-spacing: 0.1em !important;
    text-transform: uppercase !important;
    color: var(--text-faint) !important;
}
.stSlider [data-testid="stThumbValue"] {
    font-family: var(--mono) !important;
    font-size: 0.8rem !important;
    color: var(--accent) !important;
}
[data-testid="stSliderThumb"] {
    background: var(--accent) !important;
    border-color: var(--accent) !important;
}
[data-testid="stSliderTrack"] > div:first-child {
    background: var(--accent) !important;
}

/* ─── PRIMARY BUTTON ─── */
.stButton > button[kind="primary"] {
    background: var(--accent) !important;
    color: #0d0d0f !important;
    border: none !important;
    border-radius: var(--radius) !important;
    font-family: var(--sans) !important;
    font-weight: 600 !important;
    font-size: 0.95rem !important;
    letter-spacing: -0.01em !important;
    padding: 0.75rem 2rem !important;
    transition: all 0.15s ease !important;
    cursor: pointer !important;
}
.stButton > button[kind="primary"]:hover {
    background: #f5ff80 !important;
    transform: translateY(-1px) !important;
    box-shadow: 0 6px 20px rgba(232, 255, 71, 0.3) !important;
}
.stButton > button[kind="primary"]:active {
    transform: translateY(0) !important;
}
.stButton > button[kind="primary"]:disabled {
    background: var(--border) !important;
    color: var(--text-faint) !important;
    box-shadow: none !important;
    transform: none !important;
}

/* Secondary buttons */
.stButton > button[kind="secondary"] {
    background: var(--surface2) !important;
    color: var(--text) !important;
    border: 1px solid var(--border-bright) !important;
    border-radius: var(--radius) !important;
    font-family: var(--sans) !important;
    font-weight: 500 !important;
    transition: all 0.15s !important;
}
.stButton > button[kind="secondary"]:hover {
    border-color: var(--accent) !important;
    color: var(--accent) !important;
}

/* Download button */
.stDownloadButton > button {
    background: var(--surface2) !important;
    color: var(--accent) !important;
    border: 1px solid var(--accent) !important;
    border-radius: var(--radius) !important;
    font-family: var(--sans) !important;
    font-weight: 500 !important;
    padding: 0.6rem 1.4rem !important;
    transition: all 0.15s !important;
    width: 100% !important;
}
.stDownloadButton > button:hover {
    background: var(--accent-dim) !important;
    box-shadow: 0 0 20px var(--accent-dim) !important;
}

/* ─── PROGRESS / STATUS ─── */
.stProgress > div > div > div > div {
    background: var(--accent) !important;
    border-radius: 99px !important;
}
.stProgress > div > div > div {
    background: var(--border) !important;
    border-radius: 99px !important;
    height: 4px !important;
}

/* ─── METRICS ─── */
[data-testid="metric-container"] {
    background: var(--surface) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--radius) !important;
    padding: 1rem 1.2rem !important;
}
[data-testid="metric-container"] [data-testid="stMetricValue"] {
    font-family: var(--mono) !important;
    font-size: 2rem !important;
    font-weight: 500 !important;
    color: var(--text) !important;
}
[data-testid="metric-container"] [data-testid="stMetricLabel"] {
    font-family: var(--mono) !important;
    font-size: 0.72rem !important;
    letter-spacing: 0.08em !important;
    text-transform: uppercase !important;
    color: var(--text-muted) !important;
}

/* ─── ALERTS ─── */
[data-testid="stAlert"] {
    border-radius: var(--radius) !important;
    border: 1px solid !important;
    font-family: var(--sans) !important;
    font-size: 0.88rem !important;
}
[data-testid="stAlert"][data-baseweb="notification"][aria-label*="Error"] {
    background: rgba(255, 92, 92, 0.08) !important;
    border-color: rgba(255, 92, 92, 0.35) !important;
    color: #ffb3b3 !important;
}
[data-testid="stAlert"][data-baseweb="notification"][aria-label*="Info"],
div[data-testid="stInfoMessage"] {
    background: var(--accent-dim2) !important;
    border-color: rgba(232, 255, 71, 0.25) !important;
    color: #c8d8a0 !important;
}
div[data-testid="stSuccessMessage"] {
    background: rgba(92, 255, 160, 0.07) !important;
    border-color: rgba(92, 255, 160, 0.3) !important;
    color: #a0ffcc !important;
}
div[data-testid="stWarningMessage"] {
    background: rgba(255, 180, 50, 0.07) !important;
    border-color: rgba(255, 180, 50, 0.3) !important;
}

/* ─── EXPANDERS ─── */
[data-testid="stExpander"] {
    background: var(--surface) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--radius) !important;
    overflow: hidden !important;
}
[data-testid="stExpander"] summary {
    padding: 0.75rem 1rem !important;
    font-size: 0.85rem !important;
    font-weight: 500 !important;
}
[data-testid="stExpander"] summary:hover {
    background: var(--surface2) !important;
}
[data-testid="stExpander"] [data-testid="stExpanderDetails"] {
    padding: 0.5rem 1rem 1rem !important;
}

/* ─── SIDEBAR ─── */
[data-testid="stSidebar"] {
    background: var(--surface) !important;
    border-right: 1px solid var(--border) !important;
}
[data-testid="stSidebar"] > div:first-child {
    padding: 1.5rem 1.2rem !important;
}
[data-testid="stSidebar"] .stMarkdown h3 {
    font-family: var(--mono) !important;
    font-size: 0.72rem !important;
    letter-spacing: 0.14em !important;
    text-transform: uppercase !important;
    color: var(--text-faint) !important;
    font-weight: 500 !important;
    margin-bottom: 1rem !important;
    padding-bottom: 0.6rem !important;
    border-bottom: 1px solid var(--border) !important;
}

/* File uploader */
[data-testid="stFileUploader"] {
    background: var(--surface2) !important;
    border: 1px dashed var(--border-bright) !important;
    border-radius: var(--radius) !important;
    padding: 0.5rem !important;
    transition: border-color 0.2s !important;
}
[data-testid="stFileUploader"]:hover {
    border-color: var(--accent) !important;
}

/* ─── DIVIDER ─── */
hr {
    border: none !important;
    border-top: 1px solid var(--border) !important;
    margin: 1.5rem 0 !important;
}

/* ─── TOAST ─── */
[data-testid="stToast"] {
    background: var(--surface2) !important;
    border: 1px solid var(--border-bright) !important;
    border-radius: var(--radius) !important;
    color: var(--text) !important;
    font-family: var(--sans) !important;
    font-size: 0.85rem !important;
}

/* ─── SPINNER ─── */
[data-testid="stSpinner"] p {
    font-family: var(--mono) !important;
    font-size: 0.82rem !important;
    color: var(--text-muted) !important;
}

/* ─── CAPTION / CODE ─── */
.stCaption, [data-testid="stCaptionContainer"] {
    color: var(--text-faint) !important;
    font-family: var(--mono) !important;
    font-size: 0.75rem !important;
}
code {
    background: var(--surface2) !important;
    border: 1px solid var(--border) !important;
    border-radius: 4px !important;
    padding: 2px 6px !important;
    font-family: var(--mono) !important;
    font-size: 0.82rem !important;
    color: var(--accent) !important;
}

/* ─── MARKDOWN TEXT ─── */
.stMarkdown p, .stMarkdown li {
    font-size: 0.88rem !important;
    line-height: 1.65 !important;
    color: var(--text-muted) !important;
}
.stMarkdown strong {
    color: var(--text) !important;
    font-weight: 600 !important;
}
.stMarkdown a {
    color: var(--accent) !important;
    text-decoration: none !important;
}
.stMarkdown a:hover {
    text-decoration: underline !important;
}

/* Results section */
.results-header {
    font-family: var(--mono);
    font-size: 0.7rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--text-faint);
    margin-bottom: 1rem;
}

/* Status text */
.stEmpty p {
    font-family: var(--mono) !important;
    font-size: 0.82rem !important;
    color: var(--text-muted) !important;
}
</style>
"""


def format_language_option(code: str) -> str:
    return LANGUAGE_NAMES.get(code, code.upper())


# ── Session / proxy builder ────────────────────────────────────────────────────
def _build_session(
    cookies_file: str | None = None,
    proxy_url: str | None = None,
) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    if cookies_file and os.path.isfile(cookies_file):
        jar = http.cookiejar.MozillaCookieJar(cookies_file)
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies = jar  # type: ignore[assignment]
        except Exception:
            pass
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    return session


# ── URL helpers ────────────────────────────────────────────────────────────────
def is_channel_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        if parse_qs(parsed.query).keys() & {"v", "list"}:
            return False
        return bool(
            re.match(r"^/@[\w\-\.]+$", path)
            or re.match(r"^/c/[\w\-\.]+$", path)
            or re.match(r"^/channel/[\w\-]+$", path)
            or re.match(r"^/user/[\w\-]+$", path)
        )
    except Exception:
        return False


def validate_url(url: str) -> tuple[str, str | None, str]:
    try:
        parsed = urlparse(url)
        if is_channel_url(url):
            return url, None, "channel"

        if parsed.netloc == "youtu.be":
            video_id = parsed.path.lstrip("/")
            playlist_id = parse_qs(parsed.query).get("list", [None])[0]
            if playlist_id and video_id:
                return (
                    f"https://www.youtube.com/playlist?list={playlist_id}",
                    f"https://www.youtube.com/watch?v={video_id}",
                    "both",
                )
            return f"https://www.youtube.com/watch?v={video_id}", None, "video"

        qp = parse_qs(parsed.query)
        video_id = qp.get("v", [None])[0]
        playlist_id = qp.get("list", [None])[0]

        if playlist_id and video_id:
            return (
                f"https://www.youtube.com/playlist?list={playlist_id}",
                f"https://www.youtube.com/watch?v={video_id}",
                "both",
            )
        if playlist_id:
            return f"https://www.youtube.com/playlist?list={playlist_id}", None, "playlist"
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}", None, "video"
        raise ValueError("Invalid YouTube URL. Please provide a video, playlist, or channel URL.")
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Error parsing URL: {e}")


def extract_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc == "youtu.be":
        return parsed.path.lstrip("/")
    return parse_qs(parsed.query).get("v", [None])[0]


# ── Primary path: youtube-transcript-api ──────────────────────────────────────
def get_transcript_api(
    video_id: str,
    format_choice: str = "srt",
    target_lang: str = "en",
    cookies_file: str | None = None,
    proxy_url: str | None = None,
) -> tuple[str, str, bool]:
    session = _build_session(cookies_file, proxy_url)
    proxy_config = GenericProxyConfig(https_url=proxy_url) if proxy_url else None
    api = YouTubeTranscriptApi(http_client=session, proxy_config=proxy_config)

    try:
        transcript_list = api.list(video_id)

        if target_lang == "auto":
            try:
                transcript = transcript_list.find_manually_created_transcript(ALL_LANG_CODES)
            except NoTranscriptFound:
                transcript = transcript_list.find_generated_transcript(ALL_LANG_CODES)
        else:
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
        formatter = WebVTTFormatter() if dl_format == "vtt" else SRTFormatter()
        return formatter.format_transcript(fetched), lang_code, is_auto

    except PoTokenRequired:
        raise ValueError("YouTube requires a PoToken — upload cookies.txt from a logged-in browser.")
    except (RequestBlocked, IpBlocked):
        raise ValueError("Your IP is blocked by YouTube. Upload cookies.txt or enter a proxy URL.")
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


# ── Fallback path: yt-dlp ─────────────────────────────────────────────────────
_YTDLP_CLIENTS_NO_COOKIES = ["android_vr"]
_YTDLP_CLIENTS_WITH_COOKIES = ["tv_downgraded", "web_safari", "mweb"]


def _ydl_opts_for_client(
    client, dl_format, vid_dir, target_lang, cookies_file, proxy_url, debug
) -> dict:
    lang_codes = [target_lang, f"{target_lang}-orig"] if target_lang != "auto" else ["en", "tr"]
    auto_langs = [target_lang] if target_lang != "auto" else ["en", "tr"]
    opts: dict = {
        "writesubtitles": True,
        "writeautomaticsub": True,
        "skip_download": True,
        "outtmpl": os.path.join(vid_dir, "%(title)s.%(ext)s"),
        "quiet": not debug,
        "no_warnings": not debug,
        "verbose": debug,
        "restrict_filenames": True,
        "ignoreerrors": False,
        "subtitlesformat": dl_format,
        "subtitleslangs": lang_codes,
        "automaticsubslangs": auto_langs,
        "extractor_args": {"youtube": {"player_client": [client]}},
    }
    if cookies_file and os.path.isfile(cookies_file):
        opts["cookiefile"] = cookies_file
    if proxy_url:
        opts["proxy"] = proxy_url
    return opts


def get_subtitles_yt_dlp(
    video_url, format_choice, cookies_file, temp_dir,
    target_lang="en", proxy_url=None, debug=False,
) -> tuple[str, str, bool]:
    dl_format = "srt" if format_choice == "txt" else format_choice
    clients_to_try = list(_YTDLP_CLIENTS_NO_COOKIES)
    if cookies_file and os.path.isfile(cookies_file):
        clients_to_try += _YTDLP_CLIENTS_WITH_COOKIES

    last_error = "No subtitle files found"

    for client in clients_to_try:
        vid_dir = os.path.join(temp_dir, uuid.uuid4().hex)
        os.makedirs(vid_dir, exist_ok=True)
        opts = _ydl_opts_for_client(client, dl_format, vid_dir, target_lang, cookies_file, proxy_url, debug)
        try:
            with YoutubeDL(opts) as ydl:
                ydl.download([video_url])

            all_files = glob.glob(os.path.join(vid_dir, "*.srt")) + glob.glob(os.path.join(vid_dir, "*.vtt"))
            preferred = ([f for f in all_files if f".{target_lang}." in f] if target_lang != "auto" else [])
            files = preferred or all_files

            if files:
                sub_path = files[0]
                with open(sub_path, "r", encoding="utf-8") as fh:
                    sub_text = fh.read()
                try:
                    os.remove(sub_path)
                except OSError:
                    pass
                fname = os.path.basename(sub_path)
                lang_match = re.search(r"\.([a-z]{2,5}(?:-[A-Za-z]{2,4})?)\.(?:srt|vtt)$", fname)
                lang_code = (lang_match.group(1) if lang_match else (target_lang if target_lang != "auto" else "unknown"))
                is_auto = "auto" in fname.lower() or sub_path.endswith(".vtt")
                return sub_text, lang_code, is_auto

            last_error = f"No subtitle files written by client '{client}'"
        except Exception as e:
            last_error = str(e)
            if debug:
                st.caption(f"yt-dlp client `{client}` failed: {last_error[:120]}")
            continue

    raise ValueError(last_error)


# ── Info fetchers ──────────────────────────────────────────────────────────────
def _ydl_base_opts(cookies_file, proxy_url) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    }
    if cookies_file and os.path.isfile(cookies_file):
        opts["cookiefile"] = cookies_file
    if proxy_url:
        opts["proxy"] = proxy_url
    return opts


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_channel_info(channel_url, cookies_file=None, proxy_url=None):
    videos_url = channel_url.rstrip("/")
    if not videos_url.endswith("/videos"):
        videos_url += "/videos"
    opts = {**_ydl_base_opts(cookies_file, proxy_url), "extract_flat": True}
    with YoutubeDL(opts) as ydl:
        result = ydl.extract_info(videos_url, download=False)
    entries = result.get("entries", [])
    channel_title = result.get("channel", result.get("title", "channel_subtitles"))
    return [(e["id"], e.get("title", f"video_{i+1}")) for i, e in enumerate(entries) if e and e.get("id")], channel_title


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_info(url, is_playlist=False, cookies_file=None, proxy_url=None):
    if is_playlist:
        opts = {**_ydl_base_opts(cookies_file, proxy_url), "extract_flat": True}
        with YoutubeDL(opts) as ydl:
            result = ydl.extract_info(url, download=False)
        entries = result.get("entries", [])
        return [(e.get("id"), e.get("title", f"video_{i+1}")) for i, e in enumerate(entries) if e and e.get("id")], result.get("title", "playlist_subtitles")
    else:
        video_id = extract_video_id(url)
        if not video_id:
            raise ValueError("Invalid video URL")
        opts = _ydl_base_opts(cookies_file, proxy_url)
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return [(video_id, info.get("title", "video_subtitles"))], info.get("title", "video_subtitles")


# ── Text processing ────────────────────────────────────────────────────────────
def deduplicate_lines(lines):
    seen = None
    out = []
    for line in lines:
        s = line.strip()
        if s != seen:
            out.append(line)
            seen = s
    return out


def convert_srt_to_txt(srt_text: str) -> str:
    lines = srt_text.split("\n")
    txt_lines = []
    i = 0
    if lines and lines[0].startswith("WEBVTT"):
        while i < len(lines) and lines[i].strip():
            i += 1
    while i < len(lines):
        line = lines[i].strip()
        if re.match(r"^\d+$", line):
            i += 1; continue
        if re.match(r"^\d{2}:\d{2}:\d{2}[,\.]\d{3} --> ", line):
            i += 1; continue
        if re.match(r"^\d{2}:\d{2}:\d{2}\.\d{3} --> ", line):
            i += 1; continue
        if line.startswith(("NOTE", "STYLE", "REGION")) or not line:
            i += 1; continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line:
            txt_lines.append(line)
        i += 1
    return "\n".join(deduplicate_lines(txt_lines)) + "\n"


def clean_subtitle_text(text: str) -> str:
    text = re.sub(r"\[Advertisement\].*?\n", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── Output builders ────────────────────────────────────────────────────────────
def combine_subtitles(subtitle_files, output_dir, title, format_choice):
    safe_title = sanitize_filename(title)[:150]
    combined_path = os.path.join(output_dir, f"{safe_title}_combined.{format_choice}")
    cue_index = 1
    with open(combined_path, "w", encoding="utf-8") as out:
        if format_choice == "vtt":
            out.write("WEBVTT\n\n")
        for video_title, sub_text in subtitle_files:
            sep = f"\n\n### {video_title} ###\n\n" if format_choice == "txt" else f"\n\n=== {video_title} ===\n\n"
            out.write(sep)
            if format_choice in ("srt", "vtt"):
                lines = sub_text.split("\n")
                start = 0
                if format_choice == "vtt" and lines and lines[0].startswith("WEBVTT"):
                    while start < len(lines) and lines[start].strip():
                        start += 1
                for raw_line in lines[start:]:
                    if re.match(r"^\d+$", raw_line.strip()):
                        out.write(f"{cue_index}\n"); cue_index += 1
                    else:
                        out.write(raw_line + "\n")
            else:
                out.write(sub_text + "\n")
    return combined_path


def create_zip(subtitle_files, title, format_choice):
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for video_title, sub_text in subtitle_files:
            fname = f"{sanitize_filename(video_title)[:150]}.{format_choice}"
            zf.writestr(fname, sub_text.encode("utf-8"))
    buf.seek(0)
    return buf, f"{sanitize_filename(title)[:150]}_subtitles.zip"


def get_mime_type(fmt: str) -> str:
    return {"srt": "text/plain", "vtt": "text/vtt", "txt": "text/plain"}.get(fmt, "text/plain")


# ── Core download loop ─────────────────────────────────────────────────────────
def _classify_error(combined_err: str) -> str:
    e = combined_err.lower()
    if any(k in e for k in ("potoken", "po_token", "po token")):
        return "PoToken required — upload cookies.txt"
    if any(k in e for k in ("sign in", "bot", "blocked", "ip block")):
        return "Bot-detection block — upload cookies.txt or enter a proxy URL"
    if any(k in e for k in ("age", "age-restricted")):
        return "Age-restricted — upload cookies.txt"
    if "private" in e:
        return "Private video — cannot access"
    if any(k in e for k in ("no captions", "no subtitles", "no transcript", "disabled")):
        return "No subtitles available"
    return combined_err[:160]


def download_subtitles(
    entries, format_choice, temp_dir, progress_bar, status_text,
    clean_transcript, cookies_file=None, proxy_url=None,
    target_lang="en", rate_limit_delay=1.0, debug_mode=False,
):
    subtitle_files = []
    failed_videos = []
    total = len(entries)

    for i, (video_id, video_title) in enumerate(entries):
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        status_text.markdown(
            f"<span style='font-family:var(--mono,monospace);font-size:0.8rem;color:#888'>"
            f"⏳ {i+1}/{total} — {video_title[:70]}…</span>",
            unsafe_allow_html=True,
        )

        sub_text = None
        lang_code = "unknown"
        is_auto = False
        fallback_used = False
        api_error = ""

        try:
            sub_text, lang_code, is_auto = get_transcript_api(
                video_id, format_choice, target_lang,
                cookies_file=cookies_file, proxy_url=proxy_url,
            )
        except Exception as e:
            api_error = str(e)
            if debug_mode:
                st.caption(f"API failed `{video_id}`: {api_error[:150]}")

        if sub_text is None:
            fallback_used = True
            if debug_mode:
                st.caption(f"Trying yt-dlp for `{video_title[:50]}`…")
            try:
                sub_text, lang_code, is_auto = get_subtitles_yt_dlp(
                    video_url, format_choice, cookies_file, temp_dir,
                    target_lang, proxy_url=proxy_url, debug=debug_mode,
                )
            except Exception as e:
                reason = _classify_error(api_error + " " + str(e))
                failed_videos.append((video_title, reason))
                progress_bar.progress((i + 1) / total)
                if total > 3:
                    time.sleep(rate_limit_delay)
                continue

        if clean_transcript:
            sub_text = clean_subtitle_text(sub_text)
        if format_choice == "txt":
            sub_text = convert_srt_to_txt(sub_text)

        subtitle_files.append((video_title, sub_text))

        notes = []
        if is_auto:
            notes.append("auto-generated")
        if fallback_used:
            notes.append("yt-dlp")
        note_str = f" · {', '.join(notes)}" if notes else ""
        st.toast(f"✅ {video_title[:55]} — {format_language_option(lang_code)}{note_str}")

        progress_bar.progress((i + 1) / total)
        if total > 3:
            time.sleep(rate_limit_delay)

    status_text.empty()
    return subtitle_files, failed_videos


# ── Result renderer ────────────────────────────────────────────────────────────
def render_results(subtitle_files, failed_videos, title, format_choice, combine_choice, temp_dir):
    st.divider()
    st.markdown('<div class="results-header">Results</div>', unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    c1.metric("Succeeded", len(subtitle_files))
    c2.metric("Failed", len(failed_videos))

    if failed_videos:
        with st.expander(f"⚠️ {len(failed_videos)} video(s) failed"):
            for vid_title, reason in failed_videos:
                st.markdown(f"- **{vid_title}**  \n  `{reason}`")

    if not subtitle_files:
        st.error("No subtitles were downloaded successfully.")
        return

    mime = get_mime_type(format_choice)
    st.markdown("")

    if combine_choice == "single":
        vid_title, sub_text = subtitle_files[0]
        safe_name = f"{sanitize_filename(vid_title)[:150]}.{format_choice}"
        st.download_button("📥 Download subtitle file", sub_text.encode("utf-8"), safe_name, mime)

    elif combine_choice == "combined":
        combined_path = combine_subtitles(subtitle_files, temp_dir, title, format_choice)
        with open(combined_path, "rb") as fh:
            st.download_button("📥 Download combined file", fh.read(), os.path.basename(combined_path), mime)
    else:
        zip_buf, zip_name = create_zip(subtitle_files, title, format_choice)
        st.download_button("📥 Download ZIP", zip_buf, zip_name, "application/zip")


# ── Streamlit UI ───────────────────────────────────────────────────────────────
def main() -> None:
    st.set_page_config(
        page_title="YT Subtitle Downloader",
        page_icon="🎥",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # Inject custom CSS
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

    # Hero header
    st.markdown("""
    <div class="yt-hero">
        <div class="yt-hero-icon">🎥</div>
        <div>
            <div class="yt-hero-title">Subtitle Downloader</div>
            <div class="yt-hero-sub">Extract subtitles from YouTube videos, playlists &amp; channels</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Sidebar ────────────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("### ⚙️ Settings")

        # Cookies
        with st.expander("🍪 Cookies  (fix bot / age-restricted errors)"):
            st.markdown(
                "**When to upload:**\n"
                "- *Sign in to confirm you're not a bot*\n"
                "- *Age-restricted*\n"
                "- *PoToken required*\n\n"
                "**How to export:**\n"
                "1. Install **Get cookies.txt LOCALLY** (Chrome/Firefox)\n"
                "2. Log in to youtube.com\n"
                "3. Click the extension → **Export**\n"
                "4. Upload the file below\n\n"
                "⚠️ Cookies expire after days — re-export if errors return."
            )
            uploaded_file = st.file_uploader("Upload cookies.txt", type=["txt"])

        # Proxy
        with st.expander("🌐 Proxy  (alternative bot-detection bypass)"):
            st.markdown(
                "Format: `http://user:password@host:port`\n\n"
                "Providers: [Webshare](https://www.webshare.io/), "
                "[Bright Data](https://brightdata.com/), [Oxylabs](https://oxylabs.io/)"
            )
            proxy_url_input = st.text_input(
                "Proxy URL", placeholder="http://user:pass@host:port", label_visibility="collapsed"
            )
            proxy_url: str | None = proxy_url_input.strip() or None

        st.divider()

        format_choice = st.selectbox("Format", ["srt", "vtt", "txt"])

        target_display = st.radio("Language", ["English", "Turkish", "Auto"], horizontal=True)
        target_lang = {"English": "en", "Turkish": "tr", "Auto": "auto"}[target_display]

        clean_transcript = st.checkbox("Clean transcript", value=True, help="Remove ad markers and repeated lines")

        rate_limit_delay = st.slider("Delay between videos (s)", 0.5, 5.0, 1.0, 0.5)
        debug_mode = st.checkbox("Debug mode", value=False)

    # ── Cookies processing ─────────────────────────────────────────────────────
    cookies_path: str | None = None
    if uploaded_file:
        try:
            cookies_bytes = uploaded_file.getvalue()
            if b"youtube.com" not in cookies_bytes and b"NETSCAPE" not in cookies_bytes:
                st.sidebar.warning("⚠️ Doesn't look like a valid YouTube cookies.txt")
            else:
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
                tmp.write(cookies_bytes)
                tmp.flush()
                tmp.close()
                cookies_path = tmp.name
                st.sidebar.success(f"✅ Cookies loaded ({max(1, len(cookies_bytes)//1024)} KB)")
        except Exception as e:
            st.sidebar.error(f"Could not save cookies: {e}")

    # ── URL input ──────────────────────────────────────────────────────────────
    st.markdown('<div class="url-card">', unsafe_allow_html=True)
    url = st.text_input(
        "YouTube URL",
        placeholder="Paste a video, playlist, or channel URL…",
        label_visibility="visible",
    )

    url_type: str | None = None
    combine_choice = "separate"
    download_scope = "Entire Playlist"
    primary_url: str = ""
    secondary_url: str | None = None

    if url:
        try:
            primary_url, secondary_url, url_type = validate_url(url)
            type_icons = {"video": "🎬 Video", "playlist": "📋 Playlist", "channel": "📺 Channel", "both": "🎬 + 📋 Video in Playlist"}
            st.markdown(f'<div class="url-badge">✓ {type_icons.get(url_type, url_type.upper())}</div>', unsafe_allow_html=True)
        except ValueError as ve:
            st.error(str(ve))
            url_type = None

    st.markdown('</div>', unsafe_allow_html=True)

    # ── Channel UI ─────────────────────────────────────────────────────────────
    if url_type == "channel":
        if st.session_state.get("_last_channel_url") != url:
            st.session_state.pop("channel_entries", None)
            st.session_state.pop("channel_title", None)
            st.session_state["_last_channel_url"] = url

        if st.button("🔍 Fetch video list", type="secondary"):
            with st.spinner("Fetching channel video list…"):
                try:
                    fetched_entries, fetched_title = get_channel_info(url, cookies_path, proxy_url)
                    st.session_state["channel_entries"] = fetched_entries
                    st.session_state["channel_title"] = fetched_title
                except Exception as e:
                    st.error(f"Could not fetch channel: {e}")

        channel_entries = st.session_state.get("channel_entries")
        channel_title: str = st.session_state.get("channel_title", "channel")

        if channel_entries:
            st.success(f"Found **{len(channel_entries)} videos** in *{channel_title}*")
            combine_choice = st.radio(
                "Output format",
                ["separate", "combined"],
                format_func=lambda x: (
                    f"📁 Separate — one .{format_choice} per video, bundled as ZIP"
                    if x == "separate"
                    else "📄 Combined — all subtitles in one file"
                ),
            )

    elif url_type in ("playlist", "both"):
        if url_type == "both":
            download_scope = st.selectbox("Download scope", ["Entire Playlist", "Single Video"])
        if url_type == "playlist" or download_scope == "Entire Playlist":
            combine_choice = st.selectbox(
                "Output",
                ["separate", "combined"],
                format_func=lambda x: "📁 Separate files (ZIP)" if x == "separate" else "📄 Combined file",
            )

    # ── Download button ────────────────────────────────────────────────────────
    st.markdown("")
    btn_disabled = not url or url_type is None
    if st.button("⬇️  Download Subtitles", type="primary", disabled=btn_disabled, use_container_width=False):
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                if url_type == "channel":
                    if not st.session_state.get("channel_entries"):
                        st.error("Please click **Fetch video list** first.")
                    else:
                        ch_entries = st.session_state["channel_entries"]
                        ch_title = st.session_state["channel_title"]
                        pb = st.progress(0.0)
                        st_text = st.empty()
                        sub_files, fails = download_subtitles(
                            ch_entries, format_choice, temp_dir, pb, st_text,
                            clean_transcript, cookies_path, proxy_url,
                            target_lang, rate_limit_delay, debug_mode,
                        )
                        render_results(sub_files, fails, ch_title, format_choice, combine_choice, temp_dir)
                else:
                    if url_type == "both" and download_scope == "Single Video":
                        selected_url, is_playlist = secondary_url, False
                    elif url_type in ("playlist", "both"):
                        selected_url, is_playlist = primary_url, True
                    else:
                        selected_url, is_playlist = primary_url, False

                    with st.spinner("Fetching video info…"):
                        entries, collection_title = get_info(selected_url, is_playlist, cookies_path, proxy_url)

                    if not entries:
                        st.error("No videos found.")
                    else:
                        pb = st.progress(0.0)
                        st_text = st.empty()
                        sub_files, fails = download_subtitles(
                            entries, format_choice, temp_dir, pb, st_text,
                            clean_transcript, cookies_path, proxy_url,
                            target_lang, rate_limit_delay, debug_mode,
                        )
                        effective_combine = combine_choice if is_playlist else "single"
                        render_results(sub_files, fails, collection_title, format_choice, effective_combine, temp_dir)

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
