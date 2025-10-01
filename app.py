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

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_transcript(video_id, format_choice='srt'):
    """Fetch transcript using youtube-transcript-api (manual + auto-generated)."""
    try:
        # Get available transcripts
        transcripts = YouTubeTranscriptApi.list_transcripts(video_id)
        transcript_list = []

        # Prioritize manual English
        try:
            transcript = transcripts.find_generated_transcript(['en'])  # Auto-generated
            if transcript:
                transcript_list.append((transcript.fetch(), 'en', True))  # True for auto
        except TranscriptsDisabled:
            pass
        except NoTranscriptFound:
            pass

        # Manual fallback
        try:
            transcript = transcripts.find_transcript(['en'])
            if transcript:
                transcript_list.append((transcript.fetch(), 'en', False))  # False for manual
        except TranscriptsDisabled:
            pass
        except NoTranscriptFound:
            pass

        if not transcript_list:
            # Try any language
            transcript = transcripts.find_transcript(transcripts)
            transcript_list.append((transcript.fetch(), transcript.language_code, False))

        # Use first available (prioritized)
        transcript_data, lang_code, is_auto = transcript_list[0]

        # Format to desired output
        if format_choice == 'srt':
            formatter = SRTFormatter()
        elif format_choice == 'vtt':
            formatter = WebVTTFormatter()
        else:  # txt fallback to SRT then strip
            formatter = SRTFormatter()
            format_choice = 'srt'  # Temp

        sub_text = formatter.format_transcript(transcript_data)

        return sub_text, lang_code, is_auto

    except CouldNotRetrieveTranscript as e:
        raise ValueError(f"Could not retrieve transcript: {str(e)}")
    except Exception as e:
        raise ValueError(f"Error fetching transcript: {str(e)}")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_info(url, is_playlist=False, cookies_file=None):
    """Get video IDs and titles for playlist or single video."""
    if is_playlist:
        # Light yt-dlp for playlist entries
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
        # Fetch title via yt-dlp light extract
        ydl_opts = {'quiet': True, 'no_warnings': True}
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'video_subtitles')
        return [(video_id, title)], title

def convert_srt_to_txt(srt_text):
    """Convert SRT text to plain TXT by stripping timestamps, numbers, and inline tags."""
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
    """Clean subtitle text: Remove ads, extra newlines, etc."""
    text = re.sub(r'\[Advertisement\].*?\n', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def combine_subtitles(subtitle_files, output_dir, title, format_choice):
    """Combine subtitles into a single file."""
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
    """Create zip for separate files (now using text in memory)."""
    zip_buffer = BytesIO()
    safe_title = sanitize_filename(title)[:150]
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for video_title, sub_text in subtitle_files:
            filename = f"{sanitize_filename(video_title)[:150]}.srt"  # Default to SRT for ZIP
            zipf.writestr(filename, sub_text.encode('utf-8'))
    zip_buffer.seek(0)
    return zip_buffer, f"{safe_title}_subtitles.zip"

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=1, max=10))
def download_subtitles(url, format_choice, temp_dir, is_playlist, progress_bar, total_videos, clean_transcript, cookies_file=None):
    """Download subtitles using youtube-transcript-api."""
    subtitle_files = []
    try:
        entries, title = get_info(url, is_playlist, cookies_file)
        if not entries:
            st.error("No videos found in the provided URL.")
            return temp_dir, title, subtitle_files
    except Exception as e:
        st.error(f"Error fetching video info: {str(e)}")
        return temp_dir, "unknown", []

    for i, (video_id, video_title) in enumerate(entries):
        try:
            sub_text, lang_code, is_auto = get_transcript(video_id, format_choice)
            
            if clean_transcript:
                sub_text = clean_subtitle_text(sub_text)
            
            if format_choice == 'txt':
                sub_text = convert_srt_to_txt(sub_text)
            
            subtitle_files.append((video_title, sub_text))
            lang_name = format_language_option(lang_code)
            auto_note = ' (Auto-generated from voice)' if is_auto else ''
            st.info(f"Downloaded subtitles for '{video_title}' in {lang_name}{auto_note}")
        except ValueError as ve:
            st.warning(f"No subtitles available for '{video_title}': {str(ve)}")
        except Exception as e:
            logging.error(f"Error for '{video_title}': {str(e)}")
            st.warning(f"Error for '{video_title}': {str(e)}")
        
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
        **Cookies Instructions**: Optional for restricted content or rate limits. Upload if needed.
        """)
        cookies_file = None
        uploaded_file = st.file_uploader("Upload YouTube Cookies (Optional)", type=["txt"])
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
            value=True
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
                        key="download_scope"
                    )
                if url_type == 'playlist' or (url_type == 'both' and download_scope == 'Entire Playlist'):
                    combine_choice = st.selectbox(
                        "Output Style", 
                        ["separate", "combined"]
                    )
            except ValueError as ve:
                st.error(str(ve))

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
                    
                    _, _ = get_info(selected_url, is_playlist, cookies_file)  # For count
                    total_videos = len(get_info(selected_url, is_playlist, cookies_file)[0]) or 1

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
                        st.error("No subtitles were downloaded. Ensure the video has manual or auto-generated captions.")
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
                            _, sub_text = subtitle_files[0]
                            st.download_button(
                                "Download Subtitle File", 
                                data=sub_text.encode('utf-8'),
                                file_name=f"{sanitize_filename(subtitle_files[0][0])[:150]}.{format_choice}", 
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
                        os.unlink(cookies_file)

if __name__ == "__main__":
    main()
