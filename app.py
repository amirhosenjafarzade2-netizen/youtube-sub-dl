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

def get_first_video_id(info):
    """Helper to extract first video ID from playlist info."""
    if 'entries' in info and info['entries']:
        return info['entries'][0].get('id')
    return None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
def get_info(url, is_playlist, cookies_file=None):
    """Fetch info for video or playlist with retry."""
    ydl_opts = {
        'quiet': True,
        'extract_flat': True if is_playlist else False,
        'no_warnings': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'cookiefile': cookies_file,
        'restrict_filenames': True,
        'no_check_certificate': True,
    }
    with YoutubeDL(ydl_opts) as ydl:
        try:
            result = ydl.extract_info(url, download=False)
            if is_playlist:
                if 'entries' not in result or not result['entries']:
                    raise Exception("No videos found in playlist.")
                return result['entries'], result.get('title', 'playlist_subtitles')
            else:
                return [result], result.get('title', 'video_subtitles')
        except Exception as e:
            raise Exception(f"Error fetching info: {str(e)}")

@st.cache_data
def get_available_subtitle_languages(url, is_playlist, cookies_file=None):
    """Fetch available subtitle languages with timeout and retry."""
    ydl_opts = {
        'quiet': True,
        'listsubtitles': True,
        'no_warnings': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'cookiefile': cookies_file,
        'restrict_filenames': True,
        'no_check_certificate': True,
    }
    start_time = time.time()
    timeout = 30  # Increased timeout
    with YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            if is_playlist:
                first_video_id = get_first_video_id(info)
                if first_video_id:
                    info = ydl.extract_info(f"https://www.youtube.com/watch?v={first_video_id}", download=False)
                else:
                    return ['all']
            
            if time.time() - start_time > timeout:
                st.warning("Timeout fetching subtitle languages. Defaulting to all languages.")
                return ['all']
            
            languages = []
            if 'subtitles' in info and info['subtitles']:
                languages.extend(list(info['subtitles'].keys()))
            if 'automatic_captions' in info and info['automatic_captions']:
                languages.extend(list(info['automatic_captions'].keys()))
            return list(set(languages)) or ['all']
        except Exception as e:
            if "Sign in to confirm" in str(e):
                st.error("YouTube requires sign-in to access subtitle languages. Upload a cookies file or disable VPN.")
            else:
                st.warning(f"Error fetching subtitle languages: {str(e)}. Defaulting to all languages.")
            return ['all']

def find_subtitle_file(base_path, format_choice):
    """Find subtitle file for any available language."""
    patterns = [
        f"{base_path}.*.{format_choice}",
        f"{base_path}.*.auto.{format_choice}",
        f"{base_path}.*.srt",
        f"{base_path}.*.auto.srt",
        f"{base_path}.*.vtt",
        f"{base_path}.*.auto.vtt",
    ]
    logging.debug(f"Searching for subtitle files with patterns: {patterns}")
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            selected_language = matches[0].split('.')[-2].split('.auto')[0]
            logging.debug(f"Found subtitle file: {matches[0]} for language: {selected_language}")
            return matches[0], selected_language
    logging.warning(f"No subtitle files found for base_path: {base_path}")
    return None, None

def convert_vtt_to_srt(vtt_path):
    """Convert VTT to SRT format with proper timestamp conversion."""
    srt_path = vtt_path.rsplit('.', 1)[0] + '.srt'
    with open(vtt_path, 'r', encoding='utf-8') as vtt_file, open(srt_path, 'w', encoding='utf-8') as srt_file:
        for line in vtt_file:
            if line.strip() == 'WEBVTT' or line.startswith('Kind:') or line.startswith('Language:'):
                continue
            line = re.sub(r'(\d{2}:\d{2}:\d{2})\.(\d{3})', r'\1,\2', line)
            srt_file.write(line)
    return srt_path

def convert_srt_to_txt(srt_path):
    """Convert SRT to plain TXT by stripping timestamps, numbers, and inline tags."""
    txt_path = srt_path.rsplit('.', 1)[0] + '.txt'
    with open(srt_path, 'r', encoding='utf-8') as srt_file, open(txt_path, 'w', encoding='utf-8') as txt_file:
        for line in srt_file:
            if re.match(r'^\d+$', line.strip()) or re.match(r'^\d{2}:\d{2}:\d{2}[,\.]\d{3} --> \d{2}:\d{2}:\d{2}[,\.]\d{3}$', line.strip()):
                continue
            line = re.sub(r'<[\d:.]+>', '', line)
            line = re.sub(r'</?c[^>]*>', '', line)
            line = re.sub(r'</?v[^>]*>', '', line)
            if line.strip():
                txt_file.write(line.strip() + '\n')
        txt_file.write('\n')
    return txt_path

def clean_subtitle_text(text):
    """Clean subtitle text: Remove ads, extra newlines, etc."""
    text = re.sub(r'\[Advertisement\].*?\n', '', text, flags=re.DOTALL)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text

