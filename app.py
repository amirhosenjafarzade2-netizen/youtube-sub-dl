import streamlit as st
import os
import zipfile
import re
import glob
import time
from urllib.parse import urlparse, parse_qs
from yt_dlp import YoutubeDL
from yt_dlp.utils import sanitize_filename
import tempfile
from io import BytesIO
import logging
from tenacity import retry, stop_after_attempt, wait_exponential
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, CouldNotRetrieveTranscript
from youtube_transcript_api.formatters import SRTFormatter, WebVTTFormatter

logging.basicConfig(level=logging.WARNING)

# ── Language map ──────────────────────────────────────────────────────────────
LANGUAGE_NAMES = {
    'en': 'English', 'tr': 'Türkçe (Turkish)', 'es': 'Español (Spanish)',
    'fr': 'Français (French)', 'de': 'Deutsch (German)', 'it': 'Italiano (Italian)',
    'pt': 'Português (Portuguese)', 'ru': 'Русский (Russian)', 'ja': '日本語 (Japanese)',
    'ko': '한국어 (Korean)', 'zh-Hans': '中文简体 (Chinese Simplified)',
    'zh-Hant': '中文繁體 (Chinese Traditional)', 'ar': 'العربية (Arabic)',
    'hi': 'हिन्दी (Hindi)', 'nl': 'Nederlands (Dutch)', 'pl': 'Polski (Polish)',
    'sv': 'Svenska (Swedish)', 'no': 'Norsk (Norwegian)', 'da': 'Dansk (Danish)',
    'fi': 'Suomi (Finnish)', 'cs': 'Čeština (Czech)', 'el': 'Ελληνικά (Greek)',
    'he': 'עברית (Hebrew)', 'id': 'Bahasa Indonesia (Indonesian)', 'th': 'ไทย (Thai)',
    'vi': 'Tiếng Việt (Vietnamese)', 'uk': 'Українська (Ukrainian)',
    'ro': 'Română (Romanian)', 'hu': 'Magyar (Hungarian)', 'bg': 'Български (Bulgarian)',
    'sr': 'Српски (Serbian)', 'hr': 'Hrvatski (Croatian)', 'sk': 'Slovenčina (Slovak)',
    'ca': 'Català (Catalan)',
}

def format_language_option(code):
    return LANGUAGE_NAMES.get(code, code.upper())


# ── URL helpers ───────────────────────────────────────────────────────────────
def is_channel_url(url):
    """Return True if the URL points to a YouTube channel (not a video/playlist)."""
    try:
        parsed = urlparse(url)
        path = parsed.path.rstrip('/')
        query_params = parse_qs(parsed.query)
        if 'v' in query_params or 'list' in query_params:
            return False
        patterns = [
            r'^/@[\w\-\.]+$',
            r'^/c/[\w\-\.]+$',
            r'^/channel/[\w\-]+$',
            r'^/user/[\w\-]+$',
        ]
        return any(re.match(p, path) for p in patterns)
    except Exception:
        return False


def validate_url(url):
    """Classify URL. Returns (primary_url, secondary_url, url_type).
    url_type in {'video', 'playlist', 'both', 'channel'}
    """
    try:
        parsed_url = urlparse(url)

        if is_channel_url(url):
            return (url, None, 'channel')

        if parsed_url.netloc == 'youtu.be':
            video_id = parsed_url.path.lstrip('/')
            query_params = parse_qs(parsed_url.query)
            playlist_id = query_params.get('list', [None])[0]
            if playlist_id and video_id:
                return (f"https://www.youtube.com/playlist?list={playlist_id}",
                        f"https://www.youtube.com/watch?v={video_id}", 'both')
            elif video_id:
                return (f"https://www.youtube.com/watch?v={video_id}", None, 'video')

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
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Error parsing URL: {str(e)}")


def extract_video_id(url):
    parsed = urlparse(url)
    if parsed.netloc == 'youtu.be':
        return parsed.path.lstrip('/')
    return parse_qs(parsed.query).get('v', [None])[0]


