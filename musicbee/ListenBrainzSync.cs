using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;

namespace MusicBeePlugin {
  public partial class Plugin {
    private MusicBeeApiInterface mbApiInterface;
    private readonly PluginInfo about = new PluginInfo();
    private string stateDir = string.Empty;
    private int syncingFlag = 0;

    public PluginInfo Initialise(IntPtr apiInterfacePtr) {
      mbApiInterface = (MusicBeeApiInterface)MarshalCompat.PtrToApi(
          apiInterfacePtr);
      about.PluginInfoVersion = PluginInfoVersion;
      about.Name = "ListenBrainz Playcount & Hearts Sync";
      about.Description =
          "Syncs non-MusicBee ListenBrainz plays and recent Hearts on startup.";
      about.Author = "listenbrainz-sync";
      about.TargetApplication = "";
      about.Type = PluginType.General;
      about.VersionMajor = 1;
      about.VersionMinor = 0;
      about.Revision = 0;
      about.MinInterfaceVersion = MinInterfaceVersion;
      about.MinApiRevision = MinApiRevision;
      about.ReceiveNotifications = ReceiveNotificationFlags.StartupOnly;
      about.ConfigurationPanelHeight = 0;
      return about;
    }

    public bool Configure(IntPtr panelHandle) {
      return false;
    }

    public void SaveSettings() {}

    public void Close(PluginCloseReason reason) {}

    public void Uninstall() {}

    public void ReceiveNotification(string sourceFileUrl,
                                    NotificationType type) {
      if (type == NotificationType.PluginStartup) {
        string storageRoot = mbApiInterface.Setting_GetPersistentStoragePath();
        stateDir = Path.Combine(storageRoot, "ListenBrainzSync");
        Directory.CreateDirectory(stateDir);
        mbApiInterface.MB_AddMenuItem(
            "mnuTools/Sync ListenBrainz Playcounts",
            string.Empty,
            OnManualSyncClicked);
        TriggerSyncAsync();
      }
    }

    private void OnManualSyncClicked(object sender, EventArgs e) {
      TriggerSyncAsync();
    }

    private void TriggerSyncAsync() {
      if (Interlocked.CompareExchange(ref syncingFlag, 1, 0) != 0) {
        return;
      }
      ThreadPool.QueueUserWorkItem(delegate {
        try {
          RunSync();
        } catch (Exception ex) {
          Log("Sync error: " + ex.Message);
        } finally {
          Interlocked.Exchange(ref syncingFlag, 0);
        }
      });
    }

    private void RunSync() {
      ServicePointManager.SecurityProtocol =
          (SecurityProtocolType)3072 | (SecurityProtocolType)12288;

      string token = LoadToken();
      if (string.IsNullOrEmpty(token)) {
        Log("Skipping sync: missing ListenBrainz usertoken.");
        return;
      }

      Dictionary<string, string> state = LoadState();
      bool dryRun = !state.ContainsKey("dry_run") ||
                    state["dry_run"].Equals(
                        "true", StringComparison.OrdinalIgnoreCase);
      bool syncHearts = state.ContainsKey("sync_hearts") &&
                        state["sync_hearts"].Equals(
                            "true", StringComparison.OrdinalIgnoreCase);

      string username = state.ContainsKey("username") ? state["username"] : "";
      if (string.IsNullOrEmpty(username)) {
        username = ValidateAndGetUsername(token);
        if (string.IsNullOrEmpty(username)) {
          Log("Skipping sync: could not validate ListenBrainz token.");
          return;
        }
        state["username"] = username;
      }

      long nowTs = ToUnixSeconds(DateTime.UtcNow);
      long lastSyncTs = 0;
      if (state.ContainsKey("last_sync_ts")) {
        long.TryParse(state["last_sync_ts"], out lastSyncTs);
      }
      if (lastSyncTs <= 0) {
        // Default initial lookback to 24 hours so first run is safe and fast.
        lastSyncTs = nowTs - 86400;
      }

      Dictionary<string, List<string>> libraryIndex = BuildLibraryIndex();
      Log("Indexed " + libraryIndex.Count + " unique (artist, title) keys.");

      long newestSeenTs = lastSyncTs;
      int incremented = SyncPlaycounts(
          username, lastSyncTs, libraryIndex, dryRun, ref newestSeenTs);
      int hearted = 0;
      if (syncHearts) {
        hearted = SyncHearts(username, libraryIndex, dryRun);
      }

      if (!dryRun && newestSeenTs > lastSyncTs) {
        state["last_sync_ts"] = newestSeenTs.ToString();
      }
      if (!state.ContainsKey("dry_run")) {
        state["dry_run"] = dryRun ? "true" : "false";
      }
      if (!state.ContainsKey("sync_hearts")) {
        state["sync_hearts"] = syncHearts ? "true" : "false";
      }
      SaveState(state);

      if (!dryRun && (incremented > 0 || hearted > 0)) {
        mbApiInterface.MB_RefreshPanels();
      }
      Log(string.Format(
          "Sync complete (dry_run={0}): playcounts_incremented={1}, hearts_updated={2}",
          dryRun, incremented, hearted));
    }

