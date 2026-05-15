import os
import pickle
import json
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from rich.console import Console
from rich.progress import Progress
from dotenv import load_dotenv

load_dotenv()

console = Console()

# --- CONFIGURATION ---
# Fill these in your .env file or directly here
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = "http://127.0.0.1:8888/callback"

GOOGLE_CLIENT_SECRETS_FILE = "client_secrets.json"
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

# --- AUTHENTICATION ---

def get_spotify_client():
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        console.print("[red]Error: Spotify Client ID/Secret not found in .env[/red]")
        return None
    
    auth_manager = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope="playlist-modify-public playlist-modify-private playlist-read-private user-read-private user-read-email",
        show_dialog=True
    )
    sp = spotipy.Spotify(auth_manager=auth_manager)
    
    try:
        user = sp.me()
        console.print(f"[bold cyan]Spotify Account:[/bold cyan] {user['display_name']} ({user['product']})")
        # Check token scopes
        token_info = auth_manager.get_cached_token()
        if token_info:
            console.print(f"[bold cyan]Granted Scopes:[/bold cyan] {token_info.get('scope')}")
    except Exception as e:
        console.print(f"[yellow]Note: Could not fetch user profile details: {e}[/yellow]")
        
    return sp

def get_youtube_client():
    creds = None
    if os.path.exists("yt_token.pickle"):
        with open("yt_token.pickle", "rb") as token:
            creds = pickle.load(token)
            
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(GOOGLE_CLIENT_SECRETS_FILE):
                console.print(f"[red]Error: {GOOGLE_CLIENT_SECRETS_FILE} not found.[/red]")
                return None
            flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CLIENT_SECRETS_FILE, YOUTUBE_SCOPES)
            creds = flow.run_local_server(port=0)
            
        with open("yt_token.pickle", "wb") as token:
            pickle.dump(creds, token)
            
    return build("youtube", "v3", credentials=creds)

# --- TRANSFER LOGIC ---

def get_yt_playlist_tracks(youtube, playlist_id):
    tracks = []
    request = youtube.playlistItems().list(
        part="snippet,contentDetails",
        playlistId=playlist_id,
        maxResults=50
    )
    
    while request:
        response = request.execute()
        for item in response["items"]:
            title = item["snippet"]["title"]
            # Try to extract artist if possible (often "Artist - Title")
            tracks.append(title)
        request = youtube.playlistItems().list_next(request, response)
        
    return tracks

def get_spotify_playlist_tracks(sp, playlist_id):
    tracks = []
    results = sp.playlist_items(playlist_id)
    while results:
        for item in results['items']:
            track = item['track']
            if track:
                tracks.append(f"{track['name']} {track['artists'][0]['name']}")
        if results['next']:
            results = sp.next(results)
        else:
            results = None
    return tracks

def transfer_yt_to_spotify(youtube, sp, yt_playlist_id):
    # 1. Get YT playlist title
    pl_info = youtube.playlists().list(part="snippet", id=yt_playlist_id).execute()
    pl_name = pl_info["items"][0]["snippet"]["title"]
    
    # 2. Get tracks
    console.print(f"[bold blue]Fetching tracks from YouTube playlist: {pl_name}[/bold blue]")
    yt_tracks = get_yt_playlist_tracks(youtube, yt_playlist_id)
    
    # 3. Create Spotify Playlist
    try:
        # Use the absolute most modern endpoint to avoid ID issues
        data = {
            "name": f"{pl_name} (from YT)",
            "public": True,
            "description": "Transferred from YouTube using SyncTune"
        }
        new_pl = sp._post("me/playlists", payload=data)
        new_pl_id = new_pl["id"]
    except spotipy.exceptions.SpotifyException as e:
        console.print(f"[bold red]Spotify API Error:[/bold red] {e}")
        console.print(f"[yellow]Common causes:[/yellow]")
        console.print("1. Your email is not added to the 'User Management' in the Spotify Developer Dashboard.")
        console.print("2. You are logged into a different Spotify account in your browser than the one added in the dashboard.")
        console.print("3. The app is still in 'Development' mode and requires explicit user whitelist.")
        raise e
    
    # 4. Search and Add
    track_ids = []
    with Progress() as progress:
        task = progress.add_task("[green]Transferring to Spotify...", total=len(yt_tracks))
        for track_query in yt_tracks:
            results = sp.search(q=track_query, limit=1, type="track")
            items = results["tracks"]["items"]
            if items:
                track_ids.append(items[0]["uri"])
                if len(track_ids) >= 100: # Spotify limit per request
                    sp.playlist_add_items(new_pl_id, track_ids)
                    track_ids = []
            progress.update(task, advance=1)
            
        if track_ids:
            sp.playlist_add_items(new_pl_id, track_ids)
            
    console.print(f"[bold green]Success! Transferred to Spotify: {pl_name}[/bold green]")

