import streamlit as st
import os
import zipfile
import re
import glob
import time
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import urllib.request
import urllib.error
from yt_dlp import YoutubeDL
from yt_dlp.utils import sanitize_filename
import tempfile
from io import BytesIO
import logging
from tenacity import retry, stop_after_attempt, wait_exponential
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, CouldNotRetrieveTranscript
from youtube_transcript_api.formatters import SRTFormatter, WebVTTFormatter

logging.basicConfig(level=logging.DEBUG)

LANGUAGE_NAMES = {
    'en': 'English',
    'tr': 'Türkçe (Turkish)',
    'es': 'Español (Spanish)',
    'fr': 'Français (French)',
    'de': 'Deutsch (German)',
    'it': 'Italiano (Italian)',
    'pt': 'Português (Portuguese)',
    'ru': 'Русский (Russian)',
    'ja': '日本語 (Japanese)',
    'ko': '한국어 (Korean)',
    'zh-Hans': '中文简体 (Chinese Simplified)',
    'zh-Hant': '中文繁體 (Chinese Traditional)',
    'ar': 'العربية (Arabic)',
    'hi': 'हिन्दी (Hindi)',
    'nl': 'Nederlands (Dutch)',
    'pl': 'Polski (Polish)',
    'sv': 'Svenska (Swedish)',
    'no': 'Norsk (Norwegian)',
    'da': 'Dansk (Danish)',
    'fi': 'Suomi (Finnish)',
    'cs': 'Čeština (Czech)',
    'el': 'Ελληνικά (Greek)',
    'he': 'עברית (Hebrew)',
    'id': 'Bahasa Indonesia (Indonesian)',
    'th': 'ไทย (Thai)',
    'vi': 'Tiếng Việt (Vietnamese)',
    'uk': 'Українська (Ukrainian)',
    'ro': 'Română (Romanian)',
    'hu': 'Magyar (Hungarian)',
    'bg': 'Български (Bulgarian)',
    'sr': 'Српски (Serbian)',
    'hr': 'Hrvatski (Croatian)',
    'sk': 'Slovenčina (Slovak)',
    'ca': 'Català (Catalan)',
}

def format_language_option(code):
    return LANGUAGE_NAMES.get(code, code.upper() if code else 'Unknown')

def is_channel_url(url):
    try:
        parsed = urlparse(url)
        path = parsed.path.rstrip('/')
        return bool(
            re.match(r'^/@[\w.-]+', path) or
            any(p in path for p in ['/channel/', '/c/', '/user/'])
        )
    except Exception:
        return False

def validate_url(url):
    try:
        parsed_url = urlparse(url)

        if parsed_url.netloc == 'youtu.be':
            video_id = parsed_url.path.lstrip('/')
            query_params = parse_qs(parsed_url.query)
            playlist_id = query_params.get('list', [None])[0]
            if playlist_id and video_id:
                return (f"https://www.youtube.com/playlist?list={playlist_id}",
                        f"https://www.youtube.com/watch?v={video_id}", 'both')
            elif video_id:
                return (f"https://www.youtube.com/watch?v={video_id}", None, 'video')

        # Detect channel URLs: /@handle, /c/name, /channel/UCxxx, /user/name
        path = parsed_url.path.rstrip('/')
        channel_patterns = ['/channel/', '/c/', '/user/']
        is_handle = re.match(r'^/@[\w.-]+', path)
        is_ch = any(p in path for p in channel_patterns)
        if is_handle or is_ch:
            channel_url = url.rstrip('/')
            if not channel_url.endswith('/videos'):
                channel_url += '/videos'
            return (channel_url, None, 'channel')

        query_params = parse_qs(parsed_url.query)
        video_id = query_params.get('v', [None])[0]
        playlist_id = query_params.get('list', [None])[0]

        if playlist_id and video_id:
            return (f"https://www.youtube.com/playlist?list={playlist_id}",
                    f"https://www.youtube.com/watch?v={video_id}", 'both')
        elif playlist_id:
            return (f"https://www.youtube.com/playlist?list={playlist_id}", None, 'playlist')
        elif video_id:
            return (f"https://www.youtube.com/watch?v={video_id}", None, 'video')
        else:
            raise ValueError("Invalid YouTube URL. Please provide a video, playlist, or channel URL.")
    except Exception as e:
        raise ValueError(f"Error parsing URL: {str(e)}")

def extract_video_id(url):
    parsed = urlparse(url)
    if parsed.netloc == 'youtu.be':
        return parsed.path.lstrip('/')
    return parse_qs(parsed.query).get('v', [None])[0]

def get_video_metadata(url, cookies_file=None):
    """Fetch title and channel name for a single video URL."""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'cookiefile': cookies_file,
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        title = info.get('title', 'Unknown Title')
        channel = info.get('channel', info.get('uploader', 'Unknown Channel'))
        return title, channel

def _pick_original_transcript(transcript_list):
    """Prefer a manually-uploaded (creator-added) transcript since that's always in
    the video's native language. Otherwise fall back to the auto-generated transcript,
    which is also always in the video's spoken/original language."""
    manual = [t for t in transcript_list if not t.is_generated]
    if manual:
        return manual[0]
    generated = [t for t in transcript_list if t.is_generated]
    if generated:
        return generated[0]
    raise ValueError("No transcript available for this video.")