    private int SyncPlaycounts(
        string username,
        long minTs,
        Dictionary<string, List<string>> libraryIndex,
        bool dryRun,
        ref long newestSeenTs) {
      JavaScriptSerializer json = new JavaScriptSerializer();
      json.MaxJsonLength = 32 * 1024 * 1024;

      long cursor = minTs;
      int incremented = 0;
      int skippedMusicBee = 0;
      int unmatched = 0;

      while (true) {
        string url = string.Format(
            "https://api.listenbrainz.org/1/user/{0}/listens?min_ts={1}&count=1000",
            Uri.EscapeDataString(username), cursor);
        string body = HttpGet(url, null);
        Dictionary<string, object> root =
            json.DeserializeObject(body) as Dictionary<string, object>;
        if (root == null || !root.ContainsKey("payload")) {
          break;
        }
        Dictionary<string, object> payload =
            root["payload"] as Dictionary<string, object>;
        object[] listens =
            payload != null && payload.ContainsKey("listens")
                ? payload["listens"] as object[]
                : null;
        if (listens == null || listens.Length == 0) {
          break;
        }

        long batchMaxTs = cursor;
        foreach (object itemObj in listens) {
          Dictionary<string, object> item =
              itemObj as Dictionary<string, object>;
          if (item == null || !item.ContainsKey("listened_at")) {
            continue;
          }
          long ts = Convert.ToInt64(item["listened_at"]);
          if (ts > batchMaxTs) {
            batchMaxTs = ts;
          }
          if (ts > newestSeenTs) {
            newestSeenTs = ts;
          }

          Dictionary<string, object> meta =
              item.ContainsKey("track_metadata")
                  ? item["track_metadata"] as Dictionary<string, object>
                  : null;
          if (meta == null) {
            continue;
          }

          Dictionary<string, object> addInfo =
              meta.ContainsKey("additional_info")
                  ? meta["additional_info"] as Dictionary<string, object>
                  : null;
          string client = GetStr(addInfo, "submission_client");
          string player = GetStr(addInfo, "media_player");
          if (client.Equals("ScrobblerBrainz",
                            StringComparison.OrdinalIgnoreCase) ||
              player.Equals("MusicBee", StringComparison.OrdinalIgnoreCase)) {
            skippedMusicBee++;
            continue;
          }

          string artist = GetStr(meta, "artist_name");
          string title = GetStr(meta, "track_name");
          string key = NormalizeKey(artist, title);
          if (!libraryIndex.ContainsKey(key)) {
            unmatched++;
            continue;
          }

          string fileUrl = libraryIndex[key][0];
          if (!dryRun) {
            mbApiInterface.Player_UpdatePlayStatistics(
                fileUrl, PlayStatisticType.IncreasePlayCount, true);
          }
          incremented++;
          Log(string.Format(
              "{0}PlayCount +1 [{1}]: {2} - {3}",
              dryRun ? "[DRY-RUN] " : "", client, artist, title));
        }

        if (listens.Length < 1000 || batchMaxTs <= cursor) {
          break;
        }
        cursor = batchMaxTs;
      }

      Log(string.Format(
          "Playcount scan: skipped_musicbee={0}, matched={1}, unmatched={2}",
          skippedMusicBee, incremented, unmatched));
      return incremented;
    }

