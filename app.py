import streamlit as st
import os
import zipfile
import shutil
import re
from urllib.parse import urlparse, parse_qs
from yt_dlp import YoutubeDL
import tempfile
from io import BytesIO

@st.cache_data
def validate_url(url):
    """Validate and classify the URL, return corrected URL, type, and video ID if present."""
    parsed_url = urlparse(url)
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

@st.cache_data
def get_info(url, is_playlist):
    """Fetch info for video or playlist."""
    ydl_opts = {
        'quiet': True,
        'extract_flat': True if is_playlist else False,
    }
    with YoutubeDL(ydl_opts) as ydl:
        result = ydl.extract_info(url, download=False)
        if is_playlist:
            if 'entries' not in result:
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
        'extract_flat': True if is_playlist else False,
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if is_playlist:
            if 'entries' in info and info['entries']:
                info = ydl.extract_info(info['entries'][0]['url'], download=False)
        if 'automatic_captions' in info:
            return list(info['automatic_captions'].keys())
        return ['en']  # Default to English if no languages found

@st.cache_data
def get_available_manual_languages(url, is_playlist):
    """Fetch available manual subtitle languages."""
    ydl_opts = {'quiet': True, 'listsubtitles': True, 'extract_flat': True if is_playlist else False}
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if is_playlist and 'entries' in info and info['entries']:
            info = ydl.extract_info(info['entries'][0]['url'], download=False)
        return list(info.get('subtitles', {}).keys()) or ['en']  # Fallback to English

def find_subtitle_file(base_path, lang):
    """Find subtitle file, checking for .srt and .auto.srt variants."""
    for ext in ['.srt', f'.{lang}.srt', f'.{lang}.auto.srt']:
        candidate = base_path + ext
        if os.path.exists(candidate):
            return candidate
    return None

def download_subtitles(url, sub_type, lang, format_choice, output_dir, is_playlist, progress_bar, total_videos, clean_transcript, prefer_manual_only):
    """Download subtitles with progress bar."""
    os.makedirs(output_dir, exist_ok=True)

    if is_playlist:
        entries, title = get_info(url, True)
    else:
        entries, title = get_info(url, False)
        entries = [{'url': entry['webpage_url'], 'title': entry['title']} for entry in entries]

    ydl_opts = {
        'skip_download': True,
        'outtmpl': os.path.join(output_dir, '%(title)s.%(ext)s'),
        'subtitlesformat': 'srt/best',  # Force SRT for consistency
    }

    if format_choice == 'vtt':
        ydl_opts['convertsubtitles'] = 'vtt'

    langs = lang if isinstance(lang, list) else [lang]
    if sub_type == 'original':
        ydl_opts['writesubtitles'] = True  # Always enable manual
        ydl_opts['writeautomaticsub'] = not prefer_manual_only  # Fallback to auto if allowed
        ydl_opts['subtitleslangs'] = langs if langs else ['all']
    else:  # auto-translate
        ydl_opts['writesubtitles'] = False
        ydl_opts['writeautomaticsub'] = True
        ydl_opts['subtitleslangs'] = langs

    subtitle_files = []
    for i, entry in enumerate(entries, 1):
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(entry['url'], download=True)
                base_path = ydl.prepare_filename(info).rsplit('.', 1)[0]
                sub_file = None
                for l in langs:
                    candidate = find_subtitle_file(base_path, l)
                    if candidate:
                        sub_file = candidate
                        break
                if sub_file:
                    final_file = sub_file
                    if format_choice == 'txt':
                        final_file = convert_srt_to_txt(sub_file)
                    if clean_transcript:
                        with open(final_file, 'r+', encoding='utf-8') as f:
                            content = f.read()
                            f.seek(0)
                            f.write(clean_subtitle_text(content))
                            f.truncate()
                    subtitle_files.append((entry['title'], final_file))
                else:
                    # List available subs for debugging
                    list_opts = {'listsubtitles': True, 'quiet': True}
                    with YoutubeDL(list_opts) as ydl_list:
                        list_info = ydl_list.extract_info(entry['url'], download=False)
                        avail_manual = list(list_info.get('subtitles', {}).keys())
                        avail_auto = list(list_info.get('automatic_captions', {}).keys())
                        st.warning(f"No subtitle found for '{entry['title']}'. Available manual: {avail_manual}. Available auto: {avail_auto}.")
            # Update progress bar
            progress_bar.progress(min(i / total_videos, 1.0))
        except Exception as e:
            st.warning(f"Error for '{entry.get('title', 'unknown')}': {e}")

    return output_dir, title, subtitle_files, is_playlist

def convert_srt_to_txt(srt_path):
    """Convert SRT to plain TXT by stripping timestamps and numbers."""
    txt_path = srt_path.rsplit('.', 1)[0] + '.txt'
    with open(srt_path, 'r', encoding='utf-8') as srt_file, open(txt_path, 'w', encoding='utf-8') as txt_file:
        for line in srt_file:
            if not re.match(r'^\d+$', line.strip()) and not re.match(r'^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$', line.strip()):
                if line.strip():
                    txt_file.write(line)
    return txt_path

def clean_subtitle_text(text):
    """Clean subtitle text: Remove ads, extra newlines, etc."""
    text = re.sub(r'\[Advertisement\].*?\n', '', text, flags=re.DOTALL)  # Example ad removal
    text = re.sub(r'\n{3,}', '\n\n', text)  # Normalize breaks
    return text

