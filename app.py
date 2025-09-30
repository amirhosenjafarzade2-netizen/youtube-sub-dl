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

def get_first_video_id(info):
    """Helper to extract first video ID from playlist info."""
    if 'entries' in info and info['entries']:
        return info['entries'][0].get('id')
    return None

def get_info(url, is_playlist):
    """Fetch info for video or playlist."""
    ydl_opts = {
        'quiet': True,
        'extract_flat': True if is_playlist else False,
        'no_warnings': True,
    }
    with YoutubeDL(ydl_opts) as ydl:
        result = ydl.extract_info(url, download=False)
        if is_playlist:
            if 'entries' not in result or not result['entries']:
                raise Exception("No videos found in playlist.")
            return result['entries'], result.get('title', 'playlist_subtitles')
        else:
            return [result], result.get('title', 'video_subtitles')

@st.cache_data
def get_available_subtitle_languages(url, is_playlist):
    """Fetch available subtitle languages for the video or first video in playlist."""
    ydl_opts = {
        'quiet': True,
        'listsubtitles': True,
        'no_warnings': True,
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if is_playlist:
            first_video_id = get_first_video_id(info)
            if first_video_id:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={first_video_id}", download=False)
        
        if 'automatic_captions' in info and info['automatic_captions']:
            return list(info['automatic_captions'].keys())
        
        return []

@st.cache_data
def get_available_manual_languages(url, is_playlist):
    """Fetch available manual subtitle languages."""
    ydl_opts = {
        'quiet': True,
        'listsubtitles': True,
        'no_warnings': True,
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if is_playlist:
            first_video_id = get_first_video_id(info)
            if first_video_id:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={first_video_id}", download=False)
        
        if 'subtitles' in info and info['subtitles']:
            return list(info['subtitles'].keys())
        
        return []

def find_subtitle_file(base_path, lang_list, format_choice):
    """Find subtitle file for any of the selected languages, checking various extensions and variants."""
    if not isinstance(lang_list, list):
        lang_list = [lang_list]
    
    selected_language = None
    for lang in lang_list:
        lang_variants = [lang]
        if '-' not in lang:
            lang_variants.append(f"{lang}-orig")
        elif lang.endswith('-orig'):
            lang_variants.append(lang.replace('-orig', ''))
        
        patterns = []
        for variant in lang_variants:
            patterns.extend([
                f"{base_path}.{variant}.{format_choice}",
                f"{base_path}.{variant}.auto.{format_choice}",
                f"{base_path}.{variant}.srt",
                f"{base_path}.{variant}.auto.srt",
                f"{base_path}.{variant}.vtt",
                f"{base_path}.{variant}.auto.vtt",
            ])
        
        for pattern in patterns:
            matches = glob.glob(pattern)
            if matches:
                selected_language = lang
                return matches[0], selected_language
    
    wildcard_patterns = [
        f"{base_path}.*.{format_choice}",
        f"{base_path}.*.srt",
        f"{base_path}.*.vtt",
    ]
    for pattern in wildcard_patterns:
        matches = glob.glob(pattern)
        if matches:
            selected_language = matches[0].split('.')[-2]  # Extract language code
            return matches[0], selected_language
    
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

def download_subtitles(url, sub_type, lang, format_choice, temp_dir, is_playlist, progress_bar, total_videos, clean_transcript, prefer_manual_only):
    """Download subtitles for video or playlist."""
    lang_list = lang if isinstance(lang, list) else [lang]
    
    ydl_opts = {
        'writesubtitles': True,
        'writeautomaticsub': sub_type == 'auto' or not prefer_manual_only,
        'subtitleslangs': lang_list,
        'subtitlesformat': 'vtt' if format_choice == 'vtt' else 'srt',
        'skip_download': True,
        'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
    }
    
    subtitle_files = []
    entries, title = get_info(url, is_playlist)
    
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
            
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_url])
            
            sub_path, selected_language = find_subtitle_file(base_path, lang_list, format_choice)
            
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
                st.warning(f"No subtitles found for '{video_title}' in language(s): {', '.join(format_language_option(code) for code in lang_list)}")
        
        except Exception as e:
            st.warning(f"Error downloading subtitles for '{video_title}': {str(e)}")
        
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
        sub_type = st.selectbox(
            "Subtitle Type", 
            ["Original (all languages)", "Auto-translate"], 
            help="Original: Manual subtitles (with auto-fallback if enabled); Auto-translate: Automatic captions/translations."
        )
        
        lang = ['en']
        lang_codes = ['en']
        prefer_manual_only = False
        download_scope = 'Entire Playlist'
        available_languages = []
        available_manual_langs = []
        
        if st.button("Clear Language Cache"):
            get_available_subtitle_languages.clear()
            get_available_manual_languages.clear()
            st.info("Language cache cleared. Re-enter the URL to refresh available languages.")
        
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
                
                selected_url = corrected_playlist_url if url_type == 'playlist' or (url_type == 'both' and download_scope == 'Entire Playlist') else corrected_video_url
                is_playlist_temp = url_type == 'playlist' or (url_type == 'both' and download_scope == 'Entire Playlist')
                
                available_languages = get_available_subtitle_languages(selected_url, is_playlist_temp)
                available_manual_langs = get_available_manual_languages(selected_url, is_playlist_temp)
                
                if not available_languages:
                    st.info("No automatic captions detected. Defaulting to English.")
                    available_languages = ['en']
                if not available_manual_langs:
                    st.info("No manual subtitles detected. Defaulting to English.")
                    available_manual_langs = ['en']
            
            except ValueError as ve:
                st.error(str(ve))
            except Exception as e:
                st.error(f"Error validating URL: {str(e)}")
        else:
            available_languages = ['en']
            available_manual_langs = ['en']
        
        if sub_type == 'Original (all languages)':
            lang_options = {format_language_option(code): code for code in available_manual_langs}
            default_display = format_language_option('tr' if 'tr' in available_manual_langs else available_manual_langs[0])
            
            selected_display = st.multiselect(
                "Select Original Languages", 
                list(lang_options.keys()), 
                default=[default_display], 
                help="Select languages; first available language per video will be used. Auto-fallback included unless disabled."
            )
            
            lang_codes = [lang_options[display] for display in selected_display]
            if not lang_codes:
                st.warning("No language selected. Defaulting to English.")
                lang_codes = ['en']
            
            prefer_manual_only = st.checkbox(
                "Prefer Manual Only (No Auto-Fallback)", 
                value=False, 
                help="Disable auto-generated subs if manual ones are missing."
            )
        else:
            lang_options = {format_language_option(code): code for code in available_languages}
            default_index = 0
            if available_languages and 'tr' in available_languages:
                default_display = format_language_option('tr')
                if default_display in lang_options:
                    default_index = list(lang_options.keys()).index(default_display)
            
            selected_display = st.selectbox(
                "Auto-translate Language", 
                list(lang_options.keys()), 
                index=default_index, 
                help="Select a language for auto-translated subtitles"
            )
            
            lang_codes = lang_options[selected_display]

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
        if url and 'list' in url and download_scope != "Single Video":
            combine_choice = st.selectbox(
                "Output Style", 
                ["separate", "combined"], 
                help="Separate: Individual files in ZIP; Combined: Single file."
            )

    if st.button("Download Subtitles", type="primary"):
        if not url:
            st.error("Please enter a YouTube URL.")
            return

        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                corrected_playlist_url, corrected_video_url, url_type = validate_url(url)
                is_playlist = url_type == 'playlist' or (url_type == 'both' and download_scope == "Entire Playlist")
                selected_url = corrected_playlist_url if is_playlist else corrected_video_url
                
                st.info(f"Detected: {'Playlist' if is_playlist else 'Single Video'} - Using URL: {selected_url}")

                progress_container = st.empty()
                progress_bar = progress_container.progress(0.0)
                
                entries, _ = get_info(selected_url, is_playlist)
                total_videos = len(entries) or 1

                output_dir, title, subtitle_files = download_subtitles(
                    selected_url, 
                    'auto' if 'Auto' in sub_type else 'original', 
                    lang_codes, 
                    format_choice, 
                    temp_dir, 
                    is_playlist, 
                    progress_bar, 
                    total_videos, 
                    clean_transcript, 
                    prefer_manual_only
                )

                progress_container.empty()

                if not subtitle_files:
                    st.error("No subtitles were downloaded from any videos. Please check if subtitles are available in the selected language(s), or try enabling auto-fallback.")
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

if __name__ == "__main__":
    main()
