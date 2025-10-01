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
import time
import logging
from tenacity import retry, stop_after_attempt, wait_exponential
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, CouldNotRetrieveTranscript
from youtube_transcript_api.formatters import SRTFormatter, WebVTTFormatter

# Set up logging for debugging
logging.basicConfig(level=logging.DEBUG)

# Language code to name mapping
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

def get_transcript_api(video_id, format_choice='srt'):
    """Fetch transcript using youtube-transcript-api (fast for public)."""
    try:
        transcripts = YouTubeTranscriptApi.list_transcripts(video_id)
        transcript_list = []

        # Prioritize auto-generated English
        try:
            transcript = transcripts.find_generated_transcript(['en'])
            if transcript:
                transcript_list.append((transcript.fetch(), 'en', True))  # True for auto
        except (TranscriptsDisabled, NoTranscriptFound):
            pass

        # Manual fallback
        try:
            transcript = transcripts.find_transcript(['en'])
            if transcript:
                transcript_list.append((transcript.fetch(), 'en', False))  # False for manual
        except (TranscriptsDisabled, NoTranscriptFound):
            pass

        if not transcript_list:
            # Any language
            transcript = next(iter(transcripts), None)
            if transcript:
                transcript_list.append((transcript.fetch(), transcript.language_code, False))

        if not transcript_list:
            raise NoTranscriptFound("No transcripts available")

        transcript_data, lang_code, is_auto = transcript_list[0]

        # Format
        if format_choice == 'srt':
            formatter = SRTFormatter()
        elif format_choice == 'vtt':
            formatter = WebVTTFormatter()
        else:  # txt: format to SRT then strip
            formatter = SRTFormatter()
            format_choice = 'srt'  # Temp

        sub_text = formatter.format_transcript(transcript_data)

        return sub_text, lang_code, is_auto

    except CouldNotRetrieveTranscript as e:
        raise ValueError(f"Transcript access denied (likely age-restricted): {str(e)}")
    except Exception as e:
        raise ValueError(f"API error: {str(e)}")

def get_subtitles_yt_dlp(video_url, format_choice, cookies_file=None):
    """Fallback to yt-dlp for restricted videos with cookies."""
    ydl_opts = {
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['en', 'all'],  # Prioritize English
        'subtitlesformat': format_choice,
        'skip_download': True,
        'outtmpl': '%(title)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'cookiefile': cookies_file,
        'restrict_filenames': True,
        'ignoreerrors': True,
    }
    with YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(video_url, download=False)
            # Download subs
            ydl.download([video_url])
            # Find the English sub file (manual or auto)
            files = glob.glob('*.en.*') + glob.glob('*.auto.*')
            if files:
                sub_path = files[0]  # First match
                with open(sub_path, 'r', encoding='utf-8') as f:
                    sub_text = f.read()
                os.remove(sub_path)  # Cleanup
                is_auto = 'auto' in sub_path
                return sub_text, 'en', is_auto
            else:
                raise ValueError("No English subtitles found")
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
    """Convert SRT to TXT."""
    lines = srt_text.split('\n')
    txt_lines = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if re.match(r'^\d+$', line) or re.match(r'^\d{2}:\d{2}:\d{2}[,\.]\d{3} --> \d{2}:\d{2}:\d{2}[,\.]\d{3}$', line):
            i += 1
            continue
        line = re.sub(r'<[\d:.]+>', '', line)
        line = re.sub(r'</?c[^>]*>', '', line)
        line = re.sub(r'</?v[^>]*>', '', line)
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

