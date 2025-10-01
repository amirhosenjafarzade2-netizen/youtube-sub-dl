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

# Set up logging for debugging
logging.basicConfig(level=logging.DEBUG)

# Language code to name mapping (expanded for Turkish)
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
    """Convert language code to readable name for display."""
    return LANGUAGE_NAMES.get(code, code.upper())

def validate_url(url):
    """Validate and classify the URL, return corrected URL, type, and video ID if present."""
    try:
        parsed_url = urlparse(url)
        
        if parsed_url.netloc == 'youtu.be':
            video_id = parsed_url.path.lstrip('/')
            query_params = parse_qs(parsed_url.query)
            playlist_id = query_params.get('list', [None])[0]
            
            if playlist_id and video_id:
                return (f"https://www.youtube.com/playlist?list={playlist_id}", 
                        f"https://www.youtube.com/watch?v={video_id}", 
                        'both')
            elif video_id:
                return (f"https://www.youtube.com/watch?v={video_id}", None, 'video')
        
        query_params = parse_qs(parsed_url.query)
        video_id = query_params.get('v', [None])[0]
        playlist_id = query_params.get('list', [None])[0]
        
        if playlist_id and video_id:
            return (f"https://www.youtube.com/playlist?list={playlist_id}", 
                    f"https://www.youtube.com/watch?v={video_id}", 
                    'both')
        elif playlist_id:
            return (f"https://www.youtube.com/playlist?list={playlist_id}", None, 'playlist')
        elif video_id:
            return (f"https://www.youtube.com/watch?v={video_id}", None, 'video')
        else:
            raise ValueError("Invalid YouTube URL. Please provide a video or playlist URL.")
    except Exception as e:
        raise ValueError(f"Error parsing URL: {str(e)}")

def extract_video_id(url):
    """Extract video ID from YouTube URL."""
    parsed = urlparse(url)
    if parsed.netloc == 'youtu.be':
        return parsed.path.lstrip('/')
    return parse_qs(parsed.query).get('v', [None])[0]

def get_transcript_api(video_id, format_choice='srt', target_lang='en'):
    """Fetch transcript using get_transcript (supports manual/auto, multilingual fallback)."""
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
                # Fallback to auto
                transcript_data = YouTubeTranscriptApi.get_transcript(video_id)
                lang_code = transcript_data[0].get('language', 'unknown')
                is_auto = True

        # Format
        if format_choice == 'srt':
            formatter = SRTFormatter()
        elif format_choice == 'vtt':
            formatter = WebVTTFormatter()
        else:  # txt: SRT then strip
            formatter = SRTFormatter()
            format_choice = 'srt'  # Temp

        sub_text = formatter.format_transcript(transcript_data)

        return sub_text, lang_code, is_auto

    except CouldNotRetrieveTranscript as e:
        raise ValueError(f"Access denied (age-restricted?): {str(e)}")
    except Exception as e:
        raise ValueError(f"API error: {str(e)}")

def get_subtitles_yt_dlp(video_url, format_choice, cookies_file, temp_dir, target_lang='en'):
    """Fallback to yt-dlp for restricted videos."""
    # For TXT, use SRT format and convert later
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
            # Find sub file based on target_lang
            if target_lang != 'auto':
                files = (
                    glob.glob(os.path.join(temp_dir, f'*.{target_lang}.{dl_format}')) +
                    glob.glob(os.path.join(temp_dir, f'*.{target_lang}.vtt'))
                )
            else:
                files = (
                    glob.glob(os.path.join(temp_dir, f'*.en.srt')) +
                    glob.glob(os.path.join(temp_dir, f'*.en.vtt')) +
                    glob.glob(os.path.join(temp_dir, f'*.tr.srt')) +
                    glob.glob(os.path.join(temp_dir, f'*.tr.vtt'))
                )
            if files:
                sub_path = files[0]
                with open(sub_path, 'r', encoding='utf-8') as f:
                    sub_text = f.read()
                os.remove(sub_path)
                if target_lang != 'auto':
                    lang_code = target_lang
                else:
                    lang_code = 'en' if '.en.' in sub_path else 'tr' if '.tr.' in sub_path else 'unknown'
                is_auto = 'auto' in sub_path.lower()
                return sub_text, lang_code, is_auto
            else:
                raise ValueError("No subtitles found")
        except Exception as e:
            raise ValueError(f"yt-dlp error: {str(e)}")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_info(url, is_playlist=False, cookies_file=None):
    """Get video IDs and titles."""
    if is_playlist:
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
            video_ids = [entry.get('id') for entry in entries if entry.get('id')]
            titles = [entry.get('title', f'video_{i+1}') for i, entry in enumerate(entries) if entry.get('id')]
            title = result.get('title', 'playlist_subtitles')
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
    """Convert SRT or VTT to TXT by removing timestamps, numbering, and tags."""
    # Handle both SRT and VTT formats
    lines = srt_text.split('\n')
    txt_lines = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # Skip SRT/VTT numbering
        if re.match(r'^\d+$', line):
            i += 1
            continue
        # Skip SRT timestamps
        if re.match(r'^\d{2}:\d{2}:\d{2}[,\.]\d{3} --> \d{2}:\d{2}:\d{2}[,\.]\d{3}$', line):
            i += 1
            continue
        # Skip VTT timestamps (slightly different format)
        if re.match(r'^\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}', line):
            i += 1
            continue
        # Skip VTT cues like "NOTE" or empty lines in between
        if line.startswith('NOTE') or not line:
            i += 1
            continue
        # Clean tags and webvtt specifics
        line = re.sub(r'<[\d:.]+>', '', line)
        line = re.sub(r'</?c[^>]*>', '', line)
        line = re.sub(r'</?v[^>]*>', '', line)
        line = re.sub(r'WEBVTT\n.*?\n', '', line, flags=re.DOTALL)  # Remove WEBVTT header if present
        if line:
            txt_lines.append(line)
        i += 1
    return '\n'.join(txt_lines) + '\n'

