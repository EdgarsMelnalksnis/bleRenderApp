from flask import Flask, render_template, request, redirect
import pandas as pd
from datetime import datetime, timedelta
import os
import json
from io import BytesIO

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

app = Flask(__name__)

# === Google Drive Auth Setup ===
SCOPES = ['https://www.googleapis.com/auth/drive.file']
DRIVE_FOLDER_ID = "1NFAKM3Q66EQJAtDiVNvKmPbbwUYtB6qh"

def get_drive_service():
    import googleapiclient.discovery
    creds_data = json.loads(os.environ['GOOGLE_CREDENTIALS_JSON'])
    token_data = json.loads(os.environ['GOOGLE_TOKEN_JSON'])
    creds = Credentials.from_authorized_user_info(token_data)
    return googleapiclient.discovery.build('drive', 'v3', credentials=creds)

def load_latest_logs_from_drive():
    log_files = get_drive_file_map()
    combined_logs = []
    for hub, file_id in log_files.items():
        try:
            df = download_csv_from_drive(file_id)
            df["hub"] = hub
            df["time"] = pd.to_datetime(df["timestamp"], unit="s")
            df = df.drop_duplicates(subset=["mac", "time", "hub", "rssi"])
            combined_logs.append(df)
        except Exception as e:
            print(f"Failed to load log for {hub}: {e}")
    if combined_logs:
        full_df = pd.concat(combined_logs).sort_values(["mac", "time"])
        full_df["time_diff"] = full_df.groupby("mac")["time"].diff().fillna(pd.Timedelta(seconds=0))
        full_df["time_diff"] = full_df["time_diff"].apply(lambda x: str(x).split('.')[0])
        return full_df
    return pd.DataFrame()

def get_drive_file_map():
    service = get_drive_service()
    results = service.files().list(
        q=f"'{DRIVE_FOLDER_ID}' in parents and name contains '_logs.csv' and trashed = false",
        fields="files(id, name)"
    ).execute()
    files = results.get('files', [])
    return {f['name'].split("_")[0]: f['id'] for f in files}

def download_csv_from_drive(file_id):
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)
    fh = BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.seek(0)
    return pd.read_csv(fh)

# === Load logs from Google Drive ===
log_files = get_drive_file_map()
combined_logs = []
for hub, file_id in log_files.items():
    try:
        df = download_csv_from_drive(file_id)
        df["hub"] = hub
        df["time"] = pd.to_datetime(df["timestamp"], unit="s")
        df = df.drop_duplicates(subset=["mac", "time", "hub", "rssi"])
        combined_logs.append(df)
    except Exception as e:
        print(f"❌ Failed to load log for {hub}: {e}")

full_df = pd.concat(combined_logs) if combined_logs else pd.DataFrame()
full_df = full_df.sort_values(["mac", "time"])
full_df["time_diff"] = full_df.groupby("mac")["time"].diff().fillna(pd.Timedelta(seconds=0))
full_df["time_diff"] = full_df["time_diff"].apply(lambda x: str(x).split('.')[0])

all_hubs = sorted(full_df["hub"].unique()) if not full_df.empty else []
valid_macs = sorted(full_df["mac"].unique()) if not full_df.empty else []

# === MAC and HUB rename maps ===
MAC_NAME_FILE = "mac_names.json"
HUB_NAME_FILE = "hub_names.json"
mac_name_map = json.load(open(MAC_NAME_FILE)) if os.path.exists(MAC_NAME_FILE) else {}
hub_name_map = json.load(open(HUB_NAME_FILE)) if os.path.exists(HUB_NAME_FILE) else {}