def combine_subtitles(subtitle_files, output_dir, title, format_choice):
    """Combine subtitles into a single file."""
    ext = format_choice
    safe_title = sanitize_filename(title)[:150]
    combined_file = os.path.join(output_dir, f"{safe_title}_combined.{ext}")
    cue_index = 1
    
    with open(combined_file, 'w', encoding='utf-8') as outfile:
        for video_title, sub_path in subtitle_files:
            if format_choice != 'txt':
                outfile.write(f"\n\n=== {video_title} ===\n\n")
            else:
                outfile.write(f"\n\n### {video_title} ###\n\n")
            
            if format_choice in ['srt', 'vtt']:
                with open(sub_path, 'r', encoding='utf-8') as infile:
                    content = infile.read()
                    lines = content.split('\n')
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
                with open(sub_path, 'r', encoding='utf-8') as infile:
                    outfile.write(infile.read())
    
    return combined_file

def create_zip(subtitle_files, title):
    """Create zip for separate files."""
    zip_buffer = BytesIO()
    safe_title = sanitize_filename(title)[:150]
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for _, sub_path in subtitle_files:
            zipf.write(sub_path, os.path.basename(sub_path))
    zip_buffer.seek(0)
    return zip_buffer, f"{safe_title}_subtitles.zip"

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
def download_subtitles(url, format_choice, temp_dir, is_playlist, progress_bar, total_videos, clean_transcript, cookies_file=None):
    """Download all available subtitles for video or playlist with retry."""
    ydl_opts = {
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['all'],
        'subtitlesformat': 'vtt' if format_choice == 'vtt' else 'srt',
        'skip_download': True,
        'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'cookiefile': cookies_file,
        'restrict_filenames': True,
        'no_check_certificate': True,
        'ignore_no_formats_error': True,
    }
    
    subtitle_files = []
    try:
        entries, title = get_info(url, is_playlist, cookies_file)
        if not entries:
            st.error("No videos found in the provided URL.")
            return temp_dir, title, subtitle_files
    except Exception as e:
        if "Sign in to confirm" in str(e):
            st.error("YouTube requires sign-in to access video info. Upload a cookies file or disable VPN.")
        else:
            st.error(f"Error fetching video info: {str(e)}")
        return temp_dir, "unknown", []
    
    for i, entry in enumerate(entries):
        video_title = entry.get('title', f'video_{i+1}')
        
        try:
            video_id = entry.get('id')
            if not video_id and 'url' in entry:
                video_id = entry['url'].split('v=')[-1].split('&')[0]
            
            if not video_id:
                st.warning(f"Could not extract video ID for '{video_title}'")
                progress_bar.progress((i + 1) / total_videos)
                continue
            
            video_url = f"https://www.youtube.com/watch?v={video_id}" if is_playlist else url
            sanitized_title = sanitize_filename(video_title)[:150]
            base_path = os.path.join(temp_dir, sanitized_title)
            logging.debug(f"Downloading subtitles for {video_url}, base_path: {base_path}")
            
            start_time = time.time()
            timeout = 30  # Increased timeout
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_url])
            
            if time.time() - start_time > timeout:
                st.warning(f"Timeout downloading subtitles for '{video_title}'. Skipping.")
                progress_bar.progress((i + 1) / total_videos)
                continue
            
            sub_path, selected_language = find_subtitle_file(base_path, format_choice)
            
            if sub_path:
                if format_choice == 'txt':
                    if sub_path.endswith('.vtt'):
                        sub_path = convert_vtt_to_srt(sub_path)
                    sub_path = convert_srt_to_txt(sub_path)
                elif format_choice == 'srt' and sub_path.endswith('.vtt'):
                    sub_path = convert_vtt_to_srt(sub_path)
                
                if clean_transcript:
                    with open(sub_path, 'r', encoding='utf-8') as f:
                        text = clean_subtitle_text(f.read())
                    with open(sub_path, 'w', encoding='utf-8') as f:
                        f.write(text)
                
                subtitle_files.append((video_title, sub_path))
                if selected_language:
                    st.info(f"Downloaded subtitles for '{video_title}' in {format_language_option(selected_language)}")
            else:
                st.warning(f"No subtitles found for '{video_title}'")
        
        except Exception as e:
            if "Sign in to confirm" in str(e):
                st.error(f"YouTube requires sign-in for '{video_title}' ({video_id}). Upload a cookies file or disable VPN.")
            else:
                st.warning(f"Error downloading subtitles for '{video_title}': {str(e)}")
            progress_bar.progress((i + 1) / total_videos)
            continue
        
        progress_bar.progress((i + 1) / total_videos)
    
    return temp_dir, title, subtitle_files

def get_mime_type(format_choice):
    """Get appropriate MIME type for download button."""
    mime_map = {
        'srt': 'text/plain',
        'vtt': 'text/vtt',
        'txt': 'text/plain'
    }
    return mime_map.get(format_choice, 'text/plain')