def get_transcript_api(video_id, format_choice='srt', mode='original'):
    """
    mode:
      'original'       -> fetch the transcript in the video's native/original language
      'en_translation' -> fetch an English transcript, translating on the fly if needed
    """
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

        if mode == 'en_translation':
            try:
                transcript = transcript_list.find_transcript(['en'])
                is_auto = transcript.is_generated
            except NoTranscriptFound:
                candidates = list(transcript_list)
                translatable = [t for t in candidates if t.is_translatable]
                if not translatable:
                    raise ValueError(
                        "No English transcript exists and none of the available tracks can be machine-translated."
                    )
                # Auto-generated tracks are almost always translatable; manually-uploaded
                # ones often aren't, so prefer a translatable auto-generated track first.
                generated_translatable = [t for t in translatable if t.is_generated]
                base_transcript = generated_translatable[0] if generated_translatable else translatable[0]
                transcript = base_transcript.translate('en')
                is_auto = True
            lang_code = 'en'
        else:  # 'original'
            transcript = _pick_original_transcript(transcript_list)
            lang_code = transcript.language_code
            is_auto = transcript.is_generated

        transcript_data = transcript.fetch()

        if format_choice == 'srt':
            formatter = SRTFormatter()
        elif format_choice == 'vtt':
            formatter = WebVTTFormatter()
        else:
            formatter = SRTFormatter()

        sub_text = formatter.format_transcript(transcript_data)
        return sub_text, lang_code, is_auto

    except CouldNotRetrieveTranscript as e:
        raise ValueError(f"Access denied (age-restricted?): {str(e)}")
    except Exception as e:
        raise ValueError(f"API error: {str(e)}")

def vtt_to_srt(vtt_text):
    """Convert WebVTT cue text into SRT format (numbered cues, comma decimal separator)."""
    body = re.sub(r'^WEBVTT.*?\n', '', vtt_text, count=1, flags=re.DOTALL)
    blocks = re.split(r'\n\s*\n', body.strip())
    time_pattern = re.compile(r'(\d{2}:\d{2}:\d{2})[.,](\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2})[.,](\d{3})')
    srt_blocks = []
    counter = 1
    for block in blocks:
        m = time_pattern.search(block)
        if not m:
            continue
        start = f"{m.group(1)},{m.group(2)}"
        end = f"{m.group(3)},{m.group(4)}"
        text_lines = []
        for line in block.split('\n'):
            if time_pattern.search(line) or not line.strip():
                continue
            if line.strip().upper().startswith(('NOTE', 'STYLE', 'KIND:', 'LANGUAGE:')):
                continue
            line = re.sub(r'<[^>]+>', '', line)  # strip vtt tags like <c> and word timestamps
            text_lines.append(line)
        if text_lines:
            srt_blocks.append(f"{counter}\n{start} --> {end}\n" + '\n'.join(text_lines) + "\n")
            counter += 1
    return '\n'.join(srt_blocks) + '\n'

_LAST_TRANSLATE_REQUEST_TIME = [0.0]
_TRANSLATE_MIN_INTERVAL = 1.5  # seconds enforced between consecutive translate requests,
                                # process-wide, regardless of which loop is calling this

def _pace_translate_requests():
    elapsed = time.monotonic() - _LAST_TRANSLATE_REQUEST_TIME[0]
    if elapsed < _TRANSLATE_MIN_INTERVAL:
        time.sleep(_TRANSLATE_MIN_INTERVAL - elapsed)
    _LAST_TRANSLATE_REQUEST_TIME[0] = time.monotonic()

def _fetch_translated_caption_text(base_url, tlang='en', max_retries=5):
    """Manually request YouTube's on-the-fly caption translation (tlang param) for a
    given caption track URL, bypassing yt-dlp's own list of pre-known languages.
    Paces requests and retries with backoff on HTTP 429 (rate limiting)."""
    parsed = urlparse(base_url)
    qs = parse_qs(parsed.query)
    qs['tlang'] = [tlang]
    qs['fmt'] = ['vtt']
    new_query = urlencode(qs, doseq=True)
    new_url = urlunparse(parsed._replace(query=new_query))
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    for attempt in range(max_retries):
        _pace_translate_requests()
        req = urllib.request.Request(new_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode('utf-8', errors='replace')
            if not raw.strip():
                raise ValueError("YouTube returned an empty translated caption track.")
            return raw
        except urllib.error.HTTPError as e:
            if e.code == 429:
                if attempt < max_retries - 1:
                    backoff = min(2 ** attempt * 3, 45)
                    time.sleep(backoff)
                    continue
                raise ValueError(
                    "YouTube rate-limited the translation requests (HTTP 429) after several "
                    "retries. Try again in a few minutes, or download fewer videos at once."
                )
            raise ValueError(f"HTTP {e.code} fetching translated captions: {e.reason}")
    raise ValueError("Failed to fetch translated captions after multiple retries.")

def get_subtitles_yt_dlp(video_url, format_choice, cookies_file, temp_dir, mode='original'):
    dl_format = 'srt' if format_choice == 'txt' else format_choice
    base_opts = {
        'writesubtitles': True,
        'writeautomaticsub': True,
        'skip_download': True,
        'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'cookiefile': cookies_file,
        'restrict_filenames': True,
        'ignoreerrors': True,
    }

    probe_opts = {'quiet': True, 'no_warnings': True, 'cookiefile': cookies_file, 'skip_download': True}
    with YoutubeDL(probe_opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
    manual = info.get('subtitles') or {}
    auto = info.get('automatic_captions') or {}

    def _download_lang(lang_code):
        _pace_translate_requests()
        ydl_opts = {**base_opts, 'subtitleslangs': [lang_code], 'automaticsubslangs': [lang_code],
                    'subtitlesformat': f'{dl_format}/vtt/srv3/srv1/best'}
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])
        found = []
        for ext in [dl_format, 'vtt', 'ttml', 'srv3', 'srv2', 'srv1', 'json3']:
            found += glob.glob(os.path.join(temp_dir, f'*.{lang_code}.{ext}'))
        return found

    if mode == 'en_translation':
        # 1) If yt-dlp already lists a ready-made 'en' track (native or pre-listed
        #    translation), just grab it - this is the cheap, reliable path.
        if 'en' in manual or 'en' in auto:
            files = _download_lang('en')
            if files:
                sub_path = files[0]
                with open(sub_path, 'r', encoding='utf-8') as f:
                    sub_text = f.read()
                os.remove(sub_path)
                if sub_path.endswith('.vtt') and format_choice != 'txt' and format_choice == 'srt':
                    sub_text = vtt_to_srt(sub_text)
                is_auto = 'en' not in manual
                return sub_text, 'en', is_auto

        # 2) yt-dlp didn't surface an 'en' key - fall back to manually requesting
        #    YouTube's translate-on-demand (tlang=en) on whatever auto-caption track
        #    does exist. This is what actually gets you the "auto-translate to English"
        #    option you see in the YouTube web player, even when yt-dlp doesn't list it.
        if auto:
            base_lang = next(iter(auto.keys()))
            track_list = auto[base_lang]
            base_track_url = next((t.get('url') for t in track_list if t.get('ext') == 'vtt'), None)
            if not base_track_url and track_list:
                base_track_url = track_list[0].get('url')
            if base_track_url:
                vtt_text = _fetch_translated_caption_text(base_track_url, tlang='en')
                sub_text = vtt_to_srt(vtt_text) if format_choice == 'srt' else vtt_text
                return sub_text, 'en', True

        raise ValueError("No English transcript or translatable auto-caption track found for this video.")

    else:  # 'original'
        native_lang = info.get('language')
        lang_code = None
        if native_lang and (native_lang in manual or native_lang in auto):
            lang_code = native_lang
        elif manual:
            lang_code = next(iter(manual.keys()))
        elif auto:
            lang_code = next(iter(auto.keys()))

        if not lang_code:
            raise ValueError("No subtitles found")

        files = _download_lang(lang_code)
        if not files:
            raise ValueError("No subtitles found")

        sub_path = files[0]
        with open(sub_path, 'r', encoding='utf-8') as f:
            sub_text = f.read()
        os.remove(sub_path)
        if sub_path.endswith('.vtt') and format_choice == 'srt':
            sub_text = vtt_to_srt(sub_text)
        is_auto = lang_code not in manual
        return sub_text, lang_code, is_auto



