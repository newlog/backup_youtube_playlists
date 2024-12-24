import logging
import os
import unicodedata
from pathlib import Path

from googleapiclient.http import MediaFileUpload
from yt_dlp import YoutubeDL
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


def authenticate_google_drive():
    scopes = ["https://www.googleapis.com/auth/drive"]
    client_secret_file = "client_secrets.json"
    flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, scopes)
    credentials = flow.run_local_server(port=0)
    service = build("drive", "v3", credentials=credentials)
    return service


def authenticate_youtube():
    scopes = ['https://www.googleapis.com/auth/youtube.readonly']
    client_secret_file = './client_secrets.json'
    # Authenticate and return the YouTube service client
    flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, scopes)
    credentials = flow.run_local_server(port=0)
    youtube = build('youtube', 'v3', credentials=credentials)
    return youtube


def get_or_create_folder(drive_service, folder_name, parent_id=None):
    # Search for the folder in the parent folder
    query = f"mimeType='application/vnd.google-apps.folder' and trashed=false and name='{folder_name}'"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    else:
        query += " and 'root' in parents"

    response = drive_service.files().list(q=query, fields="files(id, name)").execute()
    files = response.get('files', [])
    if files:
        return files[0]['id']

    folder_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        folder_metadata["parents"] = [parent_id]

    folder = drive_service.files().create(body=folder_metadata, fields="id").execute()
    print(f"Created folder '{folder_name}' with ID: {folder['id']}")
    return folder['id']


def get_uploaded_files_in_folder(drive, folder_id):
    uploaded_files = set()
    files = drive.ListFile({"q": f"'{folder_id}' in parents and trashed=false"}).GetList()
    for file in files:
        uploaded_files.add(file['title'])
    return uploaded_files


def create_download_folder(download_folder):
    if not os.path.exists(download_folder):
        os.makedirs(download_folder)


def is_video_uploaded(video_title, uploaded_files):
    return video_title in uploaded_files


def get_video_size(file_path):
    return os.path.getsize(file_path) / (1024 * 1024)


def get_playlists_from_channel(youtube_service):
    playlists = []
    try:
        # Request the playlists from the YouTube channel
        request = youtube_service.playlists().list(
            part="snippet",
            mine=True,
            maxResults=50  # Max playlists per request
        )
        response = request.execute()

        for item in response['items']:
            playlists.append({
                'id': item['id'],
                'title': item['snippet']['title'],
            })
    except HttpError as e:
        print(f"An error occurred: {e}")
    return playlists


def get_video_urls_from_playlist(youtube_service, playlist_title, playlist_id):
    print(f"Getting videos for playlist {playlist_title}")
    videos = []
    request = youtube_service.playlistItems().list(
        part="snippet",
        playlistId=playlist_id,
        maxResults=50  # Max items per request
    )

    while request:
        response = request.execute()

        for item in response['items']:
            videos.append({
                'id': item['snippet']['resourceId']['videoId'],
                'title': custom_sanitize(item['snippet']['title']),
            })

        # Check if there's a next page
        request = youtube_service.playlistItems().list_next(request, response)

    return videos


def remotely_backup_videos(videos, download_folder, max_storage_mb, drive_service, folder_id):
    uploaded_files = list_files_in_folder(drive_service, folder_id)
    uploaded_files_without_extensions = [os.path.splitext(uploaded_file)[0] for uploaded_file in uploaded_files]
    total_size = 0
    downloaded_files = []

    for video in videos:
        video_url = f"https://www.youtube.com/watch?v={video["id"]}"
        video_title = f"{video["title"]}"
        try:
            if is_video_uploaded(video_title, uploaded_files_without_extensions):
                print(f"Skipping already uploaded video: {video_title}")
                continue

            print(f"Downloading video {video_title}")
            video_path = download_video_with_ytdlp(video_url, video_title, download_folder)
            file_size = get_video_size(video_path)

            if total_size + file_size > max_storage_mb:
                os.remove(video_path)
                print("Storage quota reached. Stopping downloads for this playlist.")
                break

            downloaded_files.append(video_path)
            total_size += file_size
            print(f"Downloaded: {video_title} ({file_size:.2f} MB)")
        except Exception as e:
            logging.error(f"Error downloading {video_title}. Error: {e}")

    upload_videos_to_drive(drive_service, folder_id, downloaded_files)

    for file in downloaded_files:
        os.remove(file)


def custom_sanitize(title):
    return ''.join(c if c.isalnum() or c in " .-_()" else '_' for c in title)