    private int SyncHearts(
        string username,
        Dictionary<string, List<string>> libraryIndex,
        bool dryRun) {
      JavaScriptSerializer json = new JavaScriptSerializer();
      string url = string.Format(
          "https://api.listenbrainz.org/1/feedback/user/{0}/get-feedback?score=1&count=100&metadata=true",
          Uri.EscapeDataString(username));
      string body = HttpGet(url, null);
      Dictionary<string, object> root =
          json.DeserializeObject(body) as Dictionary<string, object>;
      object[] feedback =
          root != null && root.ContainsKey("feedback")
              ? root["feedback"] as object[]
              : null;
      if (feedback == null) {
        return 0;
      }

      int updated = 0;
      foreach (object itemObj in feedback) {
        Dictionary<string, object> item =
            itemObj as Dictionary<string, object>;
        Dictionary<string, object> meta =
            item != null && item.ContainsKey("track_metadata")
                ? item["track_metadata"] as Dictionary<string, object>
                : null;
        if (meta == null) {
          continue;
        }
        string artist = GetStr(meta, "artist_name");
        string title = GetStr(meta, "track_name");
        string key = NormalizeKey(artist, title);
        if (!libraryIndex.ContainsKey(key)) {
          continue;
        }

        foreach (string fileUrl in libraryIndex[key]) {
          string rating =
              mbApiInterface.Library_GetFileTag(fileUrl, MetaDataType.Rating);
          if (!string.IsNullOrEmpty(rating) && rating != "0") {
            continue;
          }
          if (!dryRun) {
            mbApiInterface.Library_SetFileTag(fileUrl, MetaDataType.Rating, "4");
            mbApiInterface.Library_CommitTagsToFile(fileUrl);
          }
          updated++;
          Log(string.Format(
              "{0}Rating -> 4 (Heart): {1} - {2}",
              dryRun ? "[DRY-RUN] " : "", artist, title));
        }
      }
      return updated;
    }

    private Dictionary<string, List<string>> BuildLibraryIndex() {
      Dictionary<string, List<string>> index =
          new Dictionary<string, List<string>>();
      string[] files;
      if (!mbApiInterface.Library_QueryFilesEx("", out files) ||
          files == null) {
        return index;
      }
      foreach (string fileUrl in files) {
        string artist =
            mbApiInterface.Library_GetFileTag(fileUrl, MetaDataType.Artist);
        string title = mbApiInterface.Library_GetFileTag(
            fileUrl, MetaDataType.TrackTitle);
        string key = NormalizeKey(artist, title);
        if (string.IsNullOrEmpty(key) || key == "|") {
          continue;
        }
        if (!index.ContainsKey(key)) {
          index[key] = new List<string>();
        }
        index[key].Add(fileUrl);
      }
      return index;
    }

    private string LoadToken() {
      string localTokenPath = Path.Combine(stateDir, "usertoken");
      if (File.Exists(localTokenPath)) {
        return File.ReadAllText(localTokenPath).Trim();
      }
      string storageRoot = mbApiInterface.Setting_GetPersistentStoragePath();
      string sbTokenPath = Path.Combine(
          Path.Combine(storageRoot, "ScrobblerBrainz"), "usertoken");
      if (File.Exists(sbTokenPath)) {
        return File.ReadAllText(sbTokenPath).Trim();
      }
      return string.Empty;
    }