def clean_subtitle_text(text):
    """Clean text."""
    text = re.sub(r'\[Advertisement\].*?\n', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def combine_subtitles(subtitle_files, output_dir, title, format_choice):
    """Combine into single file."""
    ext = format_choice
    safe_title = sanitize_filename(title)[:150]
    combined_file = os.path.join(output_dir, f"{safe_title}_combined.{ext}")
    cue_index = 1
    
    with open(combined_file, 'w', encoding='utf-8') as outfile:
        for video_title, sub_text in subtitle_files:
            if format_choice != 'txt':
                outfile.write(f"\n\n=== {video_title} ===\n\n")
            else:
                outfile.write(f"\n\n### {video_title} ###\n\n")
            
            if format_choice in ['srt', 'vtt']:
                lines = sub_text.split('\n')
                i = 0
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
    """ZIP files."""
    zip_buffer = BytesIO()
    safe_title = sanitize_filename(title)[:150]
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for video_title, sub_text in subtitle_files:
            filename = f"{sanitize_filename(video_title)[:150]}.{format_choice}"
            zipf.writestr(filename, sub_text.encode('utf-8'))
    zip_buffer.seek(0)
    return zip_buffer, f"{safe_title}_subtitles.zip"

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=1, max=10))
def download_subtitles(url, format_choice, temp_dir, is_playlist, progress_bar, total_videos, clean_transcript, cookies_file=None, target_lang='en'):
    """Download with API fallback to yt-dlp (with or without cookies)."""
    subtitle_files = []
    entries, title = get_info(url, is_playlist, cookies_file)
    if not entries:
        st.error("No videos found.")
        return temp_dir, title, subtitle_files

    for i, (video_id, video_title) in enumerate(entries):
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            # Always try API first
            try:
                sub_text, lang_code, is_auto = get_transcript_api(video_id, format_choice, target_lang)
                fallback_used = False
            except:
                # Fallback to yt-dlp
                sub_text, lang_code, is_auto = get_subtitles_yt_dlp(video_url, format_choice, cookies_file, temp_dir, target_lang)
                fallback_used = True
                if not cookies_file:
                    st.info(f"Used yt-dlp fallback (no cookies) for '{video_title}'")

            if clean_transcript:
                sub_text = clean_subtitle_text(sub_text)

            if format_choice == 'txt':
                sub_text = convert_srt_to_txt(sub_text)

            subtitle_files.append((video_title, sub_text))
            lang_name = format_language_option(lang_code)
            auto_note = ' (Auto-generated from voice)' if is_auto else ''
            source_note = ' (via yt-dlp)' if fallback_used else ''
            st.info(f"Downloaded for '{video_title}' in {lang_name}{auto_note}{source_note}")
        except ValueError as ve:
            error_msg = str(ve).lower()
            if "age-restricted" in error_msg or "access denied" in error_msg:
                if cookies_file:
                    st.warning(f"Failed for '{video_title}' even with cookies: {str(ve)}")
                else:
                    st.warning(f"'{video_title}' is age-restricted. Upload cookies to access.")
            else:
                st.warning(f"No subs for '{video_title}': {str(ve)}")
        except Exception as e:
            st.warning(f"Error for '{video_title}': {str(e)}")

        progress_bar.progress((i + 1) / total_videos)

    return temp_dir, title, subtitle_files

