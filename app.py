import streamlit as st
import os
import zipfile
import shutil
import re
from urllib.parse import urlparse, parse_qs
from yt_dlp import YoutubeDL
import tempfile
from io import BytesIO

def validate_url(url):
    """Validate and classify the URL as playlist or single video, return corrected URL and type."""
    parsed_url = urlparse(url)
    query_params = parse_qs(parsed_url.query)
    
    if 'list' in query_params:
        playlist_id = query_params['list'][0]
        corrected_url = f"https://www.youtube.com/playlist?list={playlist_id}"
        return corrected_url, 'playlist'
    elif 'v' in query_params:
        video_id = query_params['v'][0]
        corrected_url = f"https://www.youtube.com/watch?v={video_id}"
        return corrected_url, 'video'
    else:
        raise ValueError("Invalid YouTube URL. Please provide a video or playlist URL.")

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

def download_subtitles(url, sub_type, lang, format_choice, output_dir, is_playlist):
    """Download subtitles."""
    os.makedirs(output_dir, exist_ok=True)

    if is_playlist:
        entries, title = get_info(url, True)
    else:
        entries, title = get_info(url, False)
        entries = [{'url': entry['webpage_url'], 'title': entry['title']} for entry in entries]

    ydl_opts = {
        'skip_download': True,
        'outtmpl': os.path.join(output_dir, '%(title)s.%(ext)s'),
        'subtitlesformat': 'srt' if format_choice == 'srt' else 'srt',  # Always download as srt, convert later if txt
    }

    if sub_type == 'original':
        ydl_opts['writesubtitles'] = True
        ydl_opts['writeautomaticsub'] = False
        ydl_opts['subtitleslangs'] = ['all']
    else:  # auto-translate
        ydl_opts['writesubtitles'] = False
        ydl_opts['writeautomaticsub'] = True
        ydl_opts['subtitleslangs'] = [lang]

    subtitle_files = []
    with YoutubeDL(ydl_opts) as ydl:
        for entry in entries:
            try:
                info = ydl.extract_info(entry['url'], download=True)
                # Find the downloaded subtitle file
                sub_file = ydl.prepare_filename(info).rsplit('.', 1)[0] + f".{lang if sub_type == 'auto' else 'en'}.srt"  # Adjust lang
                if os.path.exists(sub_file):
                    if format_choice == 'txt':
                        txt_file = convert_srt_to_txt(sub_file)
                        subtitle_files.append((entry['title'], txt_file))
                    else:
                        subtitle_files.append((entry['title'], sub_file))
                else:
                    print(f"No subtitle found for {entry['title']}")
            except Exception as e:
                print(f"Error for {entry.get('title', 'unknown')}: {e}")

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

def combine_subtitles(subtitle_files, output_dir, title, format_choice):
    """Combine subtitles into a single file."""
    ext = format_choice
    combined_file = os.path.join(output_dir, f"{title.replace(' ', '_')}_combined.{ext}")
    cue_index = 1
    with open(combined_file, 'w', encoding='utf-8') as outfile:
        for video_title, sub_path in subtitle_files:
            outfile.write(f"\n\n=== {video_title} ===\n\n")
            if format_choice == 'srt':
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
    st.title("YouTube Subtitle Downloader 🎥")
    st.markdown("Download subtitles from YouTube videos or playlists with customizable options!")

    with st.sidebar:
        st.header("Settings")
        url = st.text_input("YouTube URL", placeholder="Paste video or playlist URL here...")
        sub_type = st.selectbox("Subtitle Type", ["Original (all languages)", "Auto-translate"])
        lang = 'en' if sub_type == 'original' else st.text_input("Auto-translate Language Code", value="en", help="e.g., en, fr, es")
        format_choice = st.selectbox("Format", ["srt", "txt"])
        combine_choice = None
        if url and 'list' in url:
            combine_choice = st.selectbox("Output Style", ["separate", "combined"])

    if st.button("Download Subtitles", type="primary"):
        if not url:
            st.error("Please enter a YouTube URL.")
            return

        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                corrected_url, url_type = validate_url(url)
                is_playlist = url_type == 'playlist'
                st.info(f"Detected: {'Playlist' if is_playlist else 'Single Video'} - Using URL: {corrected_url}")

                output_dir, title, subtitle_files, _ = download_subtitles(corrected_url, sub_type.lower().split()[0], lang, format_choice, temp_dir, is_playlist)

                if not subtitle_files:
                    st.warning("No subtitles downloaded.")
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
                    # Single video or non-playlist, single file
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