# === Helper functions ===
def filter_by_time(df, time_filter, from_date=None, to_date=None, from_time=None, to_time=None):
    now = datetime.now()

    def combine_date_time(date_str, time_str, default_time):
        try:
            base = datetime.strptime(date_str, "%Y-%m-%d")
            if time_str:
                t = datetime.strptime(time_str, "%H:%M").time()
            else:
                t = default_time
            return datetime.combine(base.date(), t)
        except:
            return None

    if time_filter == "custom":
        start = combine_date_time(from_date, from_time, datetime.min.time()) if from_date else df["time"].min()
        end = combine_date_time(to_date, to_time, datetime.max.time()) if to_date else df["time"].max()
        return df[(df["time"] >= start) & (df["time"] <= end)]

    elif time_filter == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return df[df["time"] >= start]

    elif time_filter == "yesterday":
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return df[(df["time"] >= start) & (df["time"] < end)]

    elif time_filter == "this_week":
        start = now - timedelta(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        return df[df["time"] >= start]

    elif time_filter == "last_week":
        start = now - timedelta(days=now.weekday() + 7)
        end = start + timedelta(days=7)
        return df[(df["time"] >= start) & (df["time"] < end)]

    return df

def compute_mac_stats():
    stats = {}
    today = datetime.now().date()
    for mac in valid_macs:
        mac_df = full_df[full_df["mac"] == mac]
        last_seen = mac_df["time"].max()
        today_count = mac_df[mac_df["time"].dt.date == today].shape[0]
        total_count = mac_df.shape[0]
        stats[mac] = {
            "name": mac_name_map.get(mac, ""),
            "last_seen": last_seen.strftime("%Y-%m-%d %H:%M:%S") if pd.notnull(last_seen) else "—",
            "today_count": today_count,
            "total_count": total_count
        }
    return stats

def compute_hub_stats():
    stats = {}
    today = datetime.now().date()
    for hub in all_hubs:
        hub_df = full_df[full_df["hub"] == hub]
        last_seen = hub_df["time"].max()
        today_count = hub_df[hub_df["time"].dt.date == today].shape[0]
        total_count = hub_df.shape[0]
        stats[hub] = {
            "name": hub_name_map.get(hub, ""),
            "last_seen": last_seen.strftime("%Y-%m-%d %H:%M:%S") if pd.notnull(last_seen) else "—",
            "today_count": today_count,
            "total_count": total_count
        }
    return stats

# === Routes ===
@app.route("/")
def index():
    df = load_latest_logs_from_drive()
    return render_template("index.html",
        macs=valid_macs,
        hubs=all_hubs,
        mac_stats=compute_mac_stats(),
        hub_stats=compute_hub_stats()
    )

@app.route("/mac")
def mac_logs():
    mac = request.args.get("mac")
    hubs = request.args.getlist("hub")
    time_filter = request.args.get("time_filter")
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")
    from_time = request.args.get("from_time")
    to_time = request.args.get("to_time")

    df = full_df[full_df["mac"] == mac]
    if hubs:
        df = df[df["hub"].isin(hubs)]
    df = filter_by_time(df, time_filter, from_date, to_date, from_time, to_time)
    df = df.sort_values("time")

    return render_template("mac_logs.html",
        mac=mac,
        logs=df,
        selected_hubs=hubs,
        mac_stats=compute_mac_stats(),
        hub_stats=compute_hub_stats()
    )

@app.route("/hub")
def hub_logs():
    hub = request.args.get("hub")
    time_filter = request.args.get("time_filter")
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")
    from_time = request.args.get("from_time")
    to_time = request.args.get("to_time")

    df = full_df[full_df["hub"] == hub]
    df = filter_by_time(df, time_filter, from_date, to_date, from_time, to_time)
    df = df.sort_values("time")

    return render_template("hub_logs.html",
        hub=hub,
        logs=df,
        mac_stats=compute_mac_stats(),
        hub_stats=compute_hub_stats()
    )

@app.route("/rename", methods=["POST"])
def rename():
    for key, value in request.form.items():
        if key.startswith("rename_"):
            mac = key.replace("rename_", "")
            mac_name_map[mac] = value.strip()
    with open(MAC_NAME_FILE, "w") as f:
        json.dump(mac_name_map, f, indent=2)
    return redirect("/")

@app.route("/rename_hubs", methods=["POST"])
def rename_hubs():
    for key, value in request.form.items():
        if key.startswith("renamehub_"):
            hub = key.replace("renamehub_", "")
            hub_name_map[hub] = value.strip()
    with open(HUB_NAME_FILE, "w") as f:
        json.dump(hub_name_map, f, indent=2)
    return redirect("/")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=True)

