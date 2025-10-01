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
import requests
from bs4 import BeautifulSoup
import json

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

def get_caption_tracks(video_id):
    """Fetch video page and parse for available caption tracks (manual + auto-generated)."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://www.youtube.com/',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        raise ValueError(f"Failed to fetch video page: {response.status_code}")
    
    # Parse ytInitialPlayerResponse from JS
    soup = BeautifulSoup(response.text, 'html.parser')
    scripts = soup.find_all('script')
    for script in scripts:
        if script.string and 'ytInitialPlayerResponse' in script.string:
            match = re.search(r'ytInitialPlayerResponse\s*=\s*({.+?});', script.string, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
                captions = data.get('captions', {}).get('playerCaptionsTracklistRenderer', {}).get('captionTracks', [])
                audio_tracks = data.get('captions', {}).get('asrTrack', []) if data.get('captions') else []
                return captions + audio_tracks  # Returns list of dicts with 'baseUrl', 'languageCode', 'name'
    raise ValueError("No caption tracks found (manual or auto-generated)")

def fetch_subtitle(base_url, fmt='srt'):
    """Fetch subtitle from timedtext API (supports auto-generated)."""
    url = f"{base_url}&fmt={fmt}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://www.youtube.com/',
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.text
    raise ValueError(f"Failed to fetch subtitle: {response.status_code}")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_info(url, is_playlist=False, cookies_file=None):
    """Get subtitle tracks for video or playlist IDs."""
    if is_playlist:
        # Light yt-dlp for playlist entries only
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
            title = result.get('title', 'playlist_subtitles')
            return [{'id': vid} for vid in video_ids], title
    else:
        video_id = extract_video_id(url)
        if not video_id:
            raise ValueError("Invalid video URL")
        tracks = get_caption_tracks(video_id)
        # Fetch title from page
        title_url = f"https://www.youtube.com/watch?v={video_id}"
        title_response = requests.get(title_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
        title_match = re.search(r'"title":"([^"]+)"', title_response.text)
        title = title_match.group(1).replace('\\u0027', "'") if title_match else 'video_subtitles'
        return [{'id': video_id, 'tracks': tracks}], title

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

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=1, max=10))
def download_subtitles(url, format_choice, temp_dir, is_playlist, progress_bar, total_videos, clean_transcript, cookies_file=None):
    """Download subtitles using direct timedtext API (manual + auto-generated)."""
    subtitle_files = []
    try:
        entries, title = get_info(url, is_playlist, cookies_file)
        if not entries:
            st.error("No videos found in the provided URL.")
            return temp_dir, title, subtitle_files
    except Exception as e:
        st.error(f"Error fetching video info: {str(e)}")
        return temp_dir, "unknown", []

    for i, entry in enumerate(entries):
        video_id = entry.get('id')
        tracks = entry.get('tracks', []) if 'tracks' in entry else get_caption_tracks(video_id)
        video_title = f"video_{i+1}"  # Can enhance with title fetch per video

        # Prioritize English manual, then auto
        selected_track = None
        for track in tracks:
            if track.get('languageCode') == 'en' and 'asr' not in track.get('vssId', ''):  # Manual first
                selected_track = track
                break
        if not selected_track:
            for track in tracks:
                if 'asr' in track.get('vssId', ''):  # Auto-generated fallback
                    selected_track = track
                    break
        if not selected_track:
            selected_track = tracks[0] if tracks else None
        if not selected_track:
            st.warning(f"No subtitles (manual or auto-generated) available for '{video_title}'")
            progress_bar.progress((i + 1) / total_videos)
            continue

        base_url = selected_track['baseUrl']
        try:
            sub_text = fetch_subtitle(base_url, fmt=format_choice)
            sanitized_title = sanitize_filename(video_title)[:150]
            base_path = os.path.join(temp_dir, sanitized_title)
            sub_path = f"{base_path}.{format_choice}"

            with open(sub_path, 'w', encoding='utf-8') as f:
                f.write(sub_text)

            # Conversions
            if format_choice == 'txt':
                if sub_path.endswith('.vtt'):
                    sub_path = convert_vtt_to_srt(sub_path)
                sub_path = convert_srt_to_txt(sub_path)
            elif format_choice == 'srt' and sub_path.endswith('.vtt'):
                sub_path = convert_vtt_to_srt(sub_path)

            # Clean
            if clean_transcript:
                with open(sub_path, 'r', encoding='utf-8') as f:
                    text = clean_subtitle_text(f.read())
                with open(sub_path, 'w', encoding='utf-8') as f:
                    f.write(text)

            subtitle_files.append((video_title, sub_path))
            lang = format_language_option(selected_track.get('languageCode', 'unknown'))
            is_auto = ' (Auto-generated)' if 'asr' in selected_track.get('vssId', '') else ''
            st.info(f"Downloaded subtitles for '{video_title}' in {lang}{is_auto}")
        except Exception as e:
            logging.error(f"Error downloading for '{video_title}': {str(e)}")
            st.warning(f"Error for '{video_title}': {str(e)}")
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
        **Cookies Instructions**: Optional for restricted content. If needed, upload a cookies file:
        1. Install the "cookies.txt" extension for Chrome/Firefox.
        2. Log into YouTube in your browser and access the video/playlist.
        3. Export cookies using the extension (save as `cookies.txt`).
        4. Upload the file below.
        """)
        cookies_file = None
        uploaded_file = st.file_uploader("Upload YouTube Cookies (Optional)", type=["txt"], help="For age-restricted or bot-blocked content.")
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
                        st.error("No subtitles (manual or auto-generated) were downloaded. Check if available.")
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
                    st.error(f"Error: {str(e)}")
                finally:
                    if cookies_file and os.path.exists(cookies_file):
                        os.unlink(cookies_file)  # Clean up temporary cookies file

if __name__ == "__main__":
    main()