@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_info(url, is_playlist=False, cookies_file=None):
    if is_playlist or is_channel_url(url):
        ydl_opts = {
            'extract_flat': True,
            'quiet': True,
            'no_warnings': True,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'cookiefile': cookies_file,
        }
        with YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(url, download=False)
            entries = result.get('entries', [])
            # Channels can return nested entries (tabs → videos)
            if entries and isinstance(entries[0], dict) and 'entries' in entries[0]:
                entries = entries[0].get('entries', [])
            video_ids = [e.get('id') for e in entries if e and e.get('id')]
            titles = [e.get('title', f'video_{i+1}') for i, e in enumerate(entries) if e and e.get('id')]
            title = result.get('title', 'subtitles')
            return list(zip(video_ids, titles)), title
    else:
        video_id = extract_video_id(url)
        if not video_id:
            raise ValueError("Invalid video URL")
        ydl_opts = {'quiet': True, 'no_warnings': True, 'cookiefile': cookies_file}
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'video_subtitles')
        return [(video_id, title)], title


# sp= values are YouTube's own search "Sort by" filter parameters (captured
# from the live results?...&sp=... URL). None means "let ytsearch use YouTube's
# default relevance ranking" rather than building a filtered results URL.
_SEARCH_SORT_SP = {
    'relevance': None,
    'most_viewed': 'CAMSAhAB',  # Sort by: View count (all-time)
    'newest': 'CAI=',           # Sort by: Upload date (newest first)
}

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_search_info(query, max_results, cookies_file=None, sort_mode='relevance'):
    """Search YouTube for `query` and return the top `max_results` videos as
    a list of (video_id, title) tuples.

    sort_mode:
      'relevance'   -> YouTube's default relevance ranking (yt-dlp's ytsearchN: prefix)
      'most_viewed' -> sorted by all-time view count. This is the closest real
                        equivalent to "trending for this keyword" — YouTube has
                        no trending feed that can be scoped to a search term.
      'newest'      -> sorted by upload date, newest first
    """
    max_results = max(1, min(int(max_results), 500))
    sp_value = _SEARCH_SORT_SP.get(sort_mode)

    ydl_opts = {
        'extract_flat': True,
        'quiet': True,
        'no_warnings': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'cookiefile': cookies_file,
    }

    if sp_value is None:
        # Plain relevance search: yt-dlp's dedicated search extractor paginates
        # on its own until it has N results.
        search_target = f"ytsearch{max_results}:{query}"
    else:
        # Sorted search: hit a real youtube.com/results URL carrying YouTube's
        # own 'sp' sort filter, and cap how many entries get pulled from it.
        params = urlencode({'search_query': query, 'sp': sp_value})
        search_target = f"https://www.youtube.com/results?{params}"
        ydl_opts['playlistend'] = max_results

    with YoutubeDL(ydl_opts) as ydl:
        result = ydl.extract_info(search_target, download=False)
        entries = result.get('entries', []) if result else []
        entries = [e for e in entries if e and e.get('id')][:max_results]
        video_ids = [e.get('id') for e in entries]
        titles = [e.get('title', f'video_{i+1}') for i, e in enumerate(entries)]
        return list(zip(video_ids, titles))


def convert_srt_to_txt(srt_text):
    lines = srt_text.split('\n')
    txt_lines = []
    for line in lines:
        line = line.strip()
        if re.match(r'^\d+$', line):
            continue
        if re.match(r'^\d{2}:\d{2}:\d{2}[,\.]\d{3} --> \d{2}:\d{2}:\d{2}[,\.]\d{3}$', line):
            continue
        if re.match(r'^\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}', line):
            continue
        if line.startswith('NOTE') or line.startswith('WEBVTT') or not line:
            continue
        line = re.sub(r'<[\d:.]+>', '', line)
        line = re.sub(r'</?[cv][^>]*>', '', line)
        if line:
            txt_lines.append(line)
    return '\n'.join(txt_lines) + '\n'

