import streamlit as st
import os
import zipfile
import re
import glob
from urllib.parse import urlparse, parse_qs
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
    return LANGUAGE_NAMES.get(code, code.upper())

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

def get_transcript_api(video_id, format_choice='srt', target_lang='en'):
    try:
        if target_lang == 'auto':
            transcript_data = YouTubeTranscriptApi.get_transcript(video_id)
            lang_code = transcript_data[0].get('language', 'unknown')
            is_auto = True
        else:
            try:
                transcript_data = YouTubeTranscriptApi.get_transcript(video_id, languages=[target_lang])
                lang_code = target_lang
                is_auto = False
            except NoTranscriptFound:
                transcript_data = YouTubeTranscriptApi.get_transcript(video_id)
                lang_code = transcript_data[0].get('language', 'unknown')
                is_auto = True

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

def get_subtitles_yt_dlp(video_url, format_choice, cookies_file, temp_dir, target_lang='en'):
    dl_format = 'srt' if format_choice == 'txt' else format_choice
    ydl_opts = {
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
    if target_lang != 'auto':
        ydl_opts['subtitleslangs'] = [target_lang]
        ydl_opts['automaticsubslangs'] = [target_lang]
        ydl_opts['subtitlesformat'] = dl_format
    else:
        ydl_opts['subtitleslangs'] = ['en', 'tr']
        ydl_opts['automaticsubslangs'] = ['en', 'tr']
        ydl_opts['subtitlesformat'] = dl_format

    with YoutubeDL(ydl_opts) as ydl:
        try:
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
            if files:
                sub_path = files[0]
                with open(sub_path, 'r', encoding='utf-8') as f:
                    sub_text = f.read()
                os.remove(sub_path)
                lang_code = target_lang if target_lang != 'auto' else (
                    'en' if '.en.' in sub_path else 'tr' if '.tr.' in sub_path else 'unknown'
                )
                is_auto = 'auto' in sub_path.lower()
                return sub_text, lang_code, is_auto
            else:
                raise ValueError("No subtitles found")
        except Exception as e:
            raise ValueError(f"yt-dlp error: {str(e)}")

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

def create_video_zip(video_files, subtitle_files, title):
    """Bundle downloaded video files and subtitle files into one ZIP."""
    zip_buffer = BytesIO()
    safe_title = sanitize_filename(title)[:150]
    sub_map = {sanitize_filename(t)[:150]: text for t, text in subtitle_files}
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for video_path in video_files:
            if os.path.exists(video_path):
                zipf.write(video_path, os.path.basename(video_path))
        for video_title, sub_text in subtitle_files:
            ext = 'srt'
            filename = f"{sanitize_filename(video_title)[:150]}.{ext}"
            zipf.writestr(filename, sub_text.encode('utf-8'))
    zip_buffer.seek(0)
    return zip_buffer, f"{safe_title}_videos_and_subs.zip"

def download_videos_yt_dlp(entries, url, quality, cookies_file, temp_dir, progress_bar):
    """Download video files for all entries using yt-dlp."""
    video_files = []
    ydl_opts = {
        'format': quality,
        'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'cookiefile': cookies_file,
        'restrict_filenames': True,
        'ignoreerrors': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }
    total = len(entries)
    with YoutubeDL(ydl_opts) as ydl:
        for i, (video_id, video_title) in enumerate(entries):
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            try:
                ydl.download([video_url])
                # Find the downloaded file
                candidates = glob.glob(os.path.join(temp_dir, f"{sanitize_filename(video_title)[:100]}*"))
                if candidates:
                    video_files.append(candidates[0])
                    st.info(f"Downloaded video: '{video_title}'")
                else:
                    st.warning(f"Could not locate downloaded file for '{video_title}'")
            except Exception as e:
                st.warning(f"Video download failed for '{video_title}': {str(e)}")
            progress_bar.progress((i + 1) / total)
    return video_files

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=1, max=10))
def download_subtitles(url, format_choice, temp_dir, is_playlist, progress_bar, total_videos,
                       clean_transcript, cookies_file=None, target_lang='en'):
    subtitle_files = []
    entries, title = get_info(url, is_playlist, cookies_file)
    if not entries:
        st.error("No videos found.")
        return temp_dir, title, subtitle_files

    for i, (video_id, video_title) in enumerate(entries):
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            try:
                sub_text, lang_code, is_auto = get_transcript_api(video_id, format_choice, target_lang)
                fallback_used = False
            except Exception:
                sub_text, lang_code, is_auto = get_subtitles_yt_dlp(
                    video_url, format_choice, cookies_file, temp_dir, target_lang)
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