# ── Transcript fetchers ───────────────────────────────────────────────────────
def get_transcript_api(video_id, format_choice='srt', target_lang='en'):
    """Primary path: youtube-transcript-api."""
    try:
        if target_lang == 'auto':
            transcript_data = YouTubeTranscriptApi.get_transcript(video_id)
            lang_code = transcript_data[0].get('language_code', 'unknown')
            is_auto = True
        else:
            try:
                transcript_data = YouTubeTranscriptApi.get_transcript(video_id, languages=[target_lang])
                lang_code = target_lang
                is_auto = False
            except NoTranscriptFound:
                transcript_data = YouTubeTranscriptApi.get_transcript(video_id)
                lang_code = transcript_data[0].get('language_code', 'unknown')
                is_auto = True

        dl_format = 'srt' if format_choice == 'txt' else format_choice
        formatter = WebVTTFormatter() if dl_format == 'vtt' else SRTFormatter()
        sub_text = formatter.format_transcript(transcript_data)
        return sub_text, lang_code, is_auto

    except CouldNotRetrieveTranscript as e:
        raise ValueError(f"Access denied (age-restricted?): {str(e)}")
    except Exception as e:
        raise ValueError(f"API error: {str(e)}")


def get_subtitles_yt_dlp(video_url, format_choice, cookies_file, temp_dir, target_lang='en'):
    """Fallback path: yt-dlp."""
    dl_format = 'srt' if format_choice == 'txt' else format_choice
    ydl_opts = {
        'writesubtitles': True,
        'writeautomaticsub': True,
        'skip_download': True,
        'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'user_agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        ),
        'cookiefile': cookies_file,
        'restrict_filenames': True,
        'ignoreerrors': True,
        'subtitlesformat': dl_format,
    }
    if target_lang != 'auto':
        ydl_opts['subtitleslangs'] = [target_lang]
        ydl_opts['automaticsubslangs'] = [target_lang]
    else:
        ydl_opts['subtitleslangs'] = ['en', 'tr']
        ydl_opts['automaticsubslangs'] = ['en', 'tr']

    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])

    if target_lang != 'auto':
        files = (
            glob.glob(os.path.join(temp_dir, f'*.{target_lang}.{dl_format}')) +
            glob.glob(os.path.join(temp_dir, f'*.{target_lang}.vtt'))
        )
    else:
        files = (
            glob.glob(os.path.join(temp_dir, '*.en.srt')) +
            glob.glob(os.path.join(temp_dir, '*.en.vtt')) +
            glob.glob(os.path.join(temp_dir, '*.tr.srt')) +
            glob.glob(os.path.join(temp_dir, '*.tr.vtt'))
        )

    if not files:
        raise ValueError("No subtitles found via yt-dlp")

    sub_path = files[0]
    with open(sub_path, 'r', encoding='utf-8') as f:
        sub_text = f.read()
    try:
        os.remove(sub_path)
    except OSError:
        pass

    lang_code = (
        target_lang if target_lang != 'auto'
        else ('en' if '.en.' in sub_path else 'tr' if '.tr.' in sub_path else 'unknown')
    )
    is_auto = 'auto' in sub_path.lower()
    return sub_text, lang_code, is_auto


# ── Info fetchers ─────────────────────────────────────────────────────────────
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_channel_info(channel_url, cookies_file=None):
    """Return ([(video_id, title), ...], channel_title) for a channel URL."""
    videos_url = channel_url.rstrip('/')
    if not videos_url.endswith('/videos'):
        videos_url += '/videos'

    ydl_opts = {
        'extract_flat': True,
        'quiet': True,
        'no_warnings': True,
        'user_agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        ),
        'cookiefile': cookies_file,
    }
    with YoutubeDL(ydl_opts) as ydl:
        result = ydl.extract_info(videos_url, download=False)

    entries = result.get('entries', [])
    channel_title = result.get('channel', result.get('title', 'channel_subtitles'))
    video_pairs = [
        (entry['id'], entry.get('title', f'video_{i+1}'))
        for i, entry in enumerate(entries)
        if entry and entry.get('id')
    ]
    return video_pairs, channel_title


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_info(url, is_playlist=False, cookies_file=None):
    """Return ([(video_id, title), ...], collection_title) for video or playlist."""
    if is_playlist:
        ydl_opts = {
            'extract_flat': True, 'quiet': True, 'no_warnings': True,
            'user_agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            ),
            'cookiefile': cookies_file,
        }
        with YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(url, download=False)
        entries = result.get('entries', [])
        pairs = [
            (e.get('id'), e.get('title', f'video_{i+1}'))
            for i, e in enumerate(entries) if e and e.get('id')
        ]
        return pairs, result.get('title', 'playlist_subtitles')
    else:
        video_id = extract_video_id(url)
        if not video_id:
            raise ValueError("Invalid video URL")
        ydl_opts = {'quiet': True, 'no_warnings': True, 'cookiefile': cookies_file}
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return [(video_id, info.get('title', 'video_subtitles'))], info.get('title', 'video_subtitles')