def clean_subtitle_text(text):
    text = re.sub(r'\[Advertisement\].*?\n', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def combine_subtitles(subtitle_files, output_dir, title, format_choice):
    safe_title = sanitize_filename(title)[:150]
    combined_file = os.path.join(output_dir, f"{safe_title}_combined.{format_choice}")
    cue_index = 1
    with open(combined_file, 'w', encoding='utf-8') as outfile:
        for video_title, sub_text in subtitle_files:
            sep = f"\n\n=== {video_title} ===\n\n" if format_choice != 'txt' else f"\n\n### {video_title} ###\n\n"
            outfile.write(sep)
            if format_choice in ['srt', 'vtt']:
                for line in sub_text.split('\n'):
                    if re.match(r'^\d+$', line.strip()):
                        outfile.write(f"{cue_index}\n")
                        cue_index += 1
                    else:
                        outfile.write(line + '\n')
            else:
                outfile.write(sub_text + '\n')
    return combined_file

def create_zip(subtitle_files, title, format_choice):
    zip_buffer = BytesIO()
    safe_title = sanitize_filename(title)[:150]
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for video_title, sub_text in subtitle_files:
            filename = f"{sanitize_filename(video_title)[:150]}.{format_choice}"
            zipf.writestr(filename, sub_text.encode('utf-8'))
    zip_buffer.seek(0)
    return zip_buffer, f"{safe_title}_subtitles.zip"

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=1, max=10))
def download_subtitles(url, format_choice, temp_dir, is_playlist, progress_bar, total_videos,
                       clean_transcript, cookies_file=None, sub_mode='original'):
    subtitle_files = []
    entries, title = get_info(url, is_playlist, cookies_file)
    if not entries:
        st.error("No videos found.")
        return temp_dir, title, subtitle_files

    for i, (video_id, video_title) in enumerate(entries):
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            try:
                sub_text, lang_code, is_auto = get_transcript_api(video_id, format_choice, sub_mode)
                fallback_used = False
            except Exception:
                sub_text, lang_code, is_auto = get_subtitles_yt_dlp(
                    video_url, format_choice, cookies_file, temp_dir, sub_mode)
                fallback_used = True
                if not cookies_file:
                    st.info(f"Used yt-dlp fallback for '{video_title}'")

            if clean_transcript:
                sub_text = clean_subtitle_text(sub_text)
            if format_choice == 'txt':
                sub_text = convert_srt_to_txt(sub_text)

            subtitle_files.append((video_title, sub_text))
            lang_name = format_language_option(lang_code)
            auto_note = ' (Auto-generated)' if is_auto else ''
            source_note = ' (via yt-dlp)' if fallback_used else ''
            st.info(f"✓ '{video_title}' — {lang_name}{auto_note}{source_note}")
        except ValueError as ve:
            error_msg = str(ve).lower()
            if "age-restricted" in error_msg or "access denied" in error_msg:
                st.warning(f"'{video_title}' is age-restricted. Upload cookies to access.")
            else:
                st.warning(f"No subs for '{video_title}': {str(ve)}")
        except Exception as e:
            st.warning(f"Error for '{video_title}': {str(e)}")

        progress_bar.progress((i + 1) / total_videos)

    return temp_dir, title, subtitle_files

def get_mime_type(format_choice):
    return {'srt': 'text/plain', 'vtt': 'text/vtt', 'txt': 'text/plain'}.get(format_choice, 'text/plain')


# ─── Multi-video helpers ──────────────────────────────────────────────────────

def get_multi_video_info(video_url, cookies_file=None):
    """Return (video_id, title, channel) for a single video URL."""
    video_id = extract_video_id(video_url)
    if not video_id:
        raise ValueError(f"Could not extract video ID from: {video_url}")
    ydl_opts = {'quiet': True, 'no_warnings': True, 'cookiefile': cookies_file}
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
        title = info.get('title', 'Unknown Title')
        channel = info.get('channel', info.get('uploader', 'Unknown Channel'))
    return video_id, title, channel