def main():
    st.set_page_config(page_title="YouTube Subtitle Downloader", page_icon="🎥", layout="wide")
    st.title("YouTube Subtitle Downloader 🎥")
    st.markdown("Download subtitles from YouTube videos, playlists, and channels! Supports Turkish/EN and more.")

    # --- Sidebar ---
    with st.sidebar:
        st.header("Settings")
        url = st.text_input("YouTube URL", placeholder="Paste video, playlist, or channel URL...")

        st.markdown("""
**For Age-Restricted Videos**: Upload cookies to bypass blocks.
1. Use the "Get cookies.txt LOCALLY" browser extension.
2. Log in to YouTube, visit the video.
3. Export as `cookies.txt` and upload below.
        """)
        uploaded_file = st.file_uploader("Upload Cookies (Optional)", type=["txt"])
        cookies_file = None
        if uploaded_file:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
                tmp.write(uploaded_file.read())
                cookies_file = tmp.name

        format_choice = st.selectbox("Subtitle Format", ["srt", "vtt", "txt"])
        clean_transcript = st.checkbox("Clean Transcript", value=True)

        target_display = st.radio("Target Language", ['English', 'Turkish', 'Auto'], horizontal=True)
        target_lang = {'English': 'en', 'Turkish': 'tr', 'Auto': 'auto'}[target_display]

        download_videos = st.checkbox("Download Videos Too", value=False)
        quality_options = {
            "Best Quality": "best",
            "720p": "best[height<=720]",
            "480p": "best[height<=480]",
            "Audio Only": "bestaudio",
        }
        quality = st.selectbox("Video Quality", list(quality_options.keys()), index=0,
                               disabled=not download_videos)
        selected_quality = quality_options[quality]

        # Defaults
        combine_choice = "separate"
        download_scope = "Entire Playlist"
        url_type = None
        playlist_url = None
        video_url_parsed = None

        # Channel video range controls (only shown for channel URLs)
        channel_video_scope = "All Videos"
        channel_range_start = 1
        channel_range_end = 50

        if url:
            try:
                playlist_url, video_url_parsed, url_type = validate_url(url)

                if url_type == 'channel':
                    st.info("📺 Channel URL detected — will fetch all uploaded videos.")

                    # --- Channel video range selection ---
                    st.markdown("---")
                    st.subheader("📋 Video Selection")
                    channel_video_scope = st.radio(
                        "Which videos to download?",
                        ["All Videos", "Range (oldest → newest)"],
                        key="channel_scope"
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
                    st.markdown("---")

                if url_type == 'both':
                    download_scope = st.selectbox("Scope", ["Entire Playlist", "Single Video"], key="scope")

                is_playlist_mode = url_type in ['playlist', 'channel'] or (
                    url_type == 'both' and download_scope == "Entire Playlist")
                if is_playlist_mode:
                    combine_choice = st.selectbox("Output", ["separate", "combined"], key="combine")
            except ValueError as ve:
                st.error(str(ve))

    # --- Main button ---
    button_text = "⬇️ Download Videos & Subtitles" if download_videos else "⬇️ Download Subtitles"
    if st.button(button_text, type="primary"):
        if not url:
            st.error("Please enter a URL.")
            return

        try:
            playlist_url, video_url_parsed, url_type = validate_url(url)
        except ValueError as ve:
            st.error(str(ve))
            return

        # Resolve which URL and mode to use
        if url_type == 'both' and download_scope == 'Entire Playlist':
            selected_url = playlist_url
            is_playlist = True
        elif url_type == 'both' and download_scope == 'Single Video':
            selected_url = video_url_parsed
            is_playlist = False
        elif url_type in ['playlist', 'channel']:
            selected_url = playlist_url   # channel URL stored in playlist_url slot
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

        # --- Channel range filtering ---
        # YouTube channels return videos newest-first; reverse so index 1 = oldest.
        if url_type == 'channel':
            entries = list(reversed(entries))
            if channel_video_scope == "Range (oldest → newest)":
                if channel_range_start > channel_range_end:
                    st.error("'From' video number must be ≤ 'To' video number.")
                    return
                total_available = len(entries)
                # Clamp to available range
                start_idx = channel_range_start - 1          # 0-based
                end_idx = min(channel_range_end, total_available)  # inclusive end, 1-based → exclusive slice
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
                # --- Subtitle download ---
                subtitle_files = []
                for i, (video_id, video_title) in enumerate(entries):
                    video_url_item = f"https://www.youtube.com/watch?v={video_id}"
                    try:
                        try:
                            sub_text, lang_code, is_auto = get_transcript_api(video_id, format_choice, target_lang)
                            fallback_used = False
                        except Exception:
                            sub_text, lang_code, is_auto = get_subtitles_yt_dlp(
                                video_url_item, format_choice, cookies_file, temp_dir, target_lang)
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

                    if not download_videos:
                        progress_bar.progress((i + 1) / total_videos)

                # --- Optional video download ---
                video_files = []
                if download_videos:
                    st.write("Downloading video files...")
                    video_files = download_videos_yt_dlp(
                        entries, selected_url, selected_quality, cookies_file, temp_dir, progress_bar)

                progress_bar.progress(1.0)

                if not subtitle_files and not video_files:
                    st.error("Nothing was downloaded. Try a different URL or upload cookies.")
                    return

                st.success(f"Done! Got subtitles for {len(subtitle_files)}/{total_videos} video(s).")
                mime_type = get_mime_type(format_choice)

                # --- Output ---
                if download_videos:
                    zip_buffer, zip_name = create_video_zip(video_files, subtitle_files, playlist_title)
                    st.download_button("📦 Download Videos + Subs ZIP", zip_buffer, zip_name, "application/zip")

                elif is_playlist:
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
