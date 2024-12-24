Backup your YouTube playlist to your Google Drive
---

The execution of this script is interactive given its required authentication (OAuth). This is used to download your private playlists.

In order to configure OAuth for both Google Drive and YouTube APIs you need to access your Google Cloud console, and download the corresponding client_secrets.json from the UI once you've enabled OAuth for both APIs, and place the file in the root of the project.

Finally, you need ffmpeg installed locally. This is because youtube-dl will use it to join video and audio from youtube DASH streams. 