def prepend_video_header(sub_text, video_title, channel_name):
    """Prepend video title and channel name as a header for txt format."""
    header = (
        f"Video: {video_title}\n"
        f"Channel: {channel_name}\n"
        f"{'─' * 60}\n\n"
    )
    return header + sub_text


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    st.set_page_config(page_title="YouTube Subtitle Downloader", page_icon="🎥", layout="wide")

    st.title("🎥 YouTube Subtitle Downloader")
    st.caption("Download subtitles from YouTube videos, playlists, and channels.")

    # =========================================================================
    # Mode selection — single source of truth (no separate tabs + radio)
    # =========================================================================
    mode_options = ["Playlist / Channel", "Single / Multi-Video", "Channel + Keyword", "Keyword Search"]
    if "download_mode" not in st.session_state:
        st.session_state.download_mode = mode_options[0]

    mode = st.radio(
        "What do you want to download?", mode_options, horizontal=True, key="download_mode",
    )

    # Defaults so these names always exist regardless of chosen mode
    url = None
    keyword_channel_url = None
    keyword_filter = None
    keyword_case_sensitive = False
    keyword_combine_choice = "separate"
    multi_combine_choice = "separate"
    search_query = None
    search_max_results = 10
    search_combine_choice = "separate"
    search_sort_mode = "relevance"
    combine_choice = "separate"
    download_scope = "Entire Playlist"
    url_type = None
    playlist_url = None
    video_url_parsed = None
    channel_video_scope = "All Videos"
    channel_range_start = 1
    channel_range_end = 50

    st.markdown("---")

    # ── Playlist / Channel ──────────────────────────────────────────────────
    if mode == "Playlist / Channel":
        url = st.text_input(
            "YouTube URL", placeholder="Paste video, playlist, or channel URL...",
            key="playlist_channel_url",
        )

        if url:
            try:
                playlist_url, video_url_parsed, url_type = validate_url(url)

                if url_type == 'channel':
                    st.info("📺 Channel URL detected — will fetch all uploaded videos.")
                    st.markdown("**Video selection**")
                    channel_video_scope = st.radio(
                        "Which videos to download?",
                        ["All Videos", "Range (oldest → newest)"],
                        key="channel_scope",
                        horizontal=True,
                    )
                    if channel_video_scope == "Range (oldest → newest)":
                        st.caption(
                            "Videos are numbered oldest=1, newest=last. "
                            "Enter the start and end video numbers (inclusive)."
                        )
                        col1, col2 = st.columns(2)
                        with col1:
                            channel_range_start = st.number_input(
                                "From (video #)", min_value=1, value=1, step=1, key="range_start"
                            )
                        with col2:
                            channel_range_end = st.number_input(
                                "To (video #)", min_value=1, value=50, step=1, key="range_end"
                            )
                        if channel_range_start > channel_range_end:
                            st.warning("⚠️ 'From' must be ≤ 'To'.")

                if url_type == 'both':
                    download_scope = st.radio(
                        "Scope", ["Entire Playlist", "Single Video"], key="scope", horizontal=True
                    )

                is_playlist_mode = url_type in ['playlist', 'channel'] or (
                    url_type == 'both' and download_scope == "Entire Playlist")
                if is_playlist_mode:
                    combine_choice = st.radio(
                        "Output", ["separate", "combined"], key="combine", horizontal=True
                    )
            except ValueError as ve:
                st.error(str(ve))

    # ── Single / Multi-Video ────────────────────────────────────────────────
    elif mode == "Single / Multi-Video":
        st.caption("Add one video URL per field. Click **＋ Add video** to add more.")

        if "multi_urls" not in st.session_state:
            st.session_state.multi_urls = [""]

        for idx in range(len(st.session_state.multi_urls)):
            col_inp, col_del = st.columns([9, 1])
            with col_inp:
                st.session_state.multi_urls[idx] = st.text_input(
                    f"Video {idx + 1}",
                    value=st.session_state.multi_urls[idx],
                    placeholder="https://www.youtube.com/watch?v=...",
                    key=f"multi_url_{idx}",
                    label_visibility="collapsed",
                )
            with col_del:
                if len(st.session_state.multi_urls) > 1:
                    if st.button("✕", key=f"del_{idx}", help="Remove this URL"):
                        st.session_state.multi_urls.pop(idx)
                        st.rerun()

        col_add, _ = st.columns([1, 3])
        with col_add:
            if st.button("＋ Add video"):
                st.session_state.multi_urls.append("")
                st.rerun()

        multi_combine_choice = st.radio(
            "Output", ["separate", "combined"], key="multi_combine", horizontal=True
        )

    # ── Channel + Keyword ────────────────────────────────────────────────────
    elif mode == "Channel + Keyword":
        st.caption("Only videos whose title contains the keyword will have their subtitles downloaded.")
        keyword_channel_url = st.text_input(
            "Channel URL", placeholder="https://www.youtube.com/@channelname", key="kw_channel_url"
        )
        keyword_filter = st.text_input(
            "Keyword(s)", placeholder="e.g. interview-lecture-part1", key="kw_filter"
        )
        st.caption(
            "Enter one keyword, or several separated by a dash (-), e.g. "
            "'interview-lecture'. A video is included if its title contains "
            "ANY of the keywords. If one keyword matches nothing, it's just "
            "skipped with a note — it won't stop the others from downloading."
        )
        keyword_case_sensitive = st.checkbox("Case-sensitive match", value=False, key="kw_case")
        keyword_combine_choice = st.radio(
            "Output", ["separate", "combined"], key="keyword_combine", horizontal=True
        )

    # ── Keyword Search ──────────────────────────────────────────────────────
    elif mode == "Keyword Search":
        st.caption(
            "Search all of YouTube for a keyword/phrase and download subtitles "
        )
        search_query = st.text_input(
            "Search keyword(s)", placeholder="e.g. python tutorial for beginners", key="search_query"
        )
        search_sort_display = st.radio(
            "Sort results by",
            ["Relevance", "Most Viewed", "Newest First"],
            horizontal=True,
            key="search_sort",
            help=(
                "Relevance: YouTube's default ranking for the search term.\n\n"
                "Most Viewed: sorted by all-time view count — the closest real "
                "equivalent to a keyword-scoped 'trending' list, since YouTube "
                "doesn't offer a trending feed limited to a search term.\n\n"
                "Newest First: sorted by upload date."
            ),
        )
        search_sort_mode = {
            "Relevance": "relevance",
            "Most Viewed": "most_viewed",
            "Newest First": "newest",
        }[search_sort_display]
        col_n, _ = st.columns([1, 2])
        with col_n:
            search_max_results = st.number_input(
                "Number of videos", min_value=1, max_value=500, value=10, step=1,
                help="Top N YouTube search results (in the chosen sort order) to fetch subtitles for. Maximum 500."
            )
        search_combine_choice = st.radio(
            "Output", ["separate", "combined"], key="search_combine", horizontal=True
        )

    # =========================================================================
    # Shared settings — laid out as horizontal radio bars in the main area
    # =========================================================================
    with st.expander("⚙️ Shared Settings (format, language, cookies)", expanded=True):
        col_a, col_b = st.columns(2)

        with col_a:
            format_choice = st.radio(
                "Subtitle Format", ["srt", "vtt", "txt"], horizontal=True, key="format_choice"
            )
            clean_transcript = st.checkbox("Clean Transcript", value=True, key="clean_transcript")

        with col_b:
            lang_mode_display = st.radio(
                "Subtitle Language", ['Original Language', 'English Translation'],
                horizontal=True, key="lang_mode"
            )
            sub_mode = {'Original Language': 'original', 'English Translation': 'en_translation'}[lang_mode_display]

        st.markdown("---")
        st.markdown(
            "**For Age-Restricted Videos**: Upload cookies to bypass blocks.\n"
            "1. Use the \"Get cookies.txt LOCALLY\" browser extension.\n"
            "2. Log in to YouTube, visit the video.\n"
            "3. Export as `cookies.txt` and upload below."
        )
        uploaded_file = st.file_uploader("Upload Cookies (Optional)", type=["txt"], key="cookies_upload")
        cookies_file = None
        if uploaded_file:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
                tmp.write(uploaded_file.read())
                cookies_file = tmp.name

    # =========================================================================
    # Download button
    # =========================================================================
    st.markdown("---")
    button_text = "⬇️ Download Subtitles"
    if st.button(button_text, type="primary", use_container_width=True):

        # ── Keyword Search mode ─────────────────────────────────────────────────
        if mode == "Keyword Search":
            if not search_query or not search_query.strip():
                st.error("Please enter a search keyword.")
                return

            n_results = int(search_max_results)
            if n_results < 1 or n_results > 500:
                st.error("Number of videos (N) must be between 1 and 500.")
                return

            with st.spinner(f"Searching YouTube for '{search_query}'..."):
                try:
                    entries = get_search_info(search_query.strip(), n_results, cookies_file, search_sort_mode)
                except Exception as e:
                    st.error(f"Search failed: {str(e)}")
                    return

            if not entries:
                st.error("No videos found for that search.")
                return

            total_videos = len(entries)
            sort_label = {"relevance": "relevance", "most_viewed": "most viewed", "newest": "newest"}[search_sort_mode]
            st.write(f"Found **{total_videos}** video(s) for **'{search_query}'** (sorted by {sort_label}). Starting download...")
            progress_bar = st.progress(0.0)

            with tempfile.TemporaryDirectory() as temp_dir:
                subtitle_files = []
                for i, (video_id, video_title) in enumerate(entries):
                    video_url_item = f"https://www.youtube.com/watch?v={video_id}"
                    try:
                        try:
                            sub_text, lang_code, is_auto = get_transcript_api(video_id, format_choice, sub_mode)
                            fallback_used = False
                        except Exception:
                            sub_text, lang_code, is_auto = get_subtitles_yt_dlp(
                                video_url_item, format_choice, cookies_file, temp_dir, sub_mode)
                            fallback_used = True

                        if clean_transcript:
                            sub_text = clean_subtitle_text(sub_text)
                        if format_choice == 'txt':
                            sub_text = convert_srt_to_txt(sub_text)

                        subtitle_files.append((video_title, sub_text))
                        lang_name = format_language_option(lang_code)
                        auto_note = ' (Auto-generated)' if is_auto else ''
                        source_note = ' (via yt-dlp)' if fallback_used else ''
                        st.info(f"✓ '{video_title}' — {lang_name}{auto_note}{source_note}")
                    except ValueError as ve:
                        msg = str(ve).lower()
                        if "age-restricted" in msg or "access denied" in msg:
                            st.warning(f"⚠️ '{video_title}' is age-restricted. Upload cookies to access.")
                        else:
                            st.warning(f"⚠️ No subs for '{video_title}': {str(ve)}")
                    except Exception as e:
                        st.warning(f"⚠️ Error for '{video_title}': {str(e)}")

                    progress_bar.progress((i + 1) / total_videos)

                if not subtitle_files:
                    st.error("Nothing was downloaded.")
                    return

                st.success(f"Done! Got subtitles for {len(subtitle_files)}/{total_videos} video(s).")
                mime_type = get_mime_type(format_choice)
                safe_name = f"search_{search_query.strip()}"

                if search_combine_choice == "combined":
                    combined = combine_subtitles(subtitle_files, temp_dir, safe_name, format_choice)
                    with open(combined, "rb") as f:
                        st.download_button("📄 Download Combined File", f.read(),
                                           os.path.basename(combined), mime_type)
                else:
                    zip_buffer, zip_name = create_zip(subtitle_files, safe_name, format_choice)
                    st.download_button("📦 Download ZIP", zip_buffer, zip_name, "application/zip")

            if cookies_file and os.path.exists(cookies_file):
                os.unlink(cookies_file)
            return

        # ── Channel + Keyword mode ──────────────────────────────────────────────
        if mode == "Channel + Keyword":
            if not keyword_channel_url or not keyword_channel_url.strip():
                st.error("Please enter a channel URL.")
                return
            if not keyword_filter or not keyword_filter.strip():
                st.error("Please enter a keyword to filter by.")
                return

            try:
                channel_url_norm, _, utype = validate_url(keyword_channel_url.strip())
            except ValueError as ve:
                st.error(str(ve))
                return
            if utype != 'channel':
                st.error("That doesn't look like a channel URL. Use a /@handle, /c/, /channel/, or /user/ URL.")
                return

            with st.spinner("Fetching channel video list..."):
                try:
                    entries, channel_title = get_info(channel_url_norm, True, cookies_file)
                except Exception as e:
                    st.error(f"Could not fetch video list: {str(e)}")
                    return

            if not entries:
                st.error("No videos found on this channel.")
                return

            raw_keywords = [k.strip() for k in keyword_filter.split('-') if k.strip()]
            if not raw_keywords:
                st.error("Please enter at least one keyword.")
                return

            seen_ids = set()
            filtered_entries = []
            for kw in raw_keywords:
                needle = kw if keyword_case_sensitive else kw.lower()
                matches = [
                    (vid, title) for vid, title in entries
                    if needle in (title if keyword_case_sensitive else title.lower())
                ]
                if not matches:
                    st.warning(f"⚠️ Keyword '{kw}' was not found in any video title on this channel.")
                    continue
                for vid, title in matches:
                    if vid not in seen_ids:
                        seen_ids.add(vid)
                        filtered_entries.append((vid, title))

            if not filtered_entries:
                st.error(f"None of the keyword(s) matched any videos on this channel.")
                return

            total_videos = len(filtered_entries)
            kw_display = "' / '".join(raw_keywords)
            st.write(f"Found **{total_videos}** video(s) matching **'{kw_display}'**. Starting download...")
            progress_bar = st.progress(0.0)

            with tempfile.TemporaryDirectory() as temp_dir:
                subtitle_files = []
                for i, (video_id, video_title) in enumerate(filtered_entries):
                    video_url_item = f"https://www.youtube.com/watch?v={video_id}"
                    try:
                        try:
                            sub_text, lang_code, is_auto = get_transcript_api(video_id, format_choice, sub_mode)
                            fallback_used = False
                        except Exception:
                            sub_text, lang_code, is_auto = get_subtitles_yt_dlp(
                                video_url_item, format_choice, cookies_file, temp_dir, sub_mode)
                            fallback_used = True

                        if clean_transcript:
                            sub_text = clean_subtitle_text(sub_text)
                        if format_choice == 'txt':
                            sub_text = convert_srt_to_txt(sub_text)

                        subtitle_files.append((video_title, sub_text))
                        lang_name = format_language_option(lang_code)
                        auto_note = ' (Auto-generated)' if is_auto else ''
                        source_note = ' (via yt-dlp)' if fallback_used else ''
                        st.info(f"✓ '{video_title}' — {lang_name}{auto_note}{source_note}")
                    except ValueError as ve:
                        msg = str(ve).lower()
                        if "age-restricted" in msg or "access denied" in msg:
                            st.warning(f"⚠️ '{video_title}' is age-restricted. Upload cookies to access.")
                        else:
                            st.warning(f"⚠️ No subs for '{video_title}': {str(ve)}")
                    except Exception as e:
                        st.warning(f"⚠️ Error for '{video_title}': {str(e)}")

                    progress_bar.progress((i + 1) / total_videos)

                if not subtitle_files:
                    st.error("Nothing was downloaded.")
                    return

                st.success(f"Done! Got subtitles for {len(subtitle_files)}/{total_videos} matching video(s).")
                mime_type = get_mime_type(format_choice)
                safe_name = f"{channel_title}_{keyword_filter}"

                if keyword_combine_choice == "combined":
                    combined = combine_subtitles(subtitle_files, temp_dir, safe_name, format_choice)
                    with open(combined, "rb") as f:
                        st.download_button("📄 Download Combined File", f.read(),
                                           os.path.basename(combined), mime_type)
                else:
                    zip_buffer, zip_name = create_zip(subtitle_files, safe_name, format_choice)
                    st.download_button("📦 Download ZIP", zip_buffer, zip_name, "application/zip")

            if cookies_file and os.path.exists(cookies_file):
                os.unlink(cookies_file)
            return

        # ── Multi-Video mode ──────────────────────────────────────────────────
        if mode == "Single / Multi-Video":
            raw_urls = [u.strip() for u in st.session_state.get("multi_urls", []) if u.strip()]
            if not raw_urls:
                st.error("Please enter at least one video URL.")
                return

            # Validate that every URL is a single video (no playlists/channels)
            valid_video_urls = []
            for raw in raw_urls:
                try:
                    _, vid_url, utype = validate_url(raw)
                    if utype not in ('video', 'both'):
                        st.warning(f"⚠️ Skipping non-video URL: {raw}")
                        continue
                    valid_video_urls.append(vid_url if vid_url else raw)
                except ValueError as ve:
                    st.warning(f"⚠️ Invalid URL skipped ({raw}): {ve}")

            if not valid_video_urls:
                st.error("No valid video URLs found.")
                return

            total_videos = len(valid_video_urls)
            st.write(f"Found **{total_videos}** video(s). Starting download...")
            progress_bar = st.progress(0.0)

            with tempfile.TemporaryDirectory() as temp_dir:
                subtitle_files = []   # list of (video_title, sub_text)
                # We also store channel info for txt headers
                meta_list = []        # list of (video_id, title, channel)

                for i, video_url_item in enumerate(valid_video_urls):
                    video_id = extract_video_id(video_url_item)
                    # Fetch metadata (title + channel)
                    try:
                        _, vid_title, channel_name = get_multi_video_info(video_url_item, cookies_file)
                    except Exception:
                        vid_title = f"Video {i+1}"
                        channel_name = "Unknown Channel"

                    meta_list.append((video_id, vid_title, channel_name))

                    try:
                        try:
                            sub_text, lang_code, is_auto = get_transcript_api(video_id, format_choice, sub_mode)
                            fallback_used = False
                        except Exception:
                            sub_text, lang_code, is_auto = get_subtitles_yt_dlp(
                                video_url_item, format_choice, cookies_file, temp_dir, sub_mode)
                            fallback_used = True

                        if clean_transcript:
                            sub_text = clean_subtitle_text(sub_text)
                        if format_choice == 'txt':
                            sub_text = convert_srt_to_txt(sub_text)
                            # Prepend video title and channel name for txt
                            sub_text = prepend_video_header(sub_text, vid_title, channel_name)

                        subtitle_files.append((vid_title, sub_text))
                        lang_name = format_language_option(lang_code)
                        auto_note = ' (Auto-generated)' if is_auto else ''
                        source_note = ' (yt-dlp)' if fallback_used else ''
                        st.info(f"✓ Subs: '{vid_title}' [{channel_name}] — {lang_name}{auto_note}{source_note}")
                    except ValueError as ve:
                        msg = str(ve).lower()
                        if "age-restricted" in msg or "access denied" in msg:
                            st.warning(f"⚠️ '{vid_title}' is age-restricted. Upload cookies to access.")
                        else:
                            st.warning(f"⚠️ No subs for '{vid_title}': {str(ve)}")
                    except Exception as e:
                        st.warning(f"⚠️ Error for '{vid_title}': {str(e)}")

                    progress_bar.progress((i + 1) / total_videos)

                progress_bar.progress(1.0)

                if not subtitle_files:
                    st.error("Nothing was downloaded. Try different URLs or upload cookies.")
                    return

                st.success(f"Done! Got subtitles for {len(subtitle_files)}/{total_videos} video(s).")
                mime_type = get_mime_type(format_choice)

                if total_videos == 1 and subtitle_files:
                    # Single video — always just one file
                    _, sub_text = subtitle_files[0]
                    fname = f"{sanitize_filename(subtitle_files[0][0])[:150]}.{format_choice}"
                    st.download_button("📄 Download Subtitle File",
                                       sub_text.encode('utf-8'), fname, mime_type)
                elif multi_combine_choice == "combined":
                    combined = combine_subtitles(subtitle_files, temp_dir, "multi_video", format_choice)
                    with open(combined, "rb") as f:
                        st.download_button("📄 Download Combined File", f.read(),
                                           os.path.basename(combined), mime_type)
                else:
                    zip_buffer, zip_name = create_zip(subtitle_files, "multi_video", format_choice)
                    st.download_button("📦 Download ZIP", zip_buffer, zip_name, "application/zip")

            if cookies_file and os.path.exists(cookies_file):
                os.unlink(cookies_file)
            return

        # ── Single / Playlist / Channel mode (unchanged) ──────────────────────
        if not url:
            st.error("Please enter a URL.")
            return

        try:
            playlist_url, video_url_parsed, url_type = validate_url(url)
        except ValueError as ve:
            st.error(str(ve))
            return

        if url_type == 'both' and download_scope == 'Entire Playlist':
            selected_url = playlist_url
            is_playlist = True
        elif url_type == 'both' and download_scope == 'Single Video':
            selected_url = video_url_parsed
            is_playlist = False
        elif url_type in ['playlist', 'channel']:
            selected_url = playlist_url
            is_playlist = True
        else:
            selected_url = video_url_parsed
            is_playlist = False

        type_label = {
            'playlist': 'Playlist', 'channel': 'Channel',
            'video': 'Video', 'both': 'Playlist'
        }.get(url_type, 'URL')
        st.info(f"**{type_label}:** {selected_url}")

        with st.spinner("Fetching video list..."):
            try:
                entries, playlist_title = get_info(selected_url, is_playlist, cookies_file)
            except Exception as e:
                st.error(f"Could not fetch video list: {str(e)}")
                return

        if not entries:
            st.error("No videos found at this URL.")
            return

        # Channel range filtering
        if url_type == 'channel':
            entries = list(reversed(entries))
            if channel_video_scope == "Range (oldest → newest)":
                if channel_range_start > channel_range_end:
                    st.error("'From' video number must be ≤ 'To' video number.")
                    return
                total_available = len(entries)
                start_idx = channel_range_start - 1
                end_idx = min(channel_range_end, total_available)
                if start_idx >= total_available:
                    st.error(
                        f"Start video #{channel_range_start} exceeds total channel videos ({total_available}). "
                        "Please enter a smaller number."
                    )
                    return
                entries = entries[start_idx:end_idx]
                st.info(
                    f"📋 Downloading subtitles for videos **#{channel_range_start}–#{min(channel_range_end, total_available)}** "
                    f"(oldest → newest) out of **{total_available}** total videos."
                )

        total_videos = len(entries)
        st.write(f"Found **{total_videos}** video(s). Starting download...")
        progress_bar = st.progress(0.0)

        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                subtitle_files = []
                for i, (video_id, video_title) in enumerate(entries):
                    video_url_item = f"https://www.youtube.com/watch?v={video_id}"
                    try:
                        try:
                            sub_text, lang_code, is_auto = get_transcript_api(video_id, format_choice, sub_mode)
                            fallback_used = False
                        except Exception:
                            sub_text, lang_code, is_auto = get_subtitles_yt_dlp(
                                video_url_item, format_choice, cookies_file, temp_dir, sub_mode)
                            fallback_used = True

                        if clean_transcript:
                            sub_text = clean_subtitle_text(sub_text)
                        if format_choice == 'txt':
                            sub_text = convert_srt_to_txt(sub_text)

                        subtitle_files.append((video_title, sub_text))
                        lang_name = format_language_option(lang_code)
                        auto_note = ' (Auto-generated)' if is_auto else ''
                        source_note = ' (yt-dlp)' if fallback_used else ''
                        st.info(f"✓ Subs: '{video_title}' — {lang_name}{auto_note}{source_note}")
                    except ValueError as ve:
                        msg = str(ve).lower()
                        if "age-restricted" in msg or "access denied" in msg:
                            st.warning(f"⚠️ '{video_title}' is age-restricted. Upload cookies to access.")
                        else:
                            st.warning(f"⚠️ No subs for '{video_title}': {str(ve)}")
                    except Exception as e:
                        st.warning(f"⚠️ Error for '{video_title}': {str(e)}")

                    progress_bar.progress((i + 1) / total_videos)

                progress_bar.progress(1.0)

                if not subtitle_files:
                    st.error("Nothing was downloaded. Try a different URL or upload cookies.")
                    return

                st.success(f"Done! Got subtitles for {len(subtitle_files)}/{total_videos} video(s).")
                mime_type = get_mime_type(format_choice)

                if is_playlist:
                    if combine_choice == 'combined':
                        combined = combine_subtitles(subtitle_files, temp_dir, playlist_title, format_choice)
                        with open(combined, 'rb') as f:
                            st.download_button("📄 Download Combined File", f.read(),
                                               os.path.basename(combined), mime_type)
                    else:
                        zip_buffer, zip_name = create_zip(subtitle_files, playlist_title, format_choice)
                        st.download_button("📦 Download ZIP", zip_buffer, zip_name, "application/zip")

                else:
                    if subtitle_files:
                        _, sub_text = subtitle_files[0]
                        fname = f"{sanitize_filename(subtitle_files[0][0])[:150]}.{format_choice}"
                        st.download_button("📄 Download Subtitle File",
                                           sub_text.encode('utf-8'), fname, mime_type)

            except Exception as e:
                st.error(f"Unexpected error: {str(e)}")
            finally:
                if cookies_file and os.path.exists(cookies_file):
                    os.unlink(cookies_file)

if __name__ == "__main__":
    main()