def transfer_spotify_to_yt(sp, youtube, sp_playlist_id):
    # 1. Get Spotify playlist title
    pl_info = sp.playlist(sp_playlist_id)
    pl_name = pl_info["name"]
    
    # 2. Get tracks
    console.print(f"[bold green]Fetching tracks from Spotify playlist: {pl_name}[/bold green]")
    sp_tracks = get_spotify_playlist_tracks(sp, sp_playlist_id)
    
    # 3. Create YouTube Playlist
    request = youtube.playlists().insert(
        part="snippet,status",
        body={
          "snippet": {
            "title": f"{pl_name} (from Spotify)",
            "description": "Transferred from Spotify using SyncTune"
          },
          "status": {
            "privacyStatus": "private"
          }
        }
    )
    new_pl = request.execute()
    new_pl_id = new_pl["id"]
    
    # 4. Search and Add
    with Progress() as progress:
        task = progress.add_task("[red]Transferring to YouTube...", total=len(sp_tracks))
        for track_query in sp_tracks:
            # Search for the track
            search_request = youtube.search().list(
                q=track_query,
                part="snippet",
                maxResults=1,
                type="video"
            )
            search_response = search_request.execute()
            
            if search_response["items"]:
                video_id = search_response["items"][0]["id"]["videoId"]
                # Add to playlist
                youtube.playlistItems().insert(
                    part="snippet",
                    body={
                        "snippet": {
                            "playlistId": new_pl_id,
                            "resourceId": {
                                "kind": "youtube#video",
                                "videoId": video_id
                            }
                        }
                    }
                ).execute()
            progress.update(task, advance=1)

    console.print(f"[bold red]Success! Transferred to YouTube: {pl_name}[/bold red]")

# --- DIAGNOSTICS ---

def check_setup():
    missing = []
    if os.path.exists(".env"):
        with open(".env", "r") as f:
            content = f.read()
            if "your_spotify_client_id" in content or "your_spotify_client_secret" in content:
                missing.append(".env (Spotify credentials - still using placeholders)")
    else:
        missing.append(".env (Spotify credentials)")
        
    if not os.path.exists(GOOGLE_CLIENT_SECRETS_FILE):
        missing.append(f"{GOOGLE_CLIENT_SECRETS_FILE} (YouTube credentials)")
    
    if missing:
        console.rule("[bold red]Setup Required[/bold red]")
        console.print("\n[yellow]The following files are missing:[/yellow]")
        for item in missing:
            console.print(f" - {item}")
        console.print("\n[cyan]Please follow the instructions in README.md to set up your credentials.[/cyan]")
        return False
    return True

# --- MAIN RUNNER ---

if __name__ == "__main__":
    console.rule("[bold cyan]SyncTune - Playlist Porter[/bold cyan]")
    
    if not check_setup():
        exit()
        
    sp = get_spotify_client()
    yt = get_youtube_client()
    
    if not sp or not yt:
        console.print("[yellow]Authentication failed. Check your credentials in .env and client_secrets.json.[/yellow]")
        exit()
        
    console.print("\n1. YouTube -> Spotify")
    console.print("2. Spotify -> YouTube")
    choice = input("\nChoose direction (1 or 2): ")
    
    playlist_id = input("Enter the source Playlist ID (or URL): ")
    if "list=" in playlist_id:
        playlist_id = playlist_id.split("list=")[1].split("&")[0]
    elif "playlist/" in playlist_id:
        playlist_id = playlist_id.split("playlist/")[1].split("?")[0]
        
    if choice == "1":
        transfer_yt_to_spotify(yt, sp, playlist_id)
    elif choice == "2":
        transfer_spotify_to_yt(sp, yt, playlist_id)
    else:
        console.print("[red]Invalid choice.[/red]")