    private string ValidateAndGetUsername(string token) {
      JavaScriptSerializer json = new JavaScriptSerializer();
      string body = HttpGet(
          "https://api.listenbrainz.org/1/validate-token",
          "Token " + token);
      Dictionary<string, object> data =
          json.DeserializeObject(body) as Dictionary<string, object>;
      return GetStr(data, "user_name");
    }

    private Dictionary<string, string> LoadState() {
      Dictionary<string, string> state = new Dictionary<string, string>();
      string path = Path.Combine(stateDir, "state.ini");
      if (!File.Exists(path)) {
        return state;
      }
      foreach (string line in File.ReadAllLines(path)) {
        int idx = line.IndexOf('=');
        if (idx <= 0) {
          continue;
        }
        string k = line.Substring(0, idx).Trim();
        string v = line.Substring(idx + 1).Trim();
        if (!string.IsNullOrEmpty(k)) {
          state[k] = v;
        }
      }
      return state;
    }

    private void SaveState(Dictionary<string, string> state) {
      string path = Path.Combine(stateDir, "state.ini");
      List<string> lines = new List<string>();
      foreach (KeyValuePair<string, string> kv in state) {
        lines.Add(kv.Key + "=" + kv.Value);
      }
      File.WriteAllLines(path, lines.ToArray());
    }

    private static string HttpGet(string url, string authHeader) {
      using (System.Net.Http.HttpClient client =
                 new System.Net.Http.HttpClient()) {
        client.Timeout = TimeSpan.FromSeconds(20);
        client.DefaultRequestHeaders.UserAgent.ParseAdd(
            "mb_ListenBrainzSync/1.0");
        if (!string.IsNullOrEmpty(authHeader)) {
          client.DefaultRequestHeaders.TryAddWithoutValidation(
              "Authorization", authHeader);
        }
        for (int attempt = 0; attempt < 4; attempt++) {
          System.Net.Http.HttpResponseMessage resp =
              client.GetAsync(url).Result;
          if ((int)resp.StatusCode == 429 || (int)resp.StatusCode >= 500) {
            int sleepMs = (attempt + 1) * 2000;
            IEnumerable<string> vals;
            if (resp.Headers.TryGetValues("X-RateLimit-Reset-In", out vals)) {
              foreach (string v in vals) {
                double sec;
                if (double.TryParse(v, out sec)) {
                  sleepMs = Math.Min((int)((sec + 0.5) * 1000), 15000);
                  break;
                }
              }
            }
            Thread.Sleep(sleepMs);
            continue;
          }
          resp.EnsureSuccessStatusCode();
          return resp.Content.ReadAsStringAsync().Result;
        }
        throw new Exception("ListenBrainz API rate-limited after retries.");
      }
    }

    private static string GetStr(Dictionary<string, object> dict, string key) {
      if (dict == null || !dict.ContainsKey(key) || dict[key] == null) {
        return string.Empty;
      }
      return dict[key].ToString().Trim();
    }

    private static string NormalizeKey(string artist, string title) {
      string a = Regex.Replace(
          (artist ?? "").Trim().ToLowerInvariant(), @"\s+", " ");
      string t = Regex.Replace(
          (title ?? "").Trim().ToLowerInvariant(), @"\s+", " ");
      return a + "|" + t;
    }

    private static long ToUnixSeconds(DateTime utc) {
      DateTime epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
      return (long)(utc - epoch).TotalSeconds;
    }

    private void Log(string msg) {
      if (string.IsNullOrEmpty(stateDir)) {
        return;
      }
      string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + msg;
      try {
        File.AppendAllText(
            Path.Combine(stateDir, "sync.log"), line + Environment.NewLine);
      } catch {
      }
    }
  }

  internal static class MarshalCompat {
    public static Plugin.MusicBeeApiInterface PtrToApi(IntPtr ptr) {
      return (Plugin.MusicBeeApiInterface)
          System.Runtime.InteropServices.Marshal.PtrToStructure(
              ptr, typeof(Plugin.MusicBeeApiInterface));
    }
  }
}