def download_video_with_ytdlp_without_size_limit(video_url, video_title, download_folder):
    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',  # Select best quality
        'outtmpl': f'{download_folder}/{video_title}.%(ext)s',
        'noplaylist': True,  # Ensure only a single video is downloaded
        # If there are problems with permissions, one can use hardcoded cookies or cookies from interactive browser
        # 'cookiefile': 'cookies.txt'
        # 'cookiesfrombrowser': ('chrome',)
    }

    with YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(video_url, download=True)
        video_title = info_dict.get('title', 'Unknown Title')
        print(f"Downloaded: {video_title}")
    return info_dict["requested_downloads"][0]["filepath"]  # filepath = full path. filename = only filename


def select_best_dash_stream_limited_by_size(video_info_dict, size_limit_bytes=None):
    video_formats = [
        fmt for fmt in video_info_dict['formats']
        if fmt.get('vcodec') != "none" and fmt.get('acodec') == "none"
    ]
    audio_formats = [
        fmt for fmt in video_info_dict['formats']
        if fmt.get('acodec') != "none" and fmt.get('vcodec') == "none"
    ]
    video_formats.sort(key=lambda f: f.get('height') or 0, reverse=True)
    audio_formats.sort(key=lambda f: f.get('abr') or 0, reverse=True)

    for video in video_formats:
        video_size = video.get('filesize') or video.get('filesize_approx')
        if not video_size:
            continue

        for audio in audio_formats:
            audio_size = audio.get('filesize') or audio.get('filesize_approx')
            if not audio_size:
                continue

            total_size = video_size + audio_size
            if total_size is None or total_size <= size_limit_bytes:
                return video['format_id'], audio['format_id']
            else:
                print("Found video+audio above size limits. Skipping...")

    return None, None


def download_video_with_ytdlp(video_url, video_title, download_folder, size_limit_mb=1024 * 6):  # 6GB
    size_limit_bytes = size_limit_mb * 1024 * 1024  # Convert MB to bytes
    download_filepath = None

    def select_format(video_info_dict):
        video_id, audio_id = select_best_dash_stream_limited_by_size(video_info_dict, size_limit_bytes)
        if video_id and audio_id:
            return f"{video_id}+{audio_id}"  # Use combined format syntax
        return None

    ydl_opts = {
        'outtmpl': f'{download_folder}/{video_title}.%(ext)s',
        'noplaylist': True,  # Ensure only a single video is downloaded
        # If there are problems with permissions, one can use hardcoded cookies or cookies from interactive browser
        # 'cookiefile': 'cookies.txt'
        # 'cookiesfrombrowser': ('chrome',)
    }

    with YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(video_url, download=False)
        selected_format = select_format(info_dict)
        if not selected_format:
            print(f"No available formats are below {size_limit_mb} MB for this video.")
            raise ValueError("Could not find proper stream to download.")

        ydl.params['format'] = selected_format
        filename = ydl.prepare_filename(info_dict)
        ydl.download([video_url])
        print(f"Downloaded: {video_title}")
    return filename


def upload_videos_to_drive(drive, folder_id, downloaded_files):
    for file_path in downloaded_files:
        upload_file_to_drive(drive, folder_id, file_path)


def upload_file_to_drive(drive_service, folder_id, file_path):
    filename = Path(file_path).name
    print(f"Uploading file {filename}")
    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(
        file_path, resumable=True
    )
    file = (
        drive_service.files()
        .create(body=file_metadata, media_body=media, fields="id")
        .execute()
    )
    print(f'File ID: "{file.get("id")}".')
    return file.get("id")


def list_files_in_folder(drive_service, folder_id):
    query = f"'{folder_id}' in parents"

    # Call the Drive API
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get('files', [])
    return [item["name"] for item in items]


if __name__ == "__main__":
    DOWNLOAD_FOLDER = "./downloads"
    MAX_STORAGE_MB = 2048 * 2 * 10
    create_download_folder(DOWNLOAD_FOLDER)

    drive_service = authenticate_google_drive()
    backups_folder_id = get_or_create_folder(drive_service, f"backups")
    youtube_folder_id = get_or_create_folder(drive_service, f"youtube", parent_id=backups_folder_id)

    youtube_service = authenticate_youtube()
    playlists = get_playlists_from_channel(youtube_service)
    if playlists:
        for playlist in playlists:
            if playlist["title"] in ["Watch Later", "Liked Videos"]:
                continue
            videos_data = get_video_urls_from_playlist(youtube_service, playlist["title"], playlist["id"])
            playlist_folder_id = get_or_create_folder(drive_service, playlist["title"], parent_id=youtube_folder_id)
            remotely_backup_videos(videos_data, DOWNLOAD_FOLDER, MAX_STORAGE_MB, drive_service, playlist_folder_id)
    else:
        print("No playlists found on the channel.")

    print("All videos downloaded and uploaded successfully!")
