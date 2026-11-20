using System;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace MusicBeePlugin {
  public partial class Plugin {
    public const short PluginInfoVersion = 1;
    public const short MinInterfaceVersion = 35;
    public const short MinApiRevision = 45;

    [StructLayout(LayoutKind.Sequential)]
    public struct MusicBeeApiInterface {
      public short InterfaceVersion;
      public short ApiRevision;
      public IntPtr MB_ReleaseString;
      public IntPtr MB_Trace;
      public Setting_GetPersistentStoragePathDelegate Setting_GetPersistentStoragePath;
      public IntPtr Setting_GetSkin;
      public IntPtr Setting_GetSkinElementColour;
      public IntPtr Setting_IsWindowBordersSkinned;
      public Library_GetFilePropertyDelegate Library_GetFileProperty;
      public Library_GetFileTagDelegate Library_GetFileTag;
      public Library_SetFileTagDelegate Library_SetFileTag;
      public Library_CommitTagsToFileDelegate Library_CommitTagsToFile;
      public IntPtr Library_GetLyrics;
      public IntPtr Library_GetArtwork;
      public IntPtr Library_QueryFiles;
      public IntPtr Library_QueryGetNextFile;
      public IntPtr Player_GetPosition;
      public IntPtr Player_SetPosition;
      public IntPtr Player_GetPlayState;
      public IntPtr Player_PlayPause;
      public IntPtr Player_Stop;
      public IntPtr Player_StopAfterCurrent;
      public IntPtr Player_PlayPreviousTrack;
      public IntPtr Player_PlayNextTrack;
      public IntPtr Player_StartAutoDj;
      public IntPtr Player_EndAutoDj;
      public IntPtr Player_GetVolume;
      public IntPtr Player_SetVolume;
      public IntPtr Player_GetMute;
      public IntPtr Player_SetMute;
      public IntPtr Player_GetShuffle;
      public IntPtr Player_SetShuffle;
      public IntPtr Player_GetRepeat;
      public IntPtr Player_SetRepeat;
      public IntPtr Player_GetEqualiserEnabled;
      public IntPtr Player_SetEqualiserEnabled;
      public IntPtr Player_GetDspEnabled;
      public IntPtr Player_SetDspEnabled;
      public IntPtr Player_GetScrobbleEnabled;
      public IntPtr Player_SetScrobbleEnabled;
      public IntPtr NowPlaying_GetFileUrl;
      public IntPtr NowPlaying_GetDuration;
      public IntPtr NowPlaying_GetFileProperty;
      public IntPtr NowPlaying_GetFileTag;
      public IntPtr NowPlaying_GetLyrics;
      public IntPtr NowPlaying_GetArtwork;
      public IntPtr NowPlayingList_Clear;
      public IntPtr NowPlayingList_QueryFiles;
      public IntPtr NowPlayingList_QueryGetNextFile;
      public IntPtr NowPlayingList_PlayNow;
      public IntPtr NowPlayingList_QueueNext;
      public IntPtr NowPlayingList_QueueLast;
      public IntPtr NowPlayingList_PlayLibraryShuffled;
      public IntPtr Playlist_QueryPlaylists;
      public IntPtr Playlist_QueryGetNextPlaylist;
      public IntPtr Playlist_GetType;
      public IntPtr Playlist_QueryFiles;
      public IntPtr Playlist_QueryGetNextFile;
      public IntPtr MB_GetWindowHandle;
      public MB_RefreshPanelsDelegate MB_RefreshPanels;
      public IntPtr MB_SendNotification;
      public MB_AddMenuItemDelegate MB_AddMenuItem;
      public IntPtr Setting_GetFieldName;
      public IntPtr Library_QueryGetAllFiles;
      public IntPtr NowPlayingList_QueryGetAllFiles;
      public IntPtr Playlist_QueryGetAllFiles;
      public IntPtr MB_CreateBackgroundTask;
      public IntPtr MB_SetBackgroundTaskMessage;
      public IntPtr MB_RegisterCommand;
      public IntPtr Setting_GetDefaultFont;
      public IntPtr Player_GetShowTimeRemaining;
      public IntPtr NowPlayingList_GetCurrentIndex;
      public IntPtr NowPlayingList_GetListFileUrl;
      public IntPtr NowPlayingList_GetFileProperty;
      public IntPtr NowPlayingList_GetFileTag;
      public IntPtr NowPlaying_GetSpectrumData;
      public IntPtr NowPlaying_GetSoundGraph;
      public IntPtr MB_GetPanelBounds;
      public IntPtr MB_AddPanel;
      public IntPtr MB_RemovePanel;
      public IntPtr MB_GetLocalisation;
      public IntPtr NowPlayingList_IsAnyPriorTracks;
      public IntPtr NowPlayingList_IsAnyFollowingTracks;
      public IntPtr Player_ShowEqualiser;
      public IntPtr Player_GetAutoDjEnabled;
      public IntPtr Player_GetStopAfterCurrentEnabled;
      public IntPtr Player_GetCrossfade;
      public IntPtr Player_SetCrossfade;
      public IntPtr Player_GetReplayGainMode;
      public IntPtr Player_SetReplayGainMode;
      public IntPtr Player_QueueRandomTracks;
      public IntPtr Setting_GetDataType;
      public IntPtr NowPlayingList_GetNextIndex;
      public IntPtr NowPlaying_GetArtistPicture;
      public IntPtr NowPlaying_GetDownloadedArtwork;
      public IntPtr MB_ShowNowPlayingAssistant;
      public IntPtr NowPlaying_GetDownloadedLyrics;
      public IntPtr Player_GetShowRatingTrack;
      public IntPtr Player_GetShowRatingLove;
      public IntPtr MB_CreateParameterisedBackgroundTask;
      public IntPtr Setting_GetLastFmUserId;
      public IntPtr Playlist_GetName;
      public IntPtr Playlist_CreatePlaylist;
      public IntPtr Playlist_SetFiles;
      public IntPtr Library_QuerySimilarArtists;
      public IntPtr Library_QueryLookupTable;
      public IntPtr Library_QueryGetLookupTableValue;
      public IntPtr NowPlayingList_QueueFilesNext;
      public IntPtr NowPlayingList_QueueFilesLast;
      public IntPtr Setting_GetWebProxy;
      public IntPtr NowPlayingList_RemoveAt;
      public IntPtr Playlist_RemoveAt;
      public IntPtr MB_SetPanelScrollableArea;
      public IntPtr MB_InvokeCommand;
      public IntPtr MB_OpenFilterInTab;
      public IntPtr MB_SetWindowSize;
      public IntPtr Library_GetArtistPicture;
      public IntPtr Pending_GetFileUrl;
      public IntPtr Pending_GetFileProperty;
      public IntPtr Pending_GetFileTag;
      public IntPtr Player_GetButtonEnabled;
      public IntPtr NowPlayingList_MoveFiles;
      public IntPtr Library_GetArtworkUrl;
      public IntPtr Library_GetArtistPictureThumb;
      public IntPtr NowPlaying_GetArtworkUrl;
      public IntPtr NowPlaying_GetDownloadedArtworkUrl;
      public IntPtr NowPlaying_GetArtistPictureThumb;
      public IntPtr Playlist_IsInList;
      public IntPtr Library_GetArtistPictureUrls;
      public IntPtr NowPlaying_GetArtistPictureUrls;
      public IntPtr Playlist_AppendFiles;
      public IntPtr Sync_FileStart;
      public IntPtr Sync_FileEnd;
      public Library_QueryFilesExDelegate Library_QueryFilesEx;
      public IntPtr NowPlayingList_QueryFilesEx;
      public IntPtr Playlist_QueryFilesEx;
      public IntPtr Playlist_MoveFiles;
      public IntPtr Playlist_PlayNow;
      public IntPtr NowPlaying_IsSoundtrack;
      public IntPtr NowPlaying_GetSoundtrackPictureUrls;
      public IntPtr Library_GetDevicePersistentId;
      public IntPtr Library_SetDevicePersistentId;
      public IntPtr Library_FindDevicePersistentId;
      public IntPtr Setting_GetValue;
      public IntPtr Library_AddFileToLibrary;
      public IntPtr Playlist_DeletePlaylist;
      public IntPtr Library_GetSyncDelta;
      public IntPtr Library_GetFileTags;
      public IntPtr NowPlaying_GetFileTags;
      public IntPtr NowPlayingList_GetFileTags;
      public IntPtr MB_AddTreeNode;
      public IntPtr MB_DownloadFile;
      public IntPtr Setting_GetFileConvertCommandLine;
      public IntPtr Player_OpenStreamHandle;
      public Player_UpdatePlayStatisticsDelegate Player_UpdatePlayStatistics;
    }

    public enum PluginType {
      Unknown = 0,
      General = 1,
    }

    [StructLayout(LayoutKind.Sequential)]
    public class PluginInfo {
      public short PluginInfoVersion;
      public PluginType Type;
      public string Name;
      public string Description;
      public string Author;
      public string TargetApplication;
      public short VersionMajor;
      public short VersionMinor;
      public short Revision;
      public short MinInterfaceVersion;
      public short MinApiRevision;
      public ReceiveNotificationFlags ReceiveNotifications;
      public int ConfigurationPanelHeight;
    }

    [Flags]
    public enum ReceiveNotificationFlags {
      StartupOnly = 0x0,
      PlayerEvents = 0x1,
      DataStreamEvents = 0x2,
      TagEvents = 0x4,
      DownloadEvents = 0x8,
    }

    public enum NotificationType {
      PluginStartup = 0,
    }

    public enum PluginCloseReason {
      MusicBeeClosing = 1,
      UserDisabled = 2,
      StopNoUnload = 3,
    }

    public enum FilePropertyType {
      Url = 2,
      PlayCount = 14,
      SkipCount = 15,
    }

    public enum MetaDataType {
      Album = 30,
      Artist = 32,
      TrackTitle = 65,
      Rating = 75,
      RatingLove = 76,
    }

    public enum PlayStatisticType {
      NoChange = 0,
      IncreasePlayCount = 1,
      IncreaseSkipCount = 2,
    }

    public delegate string Setting_GetPersistentStoragePathDelegate();
    public delegate string Library_GetFilePropertyDelegate(
        string sourceFileUrl, FilePropertyType type);
    public delegate string Library_GetFileTagDelegate(
        string sourceFileUrl, MetaDataType field);
    public delegate bool Library_SetFileTagDelegate(
        string sourceFileUrl, MetaDataType field, string value);
    public delegate bool Library_CommitTagsToFileDelegate(string sourceFileUrl);
    public delegate bool Library_QueryFilesExDelegate(
        string query, out string[] files);
    public delegate void MB_RefreshPanelsDelegate();
    public delegate ToolStripItem MB_AddMenuItemDelegate(
        string menuPath, string hotkeyDescription, EventHandler handler);
    public delegate bool Player_UpdatePlayStatisticsDelegate(
        string url, PlayStatisticType countType, bool disableScrobble);
  }
}