def combine_subtitles(subtitle_files, output_dir, title, format_choice):
    """Combine subtitles into a single file."""
    ext = format_choice
    combined_file = os.path.join(output_dir, f"{title.replace(' ', '_')}_combined.{ext}")
    cue_index = 1
    with open(combined_file, 'w', encoding='utf-8') as outfile:
        for video_title, sub_path in subtitle_files:
            outfile.write(f"\n\n=== {video_title} ===\n\n")
            if format_choice in ['srt', 'vtt']:
                with open(sub_path, 'r', encoding='utf-8') as infile:
                    for line in infile:
                        if re.match(r'^\d+$', line.strip()):
                            outfile.write(f"{cue_index}\n")
                            cue_index += 1
                        else:
                            outfile.write(line)
            else:  # txt
                with open(sub_path, 'r', encoding='utf-8') as infile:
                    outfile.write(infile.read())
    return combined_file

def create_zip(subtitle_files, title):
    """Create zip for separate files."""
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for _, sub_path in subtitle_files:
            zipf.write(sub_path, os.path.basename(sub_path))
    zip_buffer.seek(0)
    return zip_buffer, f"{title.replace(' ', '_')}_subtitles.zip"

def main():
    st.set_page_config(page_title="YouTube Subtitle Downloader", page_icon="🎥", layout="wide")
    st.title("YouTube Subtitle Downloader 🎥")
    st.markdown("Download subtitles from YouTube videos or playlists with a sleek interface and progress tracking!")

    with st.sidebar:
        st.header("Settings")
        url = st.text_input("YouTube URL", placeholder="Paste video or playlist URL here...")
        sub_type = st.selectbox("Subtitle Type", ["Original (all languages)", "Auto-translate"], help="Original: Manual subtitles (with auto-fallback if enabled); Auto-translate: Automatic captions/translations.")
        
        # Initialize lang variable
        lang = 'en'  # Default
        available_languages = ['en']  # Default fallback
        available_manual_langs = ['en']
        if url:
            try:
                corrected_playlist_url, corrected_video_url, url_type = validate_url(url)
                selected_url = corrected_playlist_url if url_type == 'playlist' or (url_type == 'both' and st.session_state.get('download_scope', 'Entire Playlist') == 'Entire Playlist') else corrected_video_url
                is_playlist_temp = url_type == 'playlist' or (url_type == 'both' and st.session_state.get('download_scope', 'Entire Playlist') == 'Entire Playlist')
                available_languages = get_available_subtitle_languages(selected_url, is_playlist_temp)
                available_manual_langs = get_available_manual_languages(selected_url, is_playlist_temp)
            except:
                available_languages = ['en']  # Fallback if URL is invalid
                available_manual_langs = ['en']
        if sub_type == 'Original (all languages)':
            lang = st.multiselect("Select Original Languages", available_manual_langs, default=available_manual_langs, help="Select specific languages or leave for all.")
            prefer_manual_only = st.checkbox("Prefer Manual Only (No Auto-Fallback)", value=False, help="Disable auto-generated subs if manual ones are missing.")
        else:
            lang = st.selectbox("Auto-translate Language", available_languages, help="Select a language for auto-translated subtitles")

        format_choice = st.selectbox("Format", ["srt", "vtt", "txt"], help="SRT/VTT include timestamps; TXT is plain text.")
        clean_transcript = st.checkbox("Clean Transcript (Remove ads/timestamps)", value=True, help="Remove advertisements and normalize formatting in subtitles.")
        download_scope = None
        if url and 'list' in url and 'v' in url:
            download_scope = st.selectbox("Download Scope", ["Entire Playlist", "Single Video"], help="Choose whether to download for the playlist or just the video in the URL", key="download_scope")
        combine_choice = None
        if url and 'list' in url and (download_scope != "Single Video"):
            combine_choice = st.selectbox("Output Style", ["separate", "combined"], help="Separate: Individual files in ZIP; Combined: Single file.")

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

                # Initialize progress bar
                progress_container = st.empty()
                progress_bar = progress_container.progress(0.0)
                total_videos = len(get_info(selected_url, is_playlist)[0])

                output_dir, title, subtitle_files, _ = download_subtitles(
                    selected_url, sub_type.lower().split()[0], lang, format_choice, temp_dir, is_playlist, progress_bar, total_videos, clean_transcript, prefer_manual_only if sub_type == 'Original (all languages)' else False
                )

                # Clear progress bar
                progress_container.empty()

                if not subtitle_files:
                    st.warning("No subtitles downloaded. Try enabling auto-fallback or check available languages.")
                    return

                if is_playlist and combine_choice == 'combined':
                    # Combined file, no zip
                    combined_file = combine_subtitles(subtitle_files, output_dir, title, format_choice)
                    with open(combined_file, 'rb') as f:
                        st.download_button("Download Combined File", f, file_name=os.path.basename(combined_file), mime="text/plain")
                elif is_playlist and combine_choice == 'separate':
                    # Zip for separate
                    zip_buffer, zip_name = create_zip(subtitle_files, title)
                    st.download_button("Download ZIP", zip_buffer, file_name=zip_name, mime="application/zip")
                else:
                    # Single video, single file
                    if len(subtitle_files) == 1:
                        _, sub_path = subtitle_files[0]
                        with open(sub_path, 'rb') as f:
                            st.download_button("Download Subtitle File", f, file_name=os.path.basename(sub_path), mime="text/plain")
                    else:
                        st.error("Unexpected error.")

            except ValueError as ve:
                st.error(str(ve))
            except Exception as e:
                st.error(f"Error: {str(e)}")

if __name__ == "__main__":
    main()