def get_mime_type(format_choice):
    """MIME type."""
    return {'srt': 'text/plain', 'vtt': 'text/vtt', 'txt': 'text/plain'}.get(format_choice, 'text/plain')

def main():
    st.set_page_config(page_title="YouTube Subtitle Downloader", page_icon="🎥", layout="wide")
    st.title("YouTube Subtitle Downloader 🎥")
    st.markdown("Download subtitles (manual or auto-generated from voice) from YouTube videos/playlists! Supports Turkish/EN.")
    
    with st.sidebar:
        st.header("Settings")
        url = st.text_input("YouTube URL", placeholder="Paste video or playlist URL here...")
        
        st.markdown("""
        **For Age-Restricted Videos**: Upload cookies to bypass blocks.
        1. Use "cookies.txt" browser extension.
        2. Log in to YouTube, visit the video.
        3. Export as cookies.txt and upload.
        """)
        uploaded_file = st.file_uploader("Upload Cookies (Optional)", type=["txt"])
        cookies_file = None
        if uploaded_file:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
                tmp.write(uploaded_file.read())
                cookies_file = tmp.name
        
        format_choice = st.selectbox("Format", ["srt", "vtt", "txt"])
        clean_transcript = st.checkbox("Clean Transcript", value=True)
        
        target_display = st.radio("Target Language", ['English', 'Turkish', 'Auto'], horizontal=True)
        target_lang = {'English':'en', 'Turkish':'tr', 'Auto':'auto'}[target_display]
        
        combine_choice = "separate"  # Default to separate
        download_scope = 'Entire Playlist'
        if url:
            try:
                playlist_url, video_url, url_type = validate_url(url)
                if url_type == 'both':
                    download_scope = st.selectbox("Scope", ["Entire Playlist", "Single Video"], key="scope")
                is_playlist_mode = url_type in ['playlist'] or (url_type == 'both' and download_scope == "Entire Playlist")
                if is_playlist_mode:
                    combine_choice = st.selectbox("Output", ["separate", "combined"], key="combine")
            except ValueError as ve:
                st.error(str(ve))

    if st.button("Download Subtitles", type="primary"):
        if not url:
            st.error("Enter URL.")
            return

        with st.spinner("Fetching..."):
            with tempfile.TemporaryDirectory() as temp_dir:
                try:
                    playlist_url, video_url, url_type = validate_url(url)
                    if url_type == 'both' and download_scope == 'Entire Playlist':
                        selected_url = playlist_url
                        is_playlist = True
                    elif url_type == 'both' and download_scope == 'Single Video':
                        selected_url = video_url
                        is_playlist = False
                    elif url_type == 'playlist':
                        selected_url = playlist_url
                        is_playlist = True
                    else:  # video
                        selected_url = video_url
                        is_playlist = False
                    
                    st.info(f"{'Playlist' if is_playlist else 'Video'}: {selected_url}")

                    progress_bar = st.progress(0.0)
                    
                    entries, _ = get_info(selected_url, is_playlist, cookies_file)
                    total_videos = len(entries) or 1

                    _, title, subtitle_files = download_subtitles(
                        selected_url, format_choice, temp_dir, is_playlist,
                        progress_bar, total_videos, clean_transcript, cookies_file, target_lang
                    )

                    if not subtitle_files:
                        st.error("No subs found. Try a different video or upload cookies for restricted content.")
                        return

                    st.success(f"Downloaded {len(subtitle_files)} file(s)!")

                    mime_type = get_mime_type(format_choice)

                    if is_playlist:
                        if combine_choice == 'combined':
                            combined = combine_subtitles(subtitle_files, temp_dir, title, format_choice)
                            with open(combined, 'rb') as f:
                                st.download_button("Download Combined", f.read(), os.path.basename(combined), mime_type)
                        else:  # separate
                            zip_buffer, zip_name = create_zip(subtitle_files, title, format_choice)
                            st.download_button("Download ZIP", zip_buffer, zip_name, "application/zip")
                    else:
                        _, sub_text = subtitle_files[0]
                        st.download_button(
                            "Download File", sub_text.encode('utf-8'),
                            f"{sanitize_filename(subtitle_files[0][0])[:150]}.{format_choice}", mime_type
                        )

                except Exception as e:
                    st.error(f"Error: {str(e)}")
                finally:
                    if cookies_file and os.path.exists(cookies_file):
                        os.unlink(cookies_file)

if __name__ == "__main__":
    main()