# ── Text processing ───────────────────────────────────────────────────────────
def deduplicate_lines(lines):
    """Remove consecutive duplicate lines — common in auto-generated captions."""
    seen = None
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped != seen:
            out.append(line)
            seen = stripped
    return out


def convert_srt_to_txt(srt_text):
    """Strip timestamps, cue numbers, and tags. Deduplicate repeated auto-caption lines."""
    lines = srt_text.split('\n')
    txt_lines = []
    i = 0

    # Skip VTT header block
    if lines and lines[0].startswith('WEBVTT'):
        while i < len(lines) and lines[i].strip():
            i += 1

    while i < len(lines):
        line = lines[i].strip()
        if re.match(r'^\d+$', line):
            i += 1
            continue
        if re.match(r'^\d{2}:\d{2}:\d{2}[,\.]\d{3} --> \d{2}:\d{2}:\d{2}[,\.]\d{3}', line):
            i += 1
            continue
        if re.match(r'^\d{2}:\d{2}:\d{2}\.\d{3} --> ', line):
            i += 1
            continue
        if line.startswith('NOTE') or line.startswith('STYLE') or not line:
            i += 1
            continue
        # Strip all inline tags
        line = re.sub(r'<[^>]+>', '', line).strip()
        if line:
            txt_lines.append(line)
        i += 1

    txt_lines = deduplicate_lines(txt_lines)
    return '\n'.join(txt_lines) + '\n'