def create_zip(subtitle_files, title):
    """ZIP files."""
    zip_buffer = BytesIO()
    safe_title = sanitize_filename(title)[:150]
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for video_title, sub_text in subtitle_files:
            filename = f"{sanitize_filename(video_title)[:150]}.srt"
            zipf.writestr(filename, sub_text.encode('utf-8'))
    zip_buffer.seek(0)
    return zip_buffer, f"{safe_title}_subtitles.zip"

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=1, max=10))
def download_subtitles(url, format_choice, temp_dir, is_playlist, progress_bar, total_videos, clean_transcript, cookies_file=None):
    """Download with API fallback to yt-dlp."""
    subtitle_files = []
    entries, title = get_info(url, is_playlist, cookies_file)
    if not entries:
        st.error("No videos found.")
        return temp_dir, title, subtitle_files

    for i, (video_id, video_title) in enumerate(entries):
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            if cookies_file:
                # Use yt-dlp for restricted
                sub_text, lang_code, is_auto = get_subtitles_yt_dlp(video_url, format_choice, cookies_file)
            else:
                # API for public
                sub_text, lang_code, is_auto = get_transcript_api(video_id, format_choice)

            if clean_transcript:
                sub_text = clean_subtitle_text(sub_text)

            if format_choice == 'txt':
                sub_text = convert_srt_to_txt(sub_text)

            subtitle_files.append((video_title, sub_text))
            lang_name = format_language_option(lang_code)
            auto_note = ' (Auto-generated from voice)' if is_auto else ''
            st.info(f"Downloaded for '{video_title}' in {lang_name}{auto_note}")
        except ValueError as ve:
            if "age-restricted" in str(ve).lower() or "access denied" in str(ve).lower():
                st.warning(f"'{video_title}' is age-restricted. Upload cookies to access subs.")
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
    st.markdown("Download subtitles (manual or auto-generated from voice) from YouTube videos/playlists!")
    
    with st.sidebar:
        st.header("Settings")
        url = st.text_input("YouTube URL", placeholder="Paste video or playlist URL here...")
        
        st.markdown("""
        **For Age-Restricted Videos**: Upload cookies to bypass blocks (e.g., horror/true crime).
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
        
        combine_choice = None
        download_scope = 'Entire Playlist'
        if url:
            try:
                _, _, url_type = validate_url(url)
                if url_type == 'both':
                    download_scope = st.selectbox("Scope", ["Entire Playlist", "Single Video"], key="scope")
                if url_type in ['playlist', ('both' if download_scope == 'Entire Playlist' else '')]:
                    combine_choice = st.selectbox("Output", ["separate", "combined"])
            except ValueError as ve:
                st.error(str(ve))

    if st.button("Download Subtitles", type="primary"):
        if not url:
            st.error("Enter URL.")
            return

        with st.spinner("Fetching..."):
            with tempfile.TemporaryDirectory() as temp_dir:
                try:
                    _, corrected_video_url, url_type = validate_url(url)
                    is_playlist = url_type == 'playlist' or (url_type == 'both' and download_scope == "Entire Playlist")
                    selected_url = corrected_video_url if not is_playlist else url  # Simplified
                    
                    st.info(f"{'Playlist' if is_playlist else 'Video'}: {selected_url}")

                    progress_bar = st.progress(0.0)
                    
                    entries, _ = get_info(selected_url, is_playlist, cookies_file)
                    total_videos = len(entries) or 1

                    _, title, subtitle_files = download_subtitles(
                        selected_url, format_choice, temp_dir, is_playlist,
                        progress_bar, total_videos, clean_transcript, cookies_file
                    )

                    if not subtitle_files:
                        st.error("No subs found. Try uploading cookies for restricted videos.")
                        return

                    st.success(f"Downloaded {len(subtitle_files)} file(s)!")

                    mime_type = get_mime_type(format_choice)

                    if is_playlist and combine_choice == 'combined':
                        combined = combine_subtitles(subtitle_files, temp_dir, title, format_choice)
                        with open(combined, 'rb') as f:
                            st.download_button("Download Combined", f.read(), os.path.basename(combined), mime_type)
                    elif is_playlist and combine_choice == 'separate':
                        zip_buffer, zip_name = create_zip(subtitle_files, title)
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