def main():
    st.set_page_config(page_title="YouTube Subtitle Downloader", page_icon="🎥", layout="wide")
    st.title("YouTube Subtitle Downloader 🎥")
    st.markdown("Download subtitles from YouTube videos or playlists with a sleek interface and progress tracking!")
    
    with st.sidebar:
        st.header("Settings")
        url = st.text_input("YouTube URL", placeholder="Paste video or playlist URL here...")
        
        st.markdown("""
        **Cookies Instructions**: If you see a "Sign in to confirm you’re not a bot" error, upload a cookies file:
        1. Install the "cookies.txt" extension for Chrome/Firefox.
        2. Log into YouTube in your browser and access the video/playlist.
        3. Export cookies using the extension (save as `cookies.txt`).
        4. Upload the file below.
        """)
        cookies_file = None
        uploaded_file = st.file_uploader("Upload YouTube Cookies (Optional)", type=["txt"], help="Export cookies from your browser to bypass 'Sign in to confirm you’re not a bot' errors.")
        if uploaded_file:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp_file:
                tmp_file.write(uploaded_file.read())
                cookies_file = tmp_file.name
        
        format_choice = st.selectbox(
            "Format", 
            ["srt", "vtt", "txt"], 
            help="SRT/VTT include timestamps; TXT is plain text."
        )
        
        clean_transcript = st.checkbox(
            "Clean Transcript (Remove ads/extra newlines)", 
            value=True, 
            help="Remove advertisements and normalize formatting in subtitles."
        )
        
        combine_choice = None
        download_scope = 'Entire Playlist'
        if url:
            try:
                corrected_playlist_url, corrected_video_url, url_type = validate_url(url)
                if url_type == 'both':
                    download_scope = st.selectbox(
                        "Download Scope", 
                        ["Entire Playlist", "Single Video"], 
                        help="Choose whether to download for the playlist or just the video in the URL", 
                        key="download_scope"
                    )
                if url_type == 'playlist' or (url_type == 'both' and download_scope == 'Entire Playlist'):
                    combine_choice = st.selectbox(
                        "Output Style", 
                        ["separate", "combined"], 
                        help="Separate: Individual files in ZIP; Combined: Single file."
                    )
            except ValueError as ve:
                st.error(str(ve))
            except Exception as e:
                if "Sign in to confirm" in str(e):
                    st.error("YouTube requires sign-in to validate URL. Upload a cookies file or disable VPN.")
                else:
                    st.error(f"Error validating URL: {str(e)}")

    if st.button("Download Subtitles", type="primary"):
        if not url:
            st.error("Please enter a YouTube URL.")
            return

        with st.spinner("Downloading subtitles..."):
            with tempfile.TemporaryDirectory() as temp_dir:
                try:
                    corrected_playlist_url, corrected_video_url, url_type = validate_url(url)
                    is_playlist = url_type == 'playlist' or (url_type == 'both' and download_scope == "Entire Playlist")
                    selected_url = corrected_playlist_url if is_playlist else corrected_video_url
                    
                    st.info(f"Detected: {'Playlist' if is_playlist else 'Single Video'} - Using URL: {selected_url}")

                    progress_container = st.empty()
                    progress_bar = progress_container.progress(0.0)
                    
                    entries, _ = get_info(selected_url, is_playlist, cookies_file)
                    total_videos = len(entries) or 1

                    output_dir, title, subtitle_files = download_subtitles(
                        selected_url, 
                        format_choice, 
                        temp_dir, 
                        is_playlist, 
                        progress_bar, 
                        total_videos, 
                        clean_transcript, 
                        cookies_file
                    )

                    progress_container.empty()

                    if not subtitle_files:
                        st.error("No subtitles were downloaded. Upload a cookies file, disable VPN, or check if subtitles are available.")
                        return

                    st.success(f"Successfully downloaded {len(subtitle_files)} subtitle file(s)!")

                    mime_type = get_mime_type(format_choice)

                    if is_playlist and combine_choice == 'combined':
                        combined_file = combine_subtitles(subtitle_files, output_dir, title, format_choice)
                        with open(combined_file, 'rb') as f:
                            file_data = f.read()
                        st.download_button(
                            "Download Combined File", 
                            file_data, 
                            file_name=os.path.basename(combined_file), 
                            mime=mime_type
                        )
                    elif is_playlist and combine_choice == 'separate':
                        zip_buffer, zip_name = create_zip(subtitle_files, title)
                        st.download_button(
                            "Download ZIP", 
                            zip_buffer, 
                            file_name=zip_name, 
                            mime="application/zip"
                        )
                    else:
                        if subtitle_files:
                            _, sub_path = subtitle_files[0]
                            with open(sub_path, 'rb') as f:
                                file_data = f.read()
                            st.download_button(
                                "Download Subtitle File", 
                                file_data, 
                                file_name=os.path.basename(sub_path), 
                                mime=mime_type
                            )
                        else:
                            st.error("Unexpected error: No subtitle files available.")

                except ValueError as ve:
                    st.error(str(ve))
                except Exception as e:
                    if "Sign in to confirm" in str(e):
                        st.error("YouTube requires sign-in to process the request. Upload a cookies file or disable VPN.")
                    else:
                        st.error(f"Error: {str(e)}")
                finally:
                    if cookies_file and os.path.exists(cookies_file):
                        os.unlink(cookies_file)  # Clean up temporary cookies file

if __name__ == "__main__":
    main()