def clean_subtitle_text(text):
    text = re.sub(r'\[Advertisement\].*?\n', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def combine_subtitles(subtitle_files, output_dir, title, format_choice):
    """Merge all subtitles into a single file with per-video section headers.
    VTT: single WEBVTT header at top; subsequent per-chunk headers are stripped.
    """
    safe_title = sanitize_filename(title)[:150]
    combined_file = os.path.join(output_dir, f"{safe_title}_combined.{format_choice}")
    cue_index = 1

    with open(combined_file, 'w', encoding='utf-8') as outfile:
        if format_choice == 'vtt':
            outfile.write("WEBVTT\n\n")

        for video_title, sub_text in subtitle_files:
            sep = (
                f"\n\n### {video_title} ###\n\n" if format_choice == 'txt'
                else f"\n\n=== {video_title} ===\n\n"
            )
            outfile.write(sep)

            if format_choice in ('srt', 'vtt'):
                lines = sub_text.split('\n')
                start = 0
                # Skip embedded WEBVTT header in each chunk
                if format_choice == 'vtt' and lines and lines[0].startswith('WEBVTT'):
                    while start < len(lines) and lines[start].strip():
                        start += 1
                i = start
                while i < len(lines):
                    line = lines[i].strip()
                    if re.match(r'^\d+$', line):
                        outfile.write(f"{cue_index}\n")
                        cue_index += 1
                    else:
                        outfile.write(lines[i] + '\n')
                    i += 1
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


# ── Core download loop ────────────────────────────────────────────────────────
def download_subtitles(entries, format_choice, temp_dir,
                       progress_bar, status_text, clean_transcript,
                       cookies_file=None, target_lang='en',
                       rate_limit_delay=1.0):
    """
    Download subtitles for a list of (video_id, video_title) entries.
    Returns (subtitle_files, failed_videos).
      subtitle_files : [(video_title, sub_text), ...]
      failed_videos  : [(video_title, reason), ...]
    """
    subtitle_files = []
    failed_videos = []
    total = len(entries)

    for i, (video_id, video_title) in enumerate(entries):
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        status_text.text(f"⏳ {i+1}/{total} — {video_title[:70]}…")

        try:
            fallback_used = False
            try:
                sub_text, lang_code, is_auto = get_transcript_api(video_id, format_choice, target_lang)
            except Exception:
                sub_text, lang_code, is_auto = get_subtitles_yt_dlp(
                    video_url, format_choice, cookies_file, temp_dir, target_lang
                )
                fallback_used = True

            if clean_transcript:
                sub_text = clean_subtitle_text(sub_text)
            if format_choice == 'txt':
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

        except Exception as e:
            err = str(e)
            if "age-restricted" in err.lower() or "access denied" in err.lower():
                reason = "Age-restricted — upload cookies to access"
            else:
                reason = err[:120]
            failed_videos.append((video_title, reason))

        progress_bar.progress((i + 1) / total)
        if total > 5:
            time.sleep(rate_limit_delay)

    status_text.empty()
    return subtitle_files, failed_videos


def get_mime_type(format_choice):
    return {'srt': 'text/plain', 'vtt': 'text/vtt', 'txt': 'text/plain'}.get(format_choice, 'text/plain')


# ── Result renderer ───────────────────────────────────────────────────────────
def render_results(subtitle_files, failed_videos, title, format_choice, combine_choice, temp_dir):
    """Show summary metrics, failure list, and the download button."""
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
        st.download_button("📥 Download subtitle file", sub_text.encode('utf-8'), safe_name, mime)

    elif combine_choice == "combined":
        combined_path = combine_subtitles(subtitle_files, temp_dir, title, format_choice)
        with open(combined_path, 'rb') as f:
            st.download_button(
                "📥 Download combined file",
                f.read(), os.path.basename(combined_path), mime,
            )
    else:  # separate → ZIP
        zip_buffer, zip_name = create_zip(subtitle_files, title, format_choice)
        st.download_button("📥 Download ZIP", zip_buffer, zip_name, "application/zip")


# ── Streamlit UI ──────────────────────────────────────────────────────────────
def main():
    st.set_page_config(page_title="YouTube Subtitle Downloader", page_icon="🎥", layout="wide")
    st.title("YouTube Subtitle Downloader 🎥")
    st.caption("Download subtitles from YouTube videos, playlists, or entire channels.")

    # ── Sidebar ───────────────────────────────────────────────────────────────
    with st.sidebar:
        st.header("⚙️ Settings")
        url = st.text_input("YouTube URL", placeholder="Video, playlist, or channel URL…")

        with st.expander("🍪 Cookies (age-restricted videos)"):
            st.markdown(
                "1. Install the *Get cookies.txt LOCALLY* browser extension.\n"
                "2. Log in to YouTube and visit the restricted video.\n"
                "3. Export `cookies.txt` and upload below."
            )
            uploaded_file = st.file_uploader("Upload cookies.txt", type=["txt"])

        format_choice = st.selectbox("Subtitle format", ["srt", "vtt", "txt"])
        clean_transcript = st.checkbox("Clean transcript (remove ad markers)", value=True)

        target_display = st.radio("Target language", ['English', 'Turkish', 'Auto'], horizontal=True)
        target_lang = {'English': 'en', 'Turkish': 'tr', 'Auto': 'auto'}[target_display]

        rate_limit_delay = st.slider(
            "Delay between videos (s)",
            min_value=0.5, max_value=5.0, value=1.0, step=0.5,
            help="Increase if you're hitting YouTube rate limits on large batches."
        )

    # ── Write cookies to a temp file once, clean up in finally ───────────────
    cookies_path = None
    if uploaded_file:
        try:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
            tmp.write(uploaded_file.read())
            tmp.flush()
            tmp.close()
            cookies_path = tmp.name
        except Exception as e:
            st.sidebar.error(f"Could not save cookies: {e}")

    # ── URL classification ────────────────────────────────────────────────────
    url_type = None
    combine_choice = "separate"
    download_scope = "Entire Playlist"

    if url:
        try:
            playlist_url, video_url, url_type = validate_url(url)
        except ValueError as ve:
            st.error(str(ve))
            url_type = None

    # ── Channel: two-step (fetch → confirm output) ────────────────────────────
    if url_type == 'channel':
        st.info("📺 **Channel URL detected.**")

        # Invalidate stale session cache when the URL changes
        if st.session_state.get('_last_channel_url') != url:
            st.session_state.pop('channel_entries', None)
            st.session_state.pop('channel_title', None)
            st.session_state['_last_channel_url'] = url

        if st.button("🔍 Fetch video list"):
            with st.spinner("Fetching channel video list…"):
                try:
                    fetched_entries, fetched_title = get_channel_info(url, cookies_path)
                    st.session_state['channel_entries'] = fetched_entries
                    st.session_state['channel_title'] = fetched_title
                except Exception as e:
                    st.error(f"Could not fetch channel: {e}")

        if st.session_state.get('channel_entries'):
            channel_entries = st.session_state['channel_entries']
            channel_title = st.session_state['channel_title']
            st.success(f"Found **{len(channel_entries)} videos** in *{channel_title}*")

            combine_choice = st.radio(
                "📦 Output format",
                options=["separate", "combined"],
                format_func=lambda x: (
                    f"📁 Separate files — one .{format_choice} per video, bundled as ZIP"
                    if x == "separate"
                    else "📄 Combined — all subtitles merged into one file"
                ),
            )
        else:
            channel_entries, channel_title = None, None

    # ── Playlist / video scope ────────────────────────────────────────────────
    elif url_type in ('playlist', 'both'):
        with st.sidebar:
            if url_type == 'both':
                download_scope = st.selectbox("Download scope", ["Entire Playlist", "Single Video"])
            if url_type == 'playlist' or (url_type == 'both' and download_scope == "Entire Playlist"):
                combine_choice = st.selectbox("Output", ["separate", "combined"])

    # ── Download button ───────────────────────────────────────────────────────
    st.divider()
    btn_disabled = not url or url_type is None
    if st.button("⬇️ Download Subtitles", type="primary", disabled=btn_disabled):
        try:
            with tempfile.TemporaryDirectory() as temp_dir:

                # Channel ─────────────────────────────────────────────────────
                if url_type == 'channel':
                    if not st.session_state.get('channel_entries'):
                        st.error("Please click **Fetch video list** first.")
                    else:
                        channel_entries = st.session_state['channel_entries']
                        channel_title = st.session_state['channel_title']
                        progress_bar = st.progress(0.0)
                        status_text = st.empty()

                        subtitle_files, failed_videos = download_subtitles(
                            channel_entries, format_choice, temp_dir,
                            progress_bar, status_text, clean_transcript,
                            cookies_path, target_lang, rate_limit_delay,
                        )
                        render_results(
                            subtitle_files, failed_videos, channel_title,
                            format_choice, combine_choice, temp_dir,
                        )

                # Playlist / video ────────────────────────────────────────────
                else:
                    playlist_url, video_url, _ = validate_url(url)

                    if url_type == 'both' and download_scope == 'Entire Playlist':
                        selected_url, is_playlist = playlist_url, True
                    elif url_type == 'both' and download_scope == 'Single Video':
                        selected_url, is_playlist = video_url, False
                    elif url_type == 'playlist':
                        selected_url, is_playlist = playlist_url, True
                    else:
                        selected_url, is_playlist = video_url, False

                    with st.spinner("Fetching video list…"):
                        entries, collection_title = get_info(selected_url, is_playlist, cookies_path)

                    if not entries:
                        st.error("No videos found.")
                    else:
                        progress_bar = st.progress(0.0)
                        status_text = st.empty()

                        subtitle_files, failed_videos = download_subtitles(
                            entries, format_choice, temp_dir,
                            progress_bar, status_text, clean_transcript,
                            cookies_path, target_lang, rate_limit_delay,
                        )
                        effective_combine = combine_choice if is_playlist else "single"
                        render_results(
                            subtitle_files, failed_videos, collection_title,
                            format_choice, effective_combine, temp_dir,
                        )

        except Exception as e:
            st.error(f"Unexpected error: {e}")
        finally:
            # Always clean up cookies temp file regardless of outcome
            if cookies_path and os.path.exists(cookies_path):
                try:
                    os.unlink(cookies_path)
                except OSError:
                    pass


if __name__ == "__main__":
    main()
